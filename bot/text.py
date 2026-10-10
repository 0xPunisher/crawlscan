"""Тексты бота и вид вердикта (HTML parse mode). Чистые функции без сети.
Распознавание адреса — та же логика, что у сайта (engine.chain_of): 0x + 40 hex → Robinhood,
base58 32 байта (32–44 символа) → Solana."""
import html, re
from datetime import datetime, timezone

WEBSITE = "https://crawlscan.fun"
OFFICIAL_CA = "0x19dCb63C4d2F29A6f077F094a4f858fC790145e1"
BUY_URL = "https://www.ponsfamily.com/launchpad/" + OFFICIAL_CA

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CHAIN_NAME = {"robinhood": "Robinhood Chain", "solana": "Solana"}
BAND_ICON = {"CLEAN": "🟢", "OK": "🟡", "RISKY": "🟠", "DANGER": "🔴"}
TOO_EARLY = "TOO_EARLY_OR_LATE"
TOO_ESTABLISHED = "TOO_ESTABLISHED"
TOO_ACTIVE = "TOO_ACTIVE"
ACTIVE_HEADLINE = "This token has too many trades for a full scan right now."
ESTABLISHED_HEADLINE = "This token is too established for CrawlScan."
ESTABLISHED_TEXT = ("CrawlScan is built for fresh memecoins. On large, older tokens the top holders are mostly "
                    "exchanges and big liquidity pools: tokens reach exchange wallets by transfer, not by buying, "
                    "and liquidity is spread across many pools, so holder patterns don't mean what they mean "
                    "on a fresh launch.")


def _b58_len(s):
    """Сколько байт в base58-строке, None — не base58."""
    n = 0
    for c in s:
        k = B58.find(c)
        if k < 0:
            return None
        n = n * 58 + k
    zeros = len(s) - len(s.lstrip("1"))
    return zeros + (n.bit_length() + 7) // 8


def chain_of(s):
    """Строка → "robinhood" | "solana" | None."""
    if ADDR_RE.match(s):
        return "robinhood"
    if 32 <= len(s) <= 44 and _b58_len(s) == 32:
        return "solana"
    return None


def find_address(text):
    """Первый адрес токена в тексте (целиком или внутри ссылки) → (сеть, адрес) или None."""
    text = (text or "").strip()
    for piece in [text] + re.split(r"[^0-9A-Za-z]+", text):
        chain = chain_of(piece)
        if chain:
            return chain, piece
    return None


def short(addr):
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def e(s):
    return html.escape(str(s), quote=False)


START = (
    "<b>CrawlScan</b> shows how many real people are behind a memecoin's top holders.\n\n"
    "Send me a token address from Robinhood Chain or Solana. Crawlers go through the top 20 holders onchain "
    "and check who actually bought and who just received tokens, fresh wallets with no history, snipers still "
    "holding, and wallets linked by the same transaction, distributor or funder.\n\n"
    "In about 15 seconds you get a score from 0 to 100 and a verdict.\n\n"
    "<b>CrawlScan has its own token, and it rewards its holders.</b>\n\n"
    "Every 24 hours one holder wins 10% of the creator fees, paid in ETH. Every token you hold is a ticket, "
    "so the more you hold, the bigger your chance. Every 12 hours the dev burns tokens. "
    "All verifiable live on the website.\n\n"
    f"CA: <code>{OFFICIAL_CA}</code>"
)
CAPTION_MAX = 1024     # лимит подписи к фото в Telegram (видимый текст, UTF-16)
START_BUTTONS = {"inline_keyboard": [
    [{"text": "Scan a token", "callback_data": "scan"}, {"text": "Help", "callback_data": "help"}],
    [{"text": "Website", "url": WEBSITE}, {"text": "Buy $CrawlScan", "url": BUY_URL}],
]}


def start_buttons(alerts=False, premium=False):
    """Кнопки /start; алерты включены — [🔔 Watchlist] в первом ряду рядом со Scan a token / Help;
    Premium включён — ряд [⭐ Premium features]."""
    if not alerts and not premium:
        return START_BUTTONS
    first, *rest = START_BUTTONS["inline_keyboard"]
    if alerts:
        first = first + [{"text": "🔔 Watchlist", "callback_data": "watchlist"}]
    return {"inline_keyboard": [first, *rest] + ([[PREMIUM_START_BUTTON]] if premium else [])}

HELP = (
    "<b>How to read a verdict?</b>\n\n"
    "<b>Score 0–100</b>: 100 = clean.\n\n"
    "The lower it is, the more the top holders look like a few people.\n\n"
    "<b>Verdicts</b>: 🟢 CLEAN · 🟡 OK · 🟠 RISKY · 🔴 DANGER.\n\n"
    "⏳ <b>TOO EARLY OR LATE</b>: too few holders to judge.\n\n"
    "🏛 <b>TOO ESTABLISHED</b>: a large, older token, not scanned.\n\n"
    "<b>Operators</b>: real people behind the top holders.\n\n"
    "Wallets linked by shared buys or transfers count as one operator.\n\n"
    "<b>Dump impact</b>: how far the price could drop if the biggest operator sold everything.\n\n"
    "<b>Virgin wallets</b>: top holders with no trading history before this token, a typical sign "
    "of prepared wallets.\n\n"
    "<b>Transfer supply</b>: share of the float received by transfer instead of bought.\n\n"
    "<b>Snipers</b>: wallets that bought right after launch and still hold.\n\n"
    "<b>Why</b>: rules that forced the verdict down.\n\n"
    "Send a token address to scan it."
)

