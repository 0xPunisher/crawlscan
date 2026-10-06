"""Розыгрыш среди холдеров: чистые функции без сети, как detect.py.

Сеть и база — в draw_service.py и draw_store.py. Правила — DEV_NOTES.md, раздел «Draw».
Проверяемый выбор (canonical, list_hash, pick, verify) и время розыгрыша не зависят от сети:
их же использует розыгрыш Robinhood-токена (rewards.py).

Единицы: балансы и веса — целые базовые единицы токена (raw, с учётом decimals).
День розыгрыша `day` (YYYY-MM-DD) — UTC-сутки, за которые считаются веса; сам розыгрыш идёт
на следующие сутки в 00:05 UTC, seed — первый слот с blockTime >= 00:01 UTC следующих суток.
"""
import hashlib, json, math
from datetime import datetime, timedelta, timezone

SYSTEM_PROGRAM = "11111111111111111111111111111111"
DRAW_AT = (0, 5)      # розыгрыш в 00:05 UTC следующих суток
SEED_AT = (0, 1)      # seed-слот: первый слот с blockTime >= 00:01 UTC следующих суток


# ---------- время ----------
def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def hour_key(ts):
    """Ключ часового снимка: 'YYYY-MM-DDTHH' (UTC)."""
    return utc(ts).strftime("%Y-%m-%dT%H")


def day_key(ts):
    return utc(ts).strftime("%Y-%m-%d")


def _next_day(day, hm):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    return int(d.replace(hour=hm[0], minute=hm[1]).timestamp())


def draw_time(day):
    """Unix-время розыгрыша за сутки day: 00:05 UTC следующих суток."""
    return _next_day(day, DRAW_AT)


def seed_time(day):
    """Unix-время seed: 00:01 UTC следующих суток (blockhash первого слота не раньше)."""
    return _next_day(day, SEED_AT)


def is_day(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d") == s
    except (TypeError, ValueError):
        return False


# ---------- снимок ----------
def aggregate(token_accounts):
    """Токен-аккаунты [{owner, amount}] -> {владелец: суммарный баланс}, только ненулевые."""
    out = {}
    for a in token_accounts:
        amt = int(a["amount"])
        if amt > 0:
            out[a["owner"]] = out.get(a["owner"], 0) + amt
    return out


def is_wallet(owner_program, on_curve):
    """Обычный кошелёк: точка на кривой ed25519 (не PDA) и аккаунт владельца принадлежит
    System Program. Аккаунта нет (кошелёк без SOL, owner_program None) — тоже кошелёк, если на кривой.
    Пулы, кривые, PDA и контракты (программы, их аккаунты) не участвуют."""
    return bool(on_curve) and owner_program in (SYSTEM_PROGRAM, None)


def eligible_balances(balances, wallets):
    """Балансы только обычных кошельков. wallets — множество владельцев, признанных кошельками."""
    return {o: v for o, v in balances.items() if o in wallets}


# ---------- веса и порог ----------
def day_weights(snapshots):
    """snapshots — балансы кошельков по сделанным снимкам суток [{owner: balance}, ...].
    Вес = средний баланс по сделанным снимкам: нет кошелька в снимке — 0 за этот снимок;
    пропущенные из-за простоя снимки в знаменатель не входят. Целое (вниз), нулевые веса отброшены."""
    n = len(snapshots)
    if not n:
        return {}
    total = {}
    for snap in snapshots:
        for o, v in snap.items():
            total[o] = total.get(o, 0) + int(v)
    return {o: s // n for o, s in total.items() if s // n > 0}


def threshold(decimals, price_usd=None, min_usd=10.0, min_tokens=None):
    """Порог среднего баланса в базовых единицах: {"mode", "min_raw", "price_usd", "min_usd", "min_tokens"}.
    Есть цена — min_usd по цене (вверх до целой единицы); нет цены — min_tokens; нет и его — без порога."""
    scale = 10 ** int(decimals)
    if price_usd and price_usd > 0 and min_usd is not None:
        raw = math.ceil(min_usd / price_usd * scale)
        return {"mode": "usd", "min_raw": raw, "price_usd": price_usd, "min_usd": min_usd, "min_tokens": None}
    if min_tokens is not None:
        return {"mode": "tokens", "min_raw": math.ceil(float(min_tokens) * scale), "price_usd": None,
                "min_usd": None, "min_tokens": min_tokens}
    return {"mode": "none", "min_raw": 0, "price_usd": None, "min_usd": None, "min_tokens": None}


def apply_threshold(weights, min_raw):
    return {o: w for o, w in weights.items() if w >= min_raw and w > 0}


# ---------- проверяемый выбор ----------
def canonical(participants):
    """Канонический JSON списка участников: [[адрес, вес], ...], сортировка по адресу, без пробелов."""
    rows = sorted([str(a), int(w)] for a, w in dict(participants).items())
    return json.dumps(rows, separators=(",", ":"))


def list_hash(participants):
    return hashlib.sha256(canonical(participants).encode()).hexdigest()


def pick(participants, blockhash):
    """Победитель: r = int(sha256(blockhash + list_hash), 16) mod сумма_весов (строки, UTF-8);
    идём по участникам в порядке сортировки адресов, победитель — первый, у кого накопленная
    сумма весов > r. Нет участников — winner None.
    -> {"list_hash", "total", "r", "winner", "winner_weight", "participants"}"""
    lh = list_hash(participants)
    rows = sorted((str(a), int(w)) for a, w in dict(participants).items())
    total = sum(w for _, w in rows)
    if not rows or total <= 0:
        return {"list_hash": lh, "total": 0, "r": None, "winner": None, "winner_weight": None, "participants": 0}
    r = int(hashlib.sha256((blockhash + lh).encode()).hexdigest(), 16) % total
    acc = 0
    for a, w in rows:
        acc += w
        if acc > r:
            return {"list_hash": lh, "total": total, "r": r, "winner": a, "winner_weight": w, "participants": len(rows)}
    raise AssertionError("unreachable: r < total")


def verify(record, participants):
    """Пересчёт розыгрыша по сохранённым данным. record — {"list_hash", "blockhash", "winner",
    "total_weight", "r"}; participants — {адрес: вес}. -> {"ok", "checks", "recomputed"}."""
    re = pick(participants, record.get("blockhash") or "")
    checks = {
        "list_hash": re["list_hash"] == record.get("list_hash"),
        "total_weight": re["total"] == int(record.get("total_weight") or 0),
        "r": (re["r"] is None and record.get("r") is None) or (record.get("r") is not None and re["r"] == int(record["r"])),
        "winner": re["winner"] == record.get("winner"),
    }
    return {"ok": all(checks.values()), "checks": checks, "recomputed": re}
