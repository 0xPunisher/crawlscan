"""Тексты бота и вид вердикта (HTML parse mode). Чистые функции без сети.
Распознавание адреса — та же логика, что у сайта (engine.chain_of): 0x + 40 hex → Robinhood,
base58 32 байта (32–44 символа) → Solana."""
import html, re

WEBSITE = "https://crawlscan.fun"
OFFICIAL_CA = "0x19dCb63C4d2F29A6f077F094a4f858fC790145e1"
BUY_URL = "https://www.ponsfamily.com/launchpad/" + OFFICIAL_CA

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CHAIN_NAME = {"robinhood": "Robinhood Chain", "solana": "Solana"}
BAND_ICON = {"CLEAN": "🟢", "OK": "🟡", "RISKY": "🟠", "DANGER": "🔴"}
TOO_EARLY = "TOO_EARLY_OR_LATE"


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
    "Every 24 hours one holder wins 10% of the creator fees, paid in $CrawlScan. Every token you hold is a ticket, "
    "so the more you hold, the bigger your chance. Every 12 hours the dev burns tokens. "
    "All verifiable live on the website.\n\n"
    f"CA: <code>{OFFICIAL_CA}</code>"
)
CAPTION_MAX = 1024     # лимит подписи к фото в Telegram (видимый текст, UTF-16)
START_BUTTONS = {"inline_keyboard": [
    [{"text": "Scan a token", "callback_data": "scan"}, {"text": "Help", "callback_data": "help"}],
    [{"text": "Website", "url": WEBSITE}, {"text": "Buy $CrawlScan", "url": BUY_URL}],
]}

HELP = (
    "<b>How to read a verdict</b>\n\n"
    "<b>Score 0–100</b>: 100 = clean. The lower it is, the more the top holders look like a few people.\n"
    "<b>Verdict</b>: 🟢 CLEAN · 🟡 OK · 🟠 RISKY · 🔴 DANGER. ⏳ TOO EARLY OR LATE: too few holders to judge.\n"
    "<b>Operators</b>: real people behind the top holders. Wallets linked by shared buys or transfers "
    "count as one operator.\n"
    "<b>Dump impact</b>: how far the price could drop if the biggest operator sold everything.\n"
    "<b>Virgin wallets</b>: top holders with no trading history before this token, a typical sign "
    "of prepared wallets.\n"
    "<b>Transfer supply</b>: share of the float received by transfer instead of bought.\n"
    "<b>Snipers</b>: wallets that bought right after launch and still hold.\n"
    "<b>Why</b>: rules that forced the verdict down.\n\n"
    "Send a token address to scan it. In groups: /scan &lt;address&gt;."
)

ASK_ADDRESS = "Send me a token address from Robinhood Chain or Solana"
HINT = "Send me a token address from Robinhood Chain (0x…) or Solana. /help explains the verdict."
SCAN_USAGE = "Usage: /scan &lt;token address&gt;"
BUSY = "🕷 Too many scans right now, try again in a minute."
TIMEOUT = "⌛ The scan took too long. Try again in a minute."
UNREACHABLE = "⚠️ CrawlScan is not reachable right now. Try again in a minute."
FAILED = "⚠️ The scan failed. Try again in a minute."


def crawling(addr):
    return f"🕷 crawling {e(short(addr))}…"


def queued(addr):
    return f"⏳ queued {e(short(addr))}…"


def wait(seconds):
    return f"⏳ please wait {seconds} s"


def report_button(addr):
    return {"inline_keyboard": [[{"text": "Full report", "url": f"{WEBSITE}/?ca={addr}"}]]}


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


def verdict(res):
    """Результат скана сайта → HTML-текст вердикта."""
    lines = [title(res), ""]
    holders = len(res.get("holders") or [])
    ops = res.get("operators") or []
    if res.get("band") == TOO_EARLY:
        lines.append("⏳ <b>Too early or too late</b>")
        lines.append(f"Only {res.get('holders_total', 0)} holders, too few to judge.")
        return "\n".join(lines)

    band = res.get("band", "")
    lines.append(f"{BAND_ICON.get(band, '⚪️')} <b>Score {res.get('score')}/100 · {e(band)}</b>")
    lines.append(f"{holders} top holders → {len(ops)} operator{'' if len(ops) == 1 else 's'}")
    m = res.get("metrics") or {}
    if ops and "impact" in m:
        n = len(ops[0].get("wallets") or [])
        who = f"Biggest operator ({n} wallets)" if n > 1 else "Biggest operator"
        lines.append(f"{who} could move price {impact_phrase(m['impact'])} if sold")

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