LAUNCHPADS_FLAP = "<b>Launchpads</b>: Pons V2 and Flap on Robinhood Chain, pump.fun on Solana.\n\n"
LAUNCHPADS = "<b>Launchpads</b>: {pads} on Robinhood Chain, pump.fun on Solana.\n\n"


def help_text(flap=False, bankr=False):
    """/help; Flap или Bankr включены на сайте — строка о площадках перед последней строкой."""
    if not flap and not bankr:
        return HELP
    tail = "Send a token address to scan it."
    pads = ["Pons V2"] + (["Flap"] if flap else []) + (["Bankr"] if bankr else [])
    line = LAUNCHPADS_FLAP if pads == ["Pons V2", "Flap"] else \
        LAUNCHPADS.format(pads=", ".join(pads[:-1]) + " and " + pads[-1])
    return HELP[:-len(tail)] + line + tail


ASK_ADDRESS = "Send me a token address from Robinhood Chain or Solana"
HINT = "Send me a token address from Robinhood Chain (0x…) or Solana. /help explains the verdict."
SCAN_USAGE = "Usage: /scan &lt;token address&gt;"
BUSY = "🕷 Too many scans right now, try again in a minute."
TIMEOUT = "⌛ The scan took too long. Try again in a minute."
SITE_BUSY = "🕷 Scanner is busy, try again in a few seconds."   # сайт ответил 503 busy (server.BUSY)
UNREACHABLE = "⚠️ CrawlScan is not reachable right now. Try again in a minute."
FAILED = "⚠️ The scan failed. Try again in a minute."


def crawling(addr):
    return f"🕷 crawling {e(short(addr))}…"


def queued(addr):
    return f"⏳ queued {e(short(addr))}…"


def wait(seconds):
    return f"⏳ please wait {seconds} s"


def report_button(addr, trade_url=None, watch=False):
    """[Full report] и, если есть ссылка, [Trade on Axiom] в одном ряду; watch — второй ряд [Watch]."""
    row = [{"text": "Full report", "url": f"{WEBSITE}/?ca={addr}"}]
    if trade_url:
        row.append({"text": "Trade on Axiom", "url": trade_url})
    return {"inline_keyboard": [row] + ([watch_button(addr)] if watch else [])}


def rejected(addr, error):
    """Сайт отклонил адрес (400): not a token address, Solana coming soon и т.п."""
    if error == "not a token address":
        return f"🤔 {e(short(addr))} is not a token address."
    return f"⚠️ {e(error)}"


def scan_error(addr, error):
    """Ошибка скана из /api/result. Внутренние сбои ("scan failed: ...") не показываем как есть."""
    if not error or error.startswith("scan failed"):
        return FAILED
    return f"⚠️ {e(short(addr))}: {e(error)}"


def _pct(x, digits):
    return f"{x * 100:.{digits}f}"


def impact_phrase(impact):
    """Как на сайте: < 1% → "<1%", иначе −X%."""
    return "<1%" if impact < 0.01 else f"−{_pct(impact, 0)}%"


def title(res):
    h = res.get("header") or {}
    name, ticker = h.get("name"), h.get("ticker")
    if ticker and name and name != ticker:
        t = f"<b>${e(ticker)}</b> · {e(name)}"
    elif ticker or name:
        t = f"<b>${e(ticker or name)}</b>"
    else:
        t = f"<b>{e(short(res.get('token', '')))}</b>"
    return f"{t} · {CHAIN_NAME.get(res.get('chain'), e(res.get('chain', '')))}"


RUG_KIND = {"linked": "linked wallets", "transfer": "received by transfer", "virgin": "fresh wallets",
            "bundle": "bundle / snipers"}


def rug_lines(rug):
    """Проекция «probably rug» (только при DANGER): строка с падением и две главные причины по доле."""
    lines = [f"⚠️ <b>Probably rug: −{_pct(rug['drop'], 0)}% if suspicious holders sell</b>"]
    for p in sorted(rug.get("parts") or [], key=lambda p: -p["share"])[:2]:
        n = len(p.get("wallets") or [])
        lines.append(f"• {RUG_KIND.get(p['kind'], e(p['kind']))}: {n} wallet{'' if n == 1 else 's'}, "
                     f"{_pct(p['share'], 1)}% of float")
    return lines


def _usd(v):
    """$ с сокращением: $1.2M, $45.3K, $0.000123."""
    if v >= 1e9:
        return f"${v / 1e9:.1f}B"
    if v >= 1e6:
        return f"${v / 1e6:.1f}M"
    if v >= 1e3:
        return f"${v / 1e3:.1f}K"
    if v >= 1:
        return f"${v:.2f}"
    return f"${v:.3g}" if v else "$0"


def _tax(x):
    return f"{x * 100:.2f}".rstrip("0").rstrip(".") + "%"


