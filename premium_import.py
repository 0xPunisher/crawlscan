"""Premium: импорт мемкоинов привязанного кошелька в Watchlist (PREMIUM_IMPORT, только при PREMIUM_ENABLED).

Один просмотр кошелька (не чаще раза в premium.IMPORT_EVERY на пользователя — лимит и кэш в server.py):
  1. токены кошелька с ненулевым балансом — ch.wallet_token_balances: обычный кошелёк — 1 RPC
     (alchemy_getTokenBalances, до 100 токенов); длинная история — 3 RPC (первая страница + контракты из последних
     1000 входящих переводов + их балансы одним запросом);
  2. цена — DexScreener пачкой (market.ds_batch): 1 запрос на 30 токенов (без RPC); стоимость = баланс × цена
     (18 знаков — как у всех токенов Pons, Flap и Bankr); токены без пары на DexScreener не считаются;
  3. лаунчпад — от самых дорогих вниз, пачками по CHECK_BATCH в одном HTTP-батче eth_call: Pons —
     factory.getLaunchedToken, Flap (суффикс 8888/7777, FLAP_ENABLED) — Portal.getTokenV8Safe, Bankr (суффикс ba3,
     BANKR_ENABLED) — Airlock.getAssetData; ответ кэшируется навсегда (лаунчпад токена не меняется). Останавливаемся,
     когда нашли TOP поддерживаемых или проверили CHECK_MAX;
  4. TOO ESTABLISHED (последний снимок, кэш вердикта или данные DexScreener — detect.too_established) и TOO ACTIVE
     (bankr.recently_active, память) — в списке с пометкой, без кнопки.
Ни одного скана. $CrawlScan — обычный токен Pons: показывается, как остальные.
"""
import threading

import market
import premium
from chains import bankr, flap

MAX_TOKENS = 300   # токенов с балансом на оценку в DexScreener (10 запросов)
CHECK_BATCH = 10   # токенов на проверку лаунчпада в одном HTTP-батче
CHECK_MAX = 30     # токенов проверить не больше
TOP = 5            # показать самых дорогих
DECIMALS = 18

_PAD = {}          # токен -> "pons" | "flap" | "bankr" | None (лаунчпад не меняется; в памяти процесса)
PAD_MAX = 50_000
LAUNCHPAD_NAME = {"pons": "Pons", "flap": "Flap", "bankr": "Bankr"}


def launchpads(ch, tokens):
    """{токен: "pons" | "flap" | "bankr" | None} одним HTTP-батчем eth_call на некэшированные (порядок — как в
    движке: Flap, Bankr, иначе Pons). Нода не ответила на вызов Pons — токен не кэшируется (None на этот раз)."""
    need = [t for t in tokens if t not in _PAD]
    calls, idx = [], []
    for t in need:
        calls.append(ch.launched_call(t))
        idx.append((t, "pons"))
        if flap.candidate(t):
            calls.append(flap._call(flap.PORTAL, flap.SEL_STATE + flap._arg(t)))
            idx.append((t, "flap"))
        if bankr.candidate(t):
            calls.append(flap._call(bankr.AIRLOCK, bankr.SEL_ASSET_DATA + flap._arg(t)))
            idx.append((t, "bankr"))
    found, failed = {}, set()
    for (t, kind), r in zip(idx, ch.rpc_batch(calls) if calls else []):
        if r is None:                      # Flap / Bankr: revert — не этот лаунчпад; Pons — сбой ноды
            if kind == "pons":
                failed.add(t)
            continue
        if kind == "pons":
            ok = ch.is_launched(r)
        elif kind == "flap":
            w = flap._words(r)
            ok = len(w) >= len(flap.STATE_FIELDS) and w[0] != 0
        else:
            w = flap._words(r)
            ok = len(w) >= 10 and flap._addr(w[9]) == bankr.INTEGRATOR
        if ok:
            found.setdefault(t, set()).add(kind)
    if len(_PAD) > PAD_MAX:
        _PAD.clear()
    out = {t: _PAD[t] for t in tokens if t in _PAD}
    for t in need:
        kinds = found.get(t, set())
        pad = next((k for k in ("flap", "bankr", "pons") if k in kinds), None)
        out[t] = pad
        if pad or t not in failed:
            _PAD[t] = pad
    return out


