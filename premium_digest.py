"""Premium: утренняя сводка по Watchlist (PREMIUM_DIGEST, только при PREMIUM_ENABLED и ALERTS_ENABLED).

Свой поток "premium-digest": раз в TICK секунд смотрит, не пора ли. Раз в день в час PREMIUM_DIGEST_HOUR_UTC
(по умолчанию 8; опоздали — рестарт, сбой — уходит в течение WINDOW_H часов, позже — уже завтра) каждому
премиум-холдеру (баланс ≥ порога) с непустым Watchlist и не выключившему сводку (/digest off) — одно сообщение:
по каждому токену текущий вердикт из последнего снимка alerts и что поменялось за сутки — alerts.diff(снимок на момент
прошлой сводки, последний снимок): вердикт, probably rug, продажи крупнейшего оператора, ранние покупатели; плюс
изменение цены за 24 ч по DexScreener (один запрос на 30 токенов всех получателей сразу, без RPC).
Никаких сканов: только снимки, которые уже записали живые сканы и плановые перепроверки alerts.
Отправка — через очередь уведомлений alerts (notify → alerts_notify, её лимит 20/с и обработка 403 / 429).
День последней сводки и снимки «на момент сводки» — в premium.db: после рестарта второй раз не уходит.
"""
import html, threading, time, traceback
from datetime import datetime, timezone

import alerts
import premium

TICK = 60            # секунд между проверками «пора ли»
WINDOW_H = 3         # часов после назначенного, в которые сводка ещё уходит
BASE_MAX_AGE = 30 * 3600   # база старше (токен давно не был ни в одной сводке) — сравнивать не с чем
MAX_TEXT = 3900      # символов в сообщении (лимит Telegram 4096)
DAY_KEY = "digest_day"
WATCHLIST_BUTTON = {"inline_keyboard": [[{"text": "🔔 Watchlist", "callback_data": "watchlist"}]]}


def _e(x):
    return html.escape(str(x), quote=False)


def _short(addr):
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def price_change(pct):
    """+12.3% / −45.0%; нет данных — None."""
    if pct is None:
        return None
    return f"{'+' if pct >= 0 else '−'}{abs(pct):.1f}%"


def token_block(token, cur, base, mkt):
    """Строки одного токена: вердикт сейчас, цена за 24 ч, изменения за сутки."""
    ticker = (cur or {}).get("ticker") or (mkt or {}).get("ticker")
    name = f"${_e(ticker)}" if ticker else _e(_short(token))
    pc = price_change((mkt or {}).get("change_24h"))
    price = f" · 24h price {pc}" if pc else ""
    if not cur:
        return [f"⚪️ <b>{name}</b> · not scanned yet{price}"]
    band = cur.get("band") or ""
    if cur.get("score") is not None:
        head = f"{alerts.BAND_ICON.get(band, '⚪️')} <b>{name}</b> · {_e(band)} {cur['score']}/100{price}"
    else:
        head = f"⚪️ <b>{name}</b> · {_e(band.replace('_', ' '))}{price}"
    lines = [head]
    if cur.get("rug"):
        drop = cur.get("rug_drop")
        lines.append("⚠️ probably rug" + (f" −{drop * 100:.0f}%" if drop else ""))
    if base is None:
        lines.append("• New in your digest: changes show from tomorrow")
    else:
        changes = alerts.diff(base, cur)
        lines += [f"• {_e(c['text'])}" for c in changes] or ["• No important changes in 24h"]
    return lines


def message(entries, day):
    """entries — [(токен, снимок | None, база | None, рынок | None)] в порядке Watchlist → (html, кнопки)."""
    head = f"☀️ <b>Morning digest</b> · {day} · {len(entries)} token{'' if len(entries) == 1 else 's'}"
    foot = "/digest off stops this message."
    parts, used = [], len(head) + len(foot) + 4
    for i, (token, cur, base, mkt) in enumerate(entries):
        block = "\n".join(token_block(token, cur, base, mkt))
        if used + len(block) + 2 > MAX_TEXT:
            parts.append(f"…and {len(entries) - i} more. /watchlist shows all.")
            break
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join([head] + parts + [foot]), WATCHLIST_BUTTON


class Digest:
    def __init__(self, store, alerts_store, notify, prices, clock=time.time, sleep=time.sleep, log=None, hour=None):
        """store — PremiumStore; alerts_store() → AlertsStore; notify(user_id, html, markup) — очередь уведомлений;
        prices(chain, tokens) → {токен: рынок с change_24h} (market.ds_batch)."""
        self.store, self.alerts_store, self.notify, self.prices = store, alerts_store, notify, prices
        self.clock, self.sleep = clock, sleep
        self.log = log or (lambda m: print(m, flush=True))
        self.hour = premium.digest_hour() if hour is None else hour
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.loop, daemon=True, name="premium-digest")
            self.thread.start()
        return self

    def loop(self):
        while True:
            try:
                self.tick()
            except Exception:
                self.log("premium: digest loop:\n" + traceback.format_exc())
            self.sleep(TICK)

    def due(self, now):
        """День (YYYY-MM-DD UTC), если сводку пора отправить, иначе None."""
        t = datetime.fromtimestamp(now, timezone.utc)
        if not (self.hour <= t.hour < self.hour + WINDOW_H):
            return None
        day = t.strftime("%Y-%m-%d")
        return None if self.store.meta(DAY_KEY) == day else day

    def tick(self):
        now = int(self.clock())
        day = self.due(now)
        if day is None:
            return None
        self.store.set_meta(DAY_KEY, day)    # до отправки: сбой посередине — второй раз не уходит
        return self.run(now, day)

    def run(self, now, day):
        """Разослать сводку. → сколько сообщений поставлено в очередь."""
        if not alerts.enabled():
            return 0
        ast = self.alerts_store()
        lists = {}
        for u in self.store.premium_users():
            if self.store.digest_on(u):
                ws = ast.watches(u, now)
                if ws:
                    lists[u] = ws
        if not lists:
            self.log("premium: digest: nobody to send")
            return 0
        chains = {}
        for ws in lists.values():
            for w in ws:
                chains[w["token"]] = w.get("chain") or "robinhood"
        snaps = {t: ast.get(t)[1] for t in chains}
        bases = {t: b for t, b in self.store.digest_bases(chains).items()
                 if now - (b.get("_digest_at") or 0) <= BASE_MAX_AGE}
        mkt, n_ds = {}, 0
        for chain in sorted(set(chains.values())):
            toks = sorted(t for t, c in chains.items() if c == chain)
            n_ds += -(-len(toks) // 30)
            try:
                mkt |= self.prices(chain, toks)
            except Exception as e:
                self.log(f"premium: digest prices {chain} failed: {type(e).__name__}: {e}")
        sent = 0
        for u, ws in lists.items():
            text, markup = message([(w["token"], snaps.get(w["token"]), bases.get(w["token"]), mkt.get(w["token"]))
                                    for w in ws], day)
            self.notify(u, text, markup)
            sent += 1
        self.store.set_digest_bases({t: s | {"_digest_at": now} for t, s in snaps.items() if s})
        self.log(f"premium: digest {day}: {sent} users, {len(chains)} tokens, dexscreener {n_ds} requests, 0 rpc")
        return sent