def flap_line(res):
    """Токен Flap: "Flap · bonding curve 26% · tax 3%/3%" или "Flap · Uniswap V2"; не Flap — None."""
    f = res.get("flap") if res.get("launchpad") == "flap" else None
    if not isinstance(f, dict):
        return None
    parts = ["Flap", "Uniswap V2" if f.get("phase") == "dex" else e(f.get("phase_text") or "bonding curve")]
    tax = f.get("tax") or {}
    if tax.get("buy") or tax.get("sell"):
        parts.append(f"tax {_tax(tax.get('buy') or 0)}/{_tax(tax.get('sell') or 0)}")
    return " · ".join(parts)


def bankr_line(res):
    """Токен Bankr: "Bankr · ETH pair · dev vesting 15%" (пара — ETH или тикер stock token; вестинг — доля сапплая,
    разблокированное — в скобках, если есть); не Bankr — None."""
    b = res.get("bankr") if res.get("launchpad") == "bankr" else None
    if not isinstance(b, dict):
        return None
    pair = b.get("pair") or {}
    parts = ["Bankr", f"{e(pair.get('symbol') or ('ETH' if pair.get('kind') == 'eth' else 'token'))} pair"]
    v = b.get("vesting") or {}
    if v.get("total_share_supply"):
        unl = v.get("unlocked_share_supply") or 0
        parts.append(f"dev vesting {_tax(v['total_share_supply'])}" + (f" ({_tax(unl)} unlocked)" if unl >= 0.0005 else ""))
    return " · ".join(parts)


def verdict(res):
    """Результат скана сайта → HTML-текст вердикта."""
    lp = flap_line(res) or bankr_line(res)
    lines = [title(res)] + ([lp] if lp else []) + [""]
    holders = len(res.get("holders") or [])
    ops = res.get("operators") or []
    if res.get("band") == TOO_ESTABLISHED:     # полный скан не запускался: без скора
        lines.append(f"🏛 <b>{e(res.get('headline') or ESTABLISHED_HEADLINE)}</b>")
        lines.append(e(res.get("reason") or ESTABLISHED_TEXT))
        return "\n".join(lines)
    if res.get("band") == TOO_ACTIVE:          # Bankr, история больше скана: без вердикта, только точное
        lines.append(f"🌊 <b>{e(res.get('headline') or ACTIVE_HEADLINE)}</b>")
        h = res.get("header") or {}
        mk = [f"{k} {_usd(h[f])}" for k, f in (("price", "price_usd"), ("mcap", "mcap_usd"),
                                               ("liquidity", "liquidity_usd")) if h.get(f) is not None]
        if mk:
            lines.append(" · ".join(mk))
        lines.append("No verdict or score: the full holder history is too long to read in one scan.")
        return "\n".join(lines)
    if res.get("band") == TOO_EARLY:
        lines.append("⏳ <b>Too early or too late</b>")
        lines.append(f"Only {res.get('holders_total', 0)} holders, too few to judge.")
        return "\n".join(lines)

    band = res.get("band", "")
    lines.append(f"{BAND_ICON.get(band, '⚪️')} <b>Score {res.get('score')}/100 · {e(band)}</b>")
    if (ps := res.get("partial_scan")) and ps.get("message"):   # Bankr: полный индекс холдеров ещё строится
        lines.append(f"⏳ {e(ps['message'])}")
    lines.append(f"{holders} top holders → {len(ops)} operator{'' if len(ops) == 1 else 's'}")
    m = res.get("metrics") or {}
    if ops and "impact" in m:
        n = len(ops[0].get("wallets") or [])
        who = f"Biggest operator ({n} wallets)" if n > 1 else "Biggest operator"
        if m["impact"] is None:                   # резерв не измерен надёжно: падение не считаем
            lines.append(f"{who} holds {_pct(m.get('operator') or 0, 1)}% of float · liquidity not measured")
        else:
            lines.append(f"{who} could move price {impact_phrase(m['impact'])} if sold")
    if res.get("rug"):
        lines += [""] + rug_lines(res["rug"])

    signals = []
    if float(_pct(m.get("virgin") or 0, 0)):
        signals.append(f"{_pct(m['virgin'], 0)}% virgin wallets in top")
    if float(_pct(m.get("transfer") or 0, 1)):
        signals.append(f"{_pct(m['transfer'], 1)}% of float received by transfer")
    if float(_pct(m.get("sniper") or 0, 1)):
        signals.append(f"snipers still hold {_pct(m['sniper'], 1)}% of float")
    if signals:
        lines.append("")
        lines += [f"• {s}" for s in signals]

    gates = res.get("gates") or []
    if gates:
        lines += ["", "<b>Why:</b>"]
        lines += [f"• {e(g[len('soft:'):].strip() if g.startswith('soft:') else g)}" for g in gates]
    return "\n".join(lines)


# ---------- /rewards ----------
EXPLORER = "https://robinhoodchain.blockscout.com"
REWARDS_BUTTON = {"inline_keyboard": [[{"text": "Rewards on website", "url": WEBSITE}]]}
REWARDS_OFF = "🎁 Holder rewards are not running right now. Check the website for news."


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s).timestamp() if s else None
    except (TypeError, ValueError):
        return None


def _utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def until(ts, now):
    """Время до события: "in 5h 12m", "in 7m", "in <1m"; прошло — "now"."""
    s = int(ts - now)
    if s <= 0:
        return "now"
    if s < 60:
        return "in <1m"
    h, m = s // 3600, s // 60 % 60
    return f"in {h}h {m}m" if h else f"in {m}m"