def lookup(ch, wallet, ds, established, active, skip=()):
    """Просмотр кошелька → {"items": [{"token", "ticker", "launchpad", "balance", "price_usd", "value_usd",
    "status": "ok" | "established" | "active"}] (до TOP, самые дорогие первыми), "tokens": сколько токенов с балансом,
    "complete": прочитан ли весь список, "stats": {"rpc", "eth_calls", "ds"}}.
    ds(tokens) → {токен: рынок} (market.ds_batch); established(token, рынок) / active(token) → bool."""
    r0 = ch.REQUESTS[0] if hasattr(ch, "REQUESTS") else 0
    bal, complete = ch.wallet_token_balances(wallet)
    skip = {s.lower() for s in skip}
    toks = sorted((t for t in bal if t not in skip), key=lambda t: -bal[t])[:MAX_TOKENS]
    mkt = ds(toks) if toks else {}
    n_ds = -(-len(toks) // market.DS_BATCH)
    priced = []
    for t in toks:
        price = (mkt.get(t) or {}).get("price_usd")
        if price:
            amount = bal[t] / 10 ** DECIMALS
            priced.append((amount * price, t, amount))
    priced.sort(key=lambda x: -x[0])
    items, checked, eth_calls = [], 0, 0
    while len(items) < TOP and checked < min(CHECK_MAX, len(priced)):
        part = priced[checked:checked + CHECK_BATCH]
        checked += len(part)
        eth_calls += sum(1 + flap.candidate(t) + bankr.candidate(t) for _, t, _ in part if t not in _PAD)
        pads = launchpads(ch, [t for _, t, _ in part])
        for value, t, amount in part:
            if pads.get(t) and len(items) < TOP:
                m = mkt[t]
                status = "active" if active(t) else "established" if established(t, m) else "ok"
                items.append({"token": t, "ticker": m.get("ticker"), "launchpad": pads[t], "balance": amount,
                              "price_usd": m.get("price_usd"), "value_usd": round(value, 2), "status": status})
    rpc = (ch.REQUESTS[0] - r0) if hasattr(ch, "REQUESTS") else None
    return {"items": items, "tokens": len(bal), "complete": complete,
            "stats": {"rpc": rpc, "eth_calls": eth_calls, "ds": n_ds}}


class Imports:
    """Последний просмотр кошелька каждого пользователя (в памяти процесса): лимит premium.IMPORT_EVERY и кнопки
    «добавить» берут токены отсюда, без нового просмотра."""

    def __init__(self, every=None, keep=3600):
        self.lock, self.by_user, self.running = threading.Lock(), {}, set()
        self.every = every if every is not None else premium.IMPORT_EVERY
        self.keep = keep

    def get(self, user_id, now):
        with self.lock:
            hit = self.by_user.get(user_id)
            return hit if hit and now - hit["at"] < self.keep else None

    def begin(self, user_id, wallet, now):
        """Можно ли смотреть кошелёк сейчас. → (None, None) — можно (и пометка «идёт»); (последний просмотр, None) —
        не прошло IMPORT_EVERY, тот же кошелёк: отдать его; (None, секунд до следующего) — нельзя."""
        with self.lock:
            hit = self.by_user.get(user_id)
            if user_id in self.running:
                return None, max(1, int(self.every - (now - hit["at"]))) if hit else 5
            if hit and now - hit["at"] < self.every:
                if hit["wallet"] == wallet:
                    return hit, None
                return None, max(1, int(self.every - (now - hit["at"])))
            self.running.add(user_id)
            return None, None

    def done(self, user_id, wallet, now, result):
        with self.lock:
            self.running.discard(user_id)
            if result is not None:
                self.by_user[user_id] = {"at": now, "wallet": wallet} | result
                if len(self.by_user) > 10000:
                    self.by_user = {k: v for k, v in self.by_user.items() if now - v["at"] < self.keep}


def view(hit, watching, limit, now):
    """Ответ /api/premium/import: последний просмотр + что уже в Watchlist (свежее, из базы)."""
    items = [it | {"watching": it["token"] in watching} for it in hit["items"]]
    return {"ok": True, "wallet": hit["wallet"], "items": items, "tokens": hit["tokens"], "complete": hit["complete"],
            "at": hit["at"], "next_at": hit["at"] + premium.IMPORT_EVERY, "watch_count": len(watching),
            "limit": limit}


def add(hit, want, watch, watching, limit):
    """Добавить в Watchlist токены из последнего просмотра. want — список адресов или "all". watch(token) →
    "ok" | "renewed" | "limit". → {"added", "already", "full", "skipped": [{"token", "reason"}]}.
    TOO ESTABLISHED и TOO ACTIVE не добавляются; адрес не из просмотра — skipped "unknown"."""
    by_token = {it["token"]: it for it in hit["items"]}
    tokens = [it["token"] for it in hit["items"]] if want == "all" else [t.lower() for t in want]
    out = {"added": [], "already": [], "full": [], "skipped": []}
    for t in tokens:
        it = by_token.get(t)
        if it is None:
            out["skipped"].append({"token": t, "reason": "unknown"})
        elif it["status"] != "ok":
            out["skipped"].append({"token": t, "reason": it["status"], "ticker": it.get("ticker")})
        elif t in watching:
            out["already"].append(t)
        elif out["full"]:
            out["full"].append(t)
        elif watch(t) == "limit":
            out["full"].append(t)
        else:
            out["added"].append(t)
            watching.add(t)
    return out

