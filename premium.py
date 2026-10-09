"""Premium для холдеров $CrawlScan: правила и тексты. Чистые функции без сети и базы.

Всё за выключателем PREMIUM_ENABLED (по умолчанию выключено). Верификация — только покупкой: пользователь бронирует
кошелёк в боте (/verify) на RESERVE_MIN минут за свой Telegram ID, и если за это время на кошелёк пришёл $CrawlScan
от пула или известного роутера (событие Transfer токена), кошелёк привязан. Кто первый, того и кошелёк: один
кошелёк — один Telegram-аккаунт, один аккаунт — один кошелёк.
Premium = баланс привязанного кошелька ≥ PREMIUM_MIN_TOKENS (проверка при верификации и раз в сутки). Ниже порога —
GRACE_DAYS дней Watchlist остаётся как был, потом обрезается до обычного (alerts.WATCH_LIMIT, WATCH_DAYS).
Хранение — premium_store.py (своя база premium.db), сеть — premium_service.py, API — server.py (/api/premium/*).
"""
import collections, html, os, re, threading

import alerts
from draw_store import DEFAULT_PATH as DRAW_DEFAULT_PATH

DEFAULT_TOKEN = "0x19dcb63c4d2f29a6f077f094a4f858fc790145e1"   # $CrawlScan (как REWARDS_TOKEN)
MIN_TOKENS = 500_000         # по умолчанию PREMIUM_MIN_TOKENS
RESERVE_MIN = 15             # минут брони кошелька
WATCH_LIMIT = 10             # токенов в Watchlist у премиума (обычные — alerts.WATCH_LIMIT)
GRACE_DAYS = 7               # дней после падения баланса ниже порога, пока Watchlist остаётся премиальным
CHECK_EVERY = 86400          # секунд между проверками баланса
ATTEMPTS_PER_HOUR = 5        # броней в час на одного пользователя
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def enabled():
    return os.environ.get("PREMIUM_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def min_tokens():
    """PREMIUM_MIN_TOKENS (целые токены); пусто или неверно — MIN_TOKENS."""
    try:
        v = int(os.environ.get("PREMIUM_MIN_TOKENS", "").strip().replace("_", ""))
        return v if v > 0 else MIN_TOKENS
    except ValueError:
        return MIN_TOKENS


def token():
    return ((os.environ.get("REWARDS_TOKEN") or "").strip() or DEFAULT_TOKEN).lower()


def db_path():
    """PREMIUM_DB_PATH, по умолчанию premium.db в папке DRAW_DB_PATH (на проде — том /data). Не draw.db."""
    p = os.environ.get("PREMIUM_DB_PATH", "").strip()
    if p:
        return p
    draw = os.environ.get("DRAW_DB_PATH", "").strip() or DRAW_DEFAULT_PATH
    return os.path.join(os.path.dirname(os.path.abspath(draw)), "premium.db")


def wallet_of(s):
    """Адрес кошелька Robinhood Chain (нижний регистр) или None."""
    s = (s or "").strip() if isinstance(s, str) else ""
    return s.lower() if ADDR_RE.match(s) else None


def in_grace(link, now):
    """Баланс ниже порога, но GRACE_DAYS ещё не прошли (Watchlist остаётся премиальным)."""
    return bool(link and link.get("below_since") and now < link["below_since"] + GRACE_DAYS * 86400)


def watch_terms(link, now):
    """(лимит, дней) подписок: премиум или его последние GRACE_DAYS — (10, None: без срока), иначе обычные."""
    if link and (link.get("premium") or in_grace(link, now)):
        return WATCH_LIMIT, None
    return alerts.WATCH_LIMIT, alerts.WATCH_DAYS


def counts_as_buy(transfer, reservation, senders):
    """Перевод токена засчитывает бронь: пришёл на забронированный кошелёк от пула / роутера (senders), сумма > 0,
    время блока внутри брони [created_at, expires_at]. С обычного кошелька — не покупка."""
    ts = transfer.get("ts")
    return (transfer["to"] == reservation["wallet"] and transfer["frm"] in senders and transfer.get("amount", 0) > 0
            and ts is not None and reservation["created_at"] <= ts <= reservation["expires_at"])


def balance_event(link, balance, threshold, now):
    """Плановая проверка баланса → (поля для записи, событие | None). События: "on" (стал ≥ порога),
    "paused" (премиум был и баланс упал: начались GRACE_DAYS), "trim" (GRACE_DAYS прошли — Watchlist обрезать)."""
    if balance >= threshold:
        if link.get("premium"):
            return {}, None
        return {"premium": 1, "below_since": None, "downgraded": 0}, "on"
    if link.get("premium"):
        return {"premium": 0, "below_since": now, "downgraded": 0}, "paused"
    if not link.get("downgraded") and not in_grace(link, now):
        return {"downgraded": 1}, "trim"
    return {}, None


class Attempts:
    """Лимит броней на пользователя: не больше n за скользящий час (в памяти процесса)."""

    def __init__(self, n=ATTEMPTS_PER_HOUR, window=3600):
        self.n, self.window, self.lock, self.seen = n, window, threading.Lock(), {}

    def take(self, user_id, now):
        with self.lock:
            q = self.seen.setdefault(user_id, collections.deque())
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.n:
                return False
            q.append(now)
            if len(self.seen) > 10000:
                self.seen = {k: v for k, v in self.seen.items() if v and now - v[-1] < self.window}
            return True


# ---------- тексты сообщений, которые шлёт сайт (HTML) ----------

def amount(raw, decimals):
    """Сырой баланс → "1,234,567" (целые токены, вниз)."""
    return f"{int(raw) // 10 ** decimals:,}"


def short(addr):
    return f"{addr[:6]}…{addr[-4:]}"


def _code(s):
    return f"<code>{html.escape(s)}</code>"


PERKS = ("- Watchlist up to 10 tokens, no 7-day limit\n- Premium badge on your scans\n- /premium shows your status")
NORMAL_WATCHLIST = f"Your watchlist is back to {alerts.WATCH_LIMIT} tokens, {alerts.WATCH_DAYS} days each."


def removed_lines(tokens):
    return ("\n\nStopped watching:\n" + "\n".join(_code(t) for t in tokens)) if tokens else ""


def verified_message(wallet, balance, decimals, threshold, old_wallet=None):
    if balance >= threshold * 10 ** decimals:
        text = (f"✅ Wallet verified: {_code(wallet)}\n\n⭐ <b>Premium is on.</b> This wallet holds "
                f"{amount(balance, decimals)} $CrawlScan (minimum {threshold:,}).\n\n{PERKS}\n\n"
                "I check the balance once a day.")
    else:
        text = (f"✅ Wallet verified: {_code(wallet)}\n\nPremium needs at least {threshold:,} $CrawlScan in this "
                f"wallet; it holds {amount(balance, decimals)} now. I check the balance once a day, and Premium "
                "turns on when it's enough.")
    if old_wallet and old_wallet != wallet:
        text += f"\n\nYour previous wallet {_code(old_wallet)} is no longer linked."
    return text


def expired_message(wallet):
    return (f"⌛ Verification expired: no $CrawlScan buy to {_code(wallet)} in {RESERVE_MIN} minutes. "
            "Send /verify to try again.")


def paused_message(wallet, balance, decimals, threshold):
    return (f"⭐ Premium is paused: {_code(wallet)} holds {amount(balance, decimals)} $CrawlScan, "
            f"below {threshold:,}.\n\nYour watchlist stays as it is for {GRACE_DAYS} days. If the balance is still "
            f"below by then, it goes back to {alerts.WATCH_LIMIT} tokens, {alerts.WATCH_DAYS} days each. "
            "Top up the wallet and Premium comes back at the next daily check.")


def on_message(wallet, balance, decimals):
    return f"⭐ Premium is on: {_code(wallet)} holds {amount(balance, decimals)} $CrawlScan.\n\n{PERKS}"


def trim_message(wallet, threshold, removed):
    return (f"Premium ended: {_code(wallet)} still holds less than {threshold:,} $CrawlScan.\n\n"
            f"{NORMAL_WATCHLIST}{removed_lines(removed)}")


def admin_unlinked_message(wallet, removed):
    return (f"🔓 Your wallet {_code(wallet)} was unlinked by CrawlScan, so Premium is off. "
            f"Send /verify to link a wallet you own.\n\n{NORMAL_WATCHLIST}{removed_lines(removed)}")


def log_user(user_id):
    """Telegram ID в логе — только последние 4 цифры."""
    return "user …" + str(user_id)[-4:]