def tokens(x):
    """Сумма токенов для показа: 1,500,001 · 12.5 · 0.0042."""
    if x is None:
        return "—"
    return f"{x:,.0f}" if x >= 100 else f"{x:,.2f}".rstrip("0").rstrip(".") if x >= 1 else f"{x:.4g}"


def eth(x):
    """Сумма ETH для показа: до 4 знаков после точки без хвостовых нулей (0.0523, 2.5); меньше 0.0001 — "<0.0001"."""
    if 0 < x < 0.0001:
        return "<0.0001"
    return f"{x:.4f}".rstrip("0").rstrip(".")


def tx_link(tx, label="tx"):
    return f'<a href="{EXPLORER}/tx/{e(tx)}">{e(label)}</a>'


def _when(label, iso, now):
    ts = _parse_iso(iso)
    if ts is None:
        return None
    return f"{label}: <b>{datetime.fromtimestamp(ts, timezone.utc).strftime('%H:%M')} UTC</b> · {until(ts, now)}"


def rewards(st):
    """Статус /api/rewards/status → HTML-текст /rewards. Выключено — REWARDS_OFF."""
    if not isinstance(st, dict) or not st.get("enabled"):
        return REWARDS_OFF
    now = _parse_iso(st.get("now")) or datetime.now(timezone.utc).timestamp()
    lines = ["🎁 <b>$CrawlScan holder rewards</b>", ""]
    lines += [x for x in (_when("🎲 Next draw", st.get("next_draw"), now),
                          _when("🔥 Next burn", st.get("next_burn"), now)) if x]

    d = st.get("last_draw")
    lines.append("")
    if not d:
        lines.append("No draws yet. The first winner is picked at the next draw.")
    elif not d.get("winner"):
        lines.append(f"<b>Draw of {e(d.get('day', ''))}</b>: no eligible holders, the prize carries over.")
    else:
        chance = d.get("chance")
        pct = "—" if chance is None else f"{chance * 100:.2f}%" if chance >= 0.0001 else "<0.01%"
        lines.append(f"<b>Last winner</b> · draw of {e(d.get('day', ''))}")
        lines.append(f"<code>{e(short(d['winner']))}</code> · chance {pct}")
        if d.get("payout_tx") and d.get("payout_currency") == "ETH":   # единый формат: Paid: <сумма> <валюта>
            amt = f"{eth(d['payout_eth'])} ETH · " if d.get("payout_eth") is not None else ""
            lines.append(f"✅ Paid: {amt}{tx_link(d['payout_tx'])}")
        elif d.get("payout_tx"):   # выплаты токеном (до перехода на ETH)
            amt = f"{tokens(d.get('payout_tokens'))} $CrawlScan · " if d.get("payout_tokens") is not None else ""
            lines.append(f"✅ Paid: {amt}{tx_link(d['payout_tx'])}")
        else:
            lines.append("⏳ Payout pending")

    tb, dev = st.get("total_burned"), st.get("burned_by_dev") or {}
    lines.append("")
    if tb and tb.get("amount"):
        pct = f" ({tb['amount'] / tb['minted'] * 100:.2f}% of supply)" if tb.get("minted") else ""
        lines.append(f"🔥 <b>Burned</b>: {tokens(tb.get('amount_tokens'))} $CrawlScan{pct}")
    elif dev.get("amount"):
        lines.append(f"🔥 <b>Burned</b>: {tokens(dev.get('amount_tokens'))} $CrawlScan")
    else:
        lines.append("🔥 <b>Burned</b>: nothing yet")
    lb = st.get("last_burn")
    if lb:
        when = f" · {_utc(lb['time'])}" if lb.get("time") else ""
        lines.append(f"Last burn: {tokens(lb.get('amount_tokens'))} $CrawlScan{when} · {tx_link(lb['tx'])}")
    return "\n".join(lines)


# ---------- alerts: /watch, /watchlist, /unwatch ----------
ALERTS_SOON = "🔔 Alerts are coming soon."
WATCH_USAGE = "Usage: /watch &lt;token address&gt;"
UNWATCH_USAGE = "Usage: /unwatch &lt;token address&gt;"
WATCH_WHAT = ("I'll message you when something important changes:\n\n"
              "- the verdict\n- a probably rug warning\n- the biggest operator selling\n- early buyers exiting")
WATCHLIST_EMPTY = "You're not watching any tokens yet."
ASK_WATCH = "Send me the token address to watch."
ASK_WATCH_AGAIN = "That's not a token address. Send me the token address to watch (Robinhood Chain 0x… or Solana)."
NEW_BUTTON = {"text": "➕ New", "callback_data": "new"}


def watch_button(addr):
    """Ряд [Watch] под вердиктом (только личка, алерты включены). callback_data ≤ 64 байт: адрес ≤ 44 символов."""
    return [{"text": "🔔 Watch", "callback_data": f"watch:{addr}"}]


NO_EXPIRY_AFTER = 3650 * 86400   # подписка дольше этого — без срока (Premium, alerts.NO_EXPIRY)


def expires_in(ts, now):
    """Сколько осталось подписке: "6d 23h", "5h", "<1h"; Premium — "no expiry"."""
    s = int(ts - now)
    if s > NO_EXPIRY_AFTER:
        return "no expiry"
    d, h = s // 86400, s // 3600 % 24
    if d:
        return f"{d}d {h}h" if h else f"{d}d"
    return f"{h}h" if h else "<1h"


def _watch_line(w, now):
    left = expires_in(w['expires_at'], now)
    return (f"• <code>{e(w['token'])}</code>\n  {CHAIN_NAME.get(w.get('chain'), e(w.get('chain', '')))} · "
            + (left if left == "no expiry" else f"expires in {left}"))


def watching(r):
    """Ответ /api/alerts/watch (200) → HTML, полный адрес."""
    addr = f"<code>{e(r['token'])}</code>"
    if not r.get("days"):     # Premium: без срока
        head = f"🔔 {'Still watching' if r.get('renewed') else 'Watching'} {addr}, no expiry with Premium."
    else:
        head = (f"🔔 Still watching {addr}, extended to {r['days']} days." if r.get("renewed")
                else f"🔔 Watching {addr} for {r['days']} days.")
    return f"{head}\n\n{WATCH_WHAT}\n\nWatching {len(r.get('items') or [])}/{r['limit']} tokens for now."


def watch_limit(r, now):
    """409: уже limit токенов."""
    return "\n".join([f"You're already watching {r['limit']} tokens, the maximum. /unwatch one first:", ""]
                     + [_watch_line(w, now) for w in r.get("items") or []])


def watch_established(addr):
    return f"🏛 {e(short(addr))} is too established for CrawlScan, so it can't be watched."


def watch_active(addr):
    return f"🌊 {e(short(addr))} has too many trades for a full scan right now, so it can't be watched."


def unwatched(r):
    if r.get("removed"):
        return f"🔕 Stopped watching {e(short(r['token']))}."
    return f"You weren't watching {e(short(r['token']))}. /watchlist shows what you watch."


def days_left(ts, now):
    """Сколько дней осталось подписке, вверх: "7 days left", "1 day left"; меньше часа — "<1 hour left"."""
    s = ts - now
    if s > NO_EXPIRY_AFTER:
        return "⭐ no expiry"
    if s < 3600:
        return "<1 hour left"
    d = -(-int(s) // 86400)
    return f"{d} day{'' if d == 1 else 's'} left"


def watchlist_view(r, now, tickers=None, head=None):
    """Список подписок с кнопками: [Remove …] на каждую (callback rm:<адрес>) и [➕ New] → (html, кнопки).
    Тикер — из ответа /api/alerts/list (последний снимок токена), иначе из tickers — {адрес: тикер} сканов бота."""
    items, tickers = r.get("items") or [], tickers or {}
    if not items:
        return ((head + "\n\n" if head else "") + WATCHLIST_EMPTY), {"inline_keyboard": [[NEW_BUTTON]]}
    lines = [head or f"🔔 <b>Watching {len(items)}/{r['limit']} tokens</b>", ""]
    rows = []
    for i, w in enumerate(items, 1):
        t = w.get("ticker") or tickers.get(w["token"])   # сайт: тикер из последнего снимка; иначе — из сканов бота
        meta = " · ".join(x for x in (f"${e(t)}" if t else None,
                                      CHAIN_NAME.get(w.get("chain"), e(w.get("chain", ""))),
                                      days_left(w["expires_at"], now)) if x)
        lines += [f"{i}. <code>{e(w['token'])}</code>", f"{meta}", ""]
        rows.append([{"text": f"🗑 Remove {i}. " + (f"${t}" if t else short(w["token"])),
                      "callback_data": f"rm:{w['token']}"}])
    rows.append([NEW_BUTTON])
    return "\n".join(lines).rstrip(), {"inline_keyboard": rows}


def watch_limit_view(r, now, tickers=None):
    """409 из [➕ New]: лимит — сообщение и список с кнопками Remove."""
    head = f"You're already watching {r['limit']} tokens, the maximum. Remove one to add another:"
    return watchlist_view(r, now, tickers, head=head)


# ---------- Premium: /verify, /premium, /unlink, /admin_unlink ----------
PREMIUM_BADGE = "⭐ Premium"
ASK_WALLET = "Send me the Robinhood Chain wallet address that holds your $CrawlScan."
ASK_WALLET_AGAIN = "That's not a wallet address. Send me a Robinhood Chain wallet address (0x…)."
WALLET_TAKEN = "This wallet is already linked to another account."
TOO_MANY_VERIFY = "Too many verification attempts. Try again later."
NO_WALLET = "You don't have a linked wallet. Send /verify to link one."
ADMIN_UNLINK_USAGE = "Usage: /admin_unlink &lt;wallet&gt;"
NORMAL_WATCHLIST = "Your watchlist is back to 3 tokens, 7 days each."
BUY_BUTTONS = {"inline_keyboard": [[{"text": "Buy $CrawlScan", "url": BUY_URL}]]}


def _hm(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC")


def _day(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return f"{d:%b} {d.day}, {d:%H:%M} UTC"


def reserved(r):
    """Ответ /api/premium/reserve (200) → HTML."""
    w = f"<code>{e(r['wallet'])}</code>"
    if r.get("state") == "yours":
        return f"✅ {w} is already linked to your account. /premium shows your status."
    if r.get("state") == "pending":
        return (f"⭐ You're already verifying {w}.\n\nBuy any amount of $CrawlScan to this wallet by "
                f"{_hm(r['expires_at'])} to verify it.")
    return (f"⭐ <b>Verify your wallet</b>\n{w}\n\n"
            f"Buy any amount of $CrawlScan to this wallet within {r.get('minutes', 5)} minutes to verify it.\n\n"
            "Only buys count: tokens sent from another wallet don't. "
            f"I'll message you as soon as I see the buy (until {_hm(r['expires_at'])}).")


PREMIUM_START_BUTTON = {"text": "⭐ Premium features", "callback_data": "premium"}


def premium_lines(st):
    """Что даёт Premium и команды. Строки функций, выключенных на сайте (Priority, /picktokens, /digest), не
    показываются."""
    perks = (["Priority scanning — your scans are always first in line"] if st.get("premium_priority") else []) + [
        "Bigger Watchlist — up to 10 tokens with no time limit"]
    if st.get("premium_fresh"):
        perks.append("Fresh scan — rescan any token instantly, skipping the cache")
    if st.get("premium_memory"):
        perks.append("Memory insights — see when top holders are known sniper bots or repeat wallets")
    cmds = [f"/verify — link your wallet: buy any amount of $CrawlScan within {st.get('verify_min') or 5} minutes"]
    if st.get("premium_import"):
        cmds.append("/picktokens — choose tokens from your linked wallet to watch (we only read public balances, "
                    "no keys or wallet connection)")
    if st.get("premium_digest"):
        cmds.append("/digest — a daily morning summary of your Watchlist (on/off)")
    if st.get("premium_devcheck"):
        cmds.append("/dev — see what else this token's dev launched, and which of those ended up in Rug Replay")
    if st.get("premium_trending"):
        cmds.append("/trending — the most scanned tokens on CrawlScan right now")
    cmds.append("/unlink — unlink your wallet")
    return perks + ["", "Commands:"] + cmds


def premium_status_lines(st, now):
    """Статус: Wallet, Balance, Premium. Когда и как часто проверяется баланс — не показываем."""
    res = st.get("reservation")
    if not st.get("linked"):
        out = ["Wallet: not linked", "Premium: ❌ not active"]
        if res:
            out.append(f"Verifying <code>{e(res['wallet'])}</code> until {_hm(res['expires_at'])}: "
                       "buy any amount of $CrawlScan to it.")
        return out
    grace = st.get("grace_until")
    if st.get("premium"):
        prem = "✅ active"
    elif grace:
        prem = f"⏸ paused: your watchlist stays as it is until {_day(grace)}"
    else:
        prem = "❌ not active"
    return [f"Wallet: <code>{e(st['wallet'])}</code>", f"Balance: {st.get('balance_tokens', 0):,} $CrawlScan",
            f"Premium: {prem}"]


def premium_buttons(st, buy_url=None):
    """Кнопки по статусу: не привязан — [Verify wallet]; привязан без премиума — [Buy $CrawlScan] (Trade on Axiom)
    и [Unlink]; премиум — [Pick tokens] [Daily digest: on/off] [Unlink] (Pick tokens и digest — если включены)."""
    unlink = {"text": "Unlink", "callback_data": "unlink"}
    if not st.get("linked"):
        return {"inline_keyboard": [[{"text": "Verify wallet", "callback_data": "verify"}]]}
    if not st.get("premium"):
        return {"inline_keyboard": [[{"text": "Buy $CrawlScan", "url": buy_url or BUY_URL}, unlink]]}
    row = []
    if st.get("premium_import"):
        row.append(PICK_BUTTON)
    if st.get("premium_digest"):
        on = st.get("digest", True)
        row.append({"text": f"Daily digest: {'on' if on else 'off'}", "callback_data": f"digest:{'off' if on else 'on'}"})
    extra = [TRENDING_BUTTON] if st.get("premium_trending") else []
    return {"inline_keyboard": ([row] if row else []) + [extra + [unlink]]}


def premium_view(st, now, buy_url=None):
    """/premium и [⭐ Premium features]: ответ /api/premium/status → (html, кнопки)."""
    need = f"{st.get('min_tokens', 0):,}"
    text = "\n".join([
        "⭐ <b>Premium features for $CrawlScan holders</b>", "",
        f"Hold {need} $CrawlScan in a linked wallet to unlock:",
        *premium_lines(st), "", *premium_status_lines(st, now)])
    return text, premium_buttons(st, buy_url)


UNLINK_CONFIRM = ("Unlink <code>{wallet}</code>? Premium turns off and your watchlist goes back to 3 tokens. "
                  "To link it again you'll need a new $CrawlScan buy.")
UNLINK_BUTTONS = {"inline_keyboard": [[{"text": "Yes, unlink", "callback_data": "unlink:yes"},
                                       {"text": "Cancel", "callback_data": "premium"}]]}


def _removed(tokens):
    return ("\n\nStopped watching:\n" + "\n".join(f"<code>{e(t)}</code>" for t in tokens)) if tokens else ""


def unlinked(r):
    """Ответ /api/premium/unlink → HTML."""
    if not r.get("wallet"):
        return NO_WALLET
    return (f"🔓 Wallet <code>{e(r['wallet'])}</code> unlinked, Premium is off.\n\n{NORMAL_WATCHLIST}"
            + _removed(r.get("removed")))


def admin_unlinked(r):
    """Ответ /api/premium/admin_unlink → HTML."""
    w = f"<code>{e(r['wallet'])}</code>"
    if not r.get("found"):
        return f"No account is linked to {w}."
    return f"🔓 Unlinked {w}. The account was told, {r.get('removed', 0)} watched tokens removed."


# ---------- Premium: Pick tokens — импорт из кошелька (PREMIUM_IMPORT) ----------
PICK_BUTTON = {"text": "Pick tokens", "callback_data": "pick"}
IMPORT_BUTTON = PICK_BUTTON           # прежнее имя
IMPORT_NOT_PREMIUM = "Pick tokens is a Premium feature. /premium shows your status."
IMPORT_EXPIRED = "This list is out of date. Tap Pick tokens again."
IMPORT_EMPTY = ("No supported memecoins (Pons, Flap, Bankr) with a market price in your wallet "
                "<code>{wallet}</code>.")
IMPORT_SKIP = {"established": "too established for CrawlScan, can't be watched",
               "active": "too many trades for a full scan right now, can't be watched"}
PADS = {"pons": "Pons", "flap": "Flap", "bankr": "Bankr"}


def import_wait(seconds):
    m = -(-int(seconds) // 60)
    return f"You can pick tokens once every 10 minutes. Try again in {m} min."


def _tick(it):
    return f"${it['ticker']}" if it.get("ticker") else short(it["token"])


def import_view(r):
    """Ответ /api/premium/import → (html, кнопки): топ-5 мемкоинов кошелька по стоимости; [➕ $TICKER] на каждый,
    который можно добавить (callback ia:<адрес>), и [➕ Add all (N)] (ia:all)."""
    items = r.get("items") or []
    if not items:
        return IMPORT_EMPTY.format(wallet=e(r.get("wallet", ""))), None
    lines = [f"<b>Pick tokens</b> from <code>{e(r['wallet'])}</code>: your top memecoins by value", ""]
    rows, addable = [], 0
    free = max(0, r.get("limit", 10) - r.get("watch_count", 0))
    for i, it in enumerate(items, 1):
        meta = f"{e(_tick(it))} · {PADS.get(it.get('launchpad'), '')} · {_usd(it.get('value_usd') or 0)}"
        lines.append(f"{i}. {meta}")
        if it.get("status") in IMPORT_SKIP:
            lines.append(f"   {'🏛' if it['status'] == 'established' else '🌊'} {IMPORT_SKIP[it['status']]}")
        elif it.get("watching"):
            lines.append("   🔔 already watching")
        else:
            addable += 1
            rows.append([{"text": f"➕ {_tick(it)}", "callback_data": f"ia:{it['token']}"}])
    lines += ["", f"Watchlist: {r.get('watch_count', 0)}/{r.get('limit', 10)}."]
    if addable and addable > free:
        lines.append(f"Only {free} more fit{'s' if free == 1 else ''}: remove a token to add more." if free
                     else "Your watchlist is full: remove a token to add more.")
    if addable > 1:
        rows.append([{"text": f"➕ Add all ({addable})", "callback_data": "ia:all"}])
    return "\n".join(lines), ({"inline_keyboard": rows} if rows else None)


def import_added(r):
    """Ответ /api/premium/import_add → HTML."""
    out = []
    name = lambda t: f"<code>{e(short(t))}</code>"
    if r.get("added"):
        out.append("🔔 Added to your watchlist: " + ", ".join(name(t) for t in r["added"]) + ".")
    if r.get("already"):
        out.append("Already watching: " + ", ".join(name(t) for t in r["already"]) + ".")
    if r.get("full"):
        out.append(f"Your watchlist is full ({r.get('limit', 10)} tokens), not added: "
                   + ", ".join(name(t) for t in r["full"]) + ". Remove a token to add more.")
    for sk in r.get("skipped") or []:
        if sk.get("reason") in IMPORT_SKIP:
            out.append(f"{name(sk['token'])} is {IMPORT_SKIP[sk['reason']]}.")
    if not out:
        out.append("Nothing to add.")
    out.append(f"Watching {r.get('watch_count', 0)}/{r.get('limit', 10)} tokens.")
    return "\n\n".join(out)


# ---------- Premium: утренняя сводка (PREMIUM_DIGEST) ----------
DIGEST_USAGE = "Usage: /digest on or /digest off"


def digest_line(on, hour):
    return (f"Morning digest: on, daily at {hour:02d}:00 UTC (/digest off)" if on
            else "Morning digest: off (/digest on)")


def digest_state(r):
    """Ответ /api/premium/digest → HTML."""
    hour = r.get("hour", 8)
    if r.get("digest"):
        text = (f"☀️ Morning digest is on: every day at {hour:02d}:00 UTC I'll send one message with the verdict "
                "of each watched token and what changed in 24 hours. /digest off stops it.")
        if not r.get("premium"):
            text += "\n\nIt's for Premium holders: it starts when your Premium is active. /premium shows your status."
        return text
    return "Morning digest is off. /digest on turns it back on."


# ---------- Premium: Dev history, Memory, Trending, Fresh scan, лимит сканов ----------
TRENDING_BUTTON = {"text": "Trending", "callback_data": "trending"}
DEV_USAGE = "Usage: /dev &lt;token address&gt;"
DEV_UNKNOWN = ("We haven't recorded a scan of this token yet, so we don't know its dev. Scan it first, "
               "then check again.")
DEV_NONE = "No other tokens from this dev in our records yet."
PREMIUM_ONLY = "This is a Premium feature. /premium shows how to get it."
TRENDING_EMPTY = "No scans in the last hour yet."


def premium_row(addr, dev=False, fresh=False):
    """Ряд под вердиктом для премиума: [Dev history] [Fresh scan] (только включённые функции) или None."""
    row = ([{"text": "Dev history", "callback_data": f"dev:{addr}"}] if dev else []) + (
        [{"text": "Fresh scan", "callback_data": f"fresh:{addr}"}] if fresh else [])
    return row or None


def too_fast(message):
    return f"🕷 {e(message)}"


def _date(ts):
    d = datetime.fromtimestamp(ts, timezone.utc)
    return f"{d:%b} {d.day}"


def _verdict_short(band, score):
    if not band:
        return "no verdict"
    icon = BAND_ICON.get(band, "⚪️")
    return f"{icon} {e(band.replace('_', ' '))}" + (f" {score}" if score is not None else "")


def replay_mark(rep):
    drop, hours = rep.get("drop"), rep.get("hours")
    tail = []
    if drop is not None:
        tail.append(f"fell {drop * 100:.0f}%")
    if hours is not None:
        tail.append(f"within {max(1, round(hours))} hours")
    return "🔴 in Rug Replay" + (f" ({' '.join(tail)})" if tail else "")


def dev_view(r):
    """Ответ /api/premium/dev → HTML."""
    if not r.get("known"):
        return DEV_UNKNOWN
    name = f"${e(r['ticker'])}" if r.get("ticker") else e(short(r["token"]))
    lines = [f"👤 <b>Dev history</b> · {name}", f"Dev: <code>{e(r['dev'])}</code>", ""]
    if not r.get("total"):
        lines.append(DEV_NONE)
        return "\n".join(lines)
    n, m = r["total"], r.get("in_replay", 0)
    lines += [f"This dev launched {n} other token{'' if n == 1 else 's'} we've seen, {m} of them "
              f"{'is' if m == 1 else 'are'} in Rug Replay.", ""]
    for i, t in enumerate(r.get("tokens") or [], 1):
        tick = f"${e(t['ticker'])}" if t.get("ticker") else e(short(t["token"]))
        mcap = f" · mcap {_usd(t['mcap_usd'])} at scan" if t.get("mcap_usd") else ""
        lines.append(f"{i}. {tick} · {_date(t['ts'])} · {_verdict_short(t.get('band'), t.get('score'))}{mcap}")
        if t.get("replay"):
            lines.append(f"   {replay_mark(t['replay'])}")
    more = n - len(r.get("tokens") or [])
    if more > 0:
        lines.append(f"…and {more} more.")
    return "\n".join(lines)


def memory_block(r):
    """Ответ /api/premium/memory → HTML-блок «Memory» под вердиктом или None (ничего примечательного)."""
    if not r or not r.get("notable"):
        return None
    lines = ["🧠 <b>Memory</b>"]
    if r.get("repeat"):
        lines.append(f"• {r['repeat']} of the top {r.get('holders', 20)} holders were in other tokens we scanned")
    for sn in r.get("snipers") or []:
        lines.append(f"• <code>{e(short(sn['wallet']))}</code> — sniper bot, seen in {sn['tokens']} tokens today")
    for c in r.get("clusters") or []:
        toks = c.get("tokens") or []
        names = ", ".join(f"${e(t['ticker'])}" if t.get("ticker") else e(short(t["token"])) for t in toks[:5])
        if len(toks) > 5:
            names += f" +{len(toks) - 5}"
        share = f" ({c['share_supply'] * 100:.1f}% of supply)" if c.get("share_supply") else ""
        rugs = sum(1 for t in toks if t.get("replay"))
        tail = f" — 🔴 {rugs} in Rug Replay" if rugs else ""
        lines.append(f"• {c['wallets']} linked wallets{share} were together in {len(toks)} other "
                     f"token{'' if len(toks) == 1 else 's'}: {names}{tail}")
    return "\n".join(lines)


def trending_view(r):
    """Ответ /api/premium/trending → (html, кнопки [Scan $TICKER] по две в ряд)."""
    items = r.get("items") or []
    if not items:
        return f"🔥 <b>Trending on CrawlScan</b>\n\n{TRENDING_EMPTY}", None
    lines, buttons = [f"🔥 <b>Trending on CrawlScan</b> · last {r.get('window_min', 60)} min", ""], []
    for i, it in enumerate(items, 1):
        tick = f"${e(it['ticker'])}" if it.get("ticker") else e(short(it["token"]))
        verdict = _verdict_short(it.get("band"), it.get("score")) + ("/100" if it.get("score") is not None else "")
        lines.append(f"{i}. {tick} · {verdict} · {it['scans']} scan{'' if it['scans'] == 1 else 's'}")
        label = f"${it['ticker']}" if it.get("ticker") else short(it["token"])
        buttons.append({"text": f"Scan {label}", "callback_data": f"sc:{it['token']}"})
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return "\n".join(lines), {"inline_keyboard": rows}
