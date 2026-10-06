"""Token Burn & Holder Rewards: чистые функции без сети, как detect.py.

Сеть — rewards_service.py (через chains/robinhood.py), база — rewards_store.py. Правила — DEV_NOTES.md,
раздел «Rewards & Burns».

Проверяемый выбор победителя (канонический список, list_hash, r, накопленная сумма) и verify —
общие с солановским розыгрышем: draw.pick / draw.verify, они не зависят от сети.

Расписание — фиксированное время UTC:
  розыгрыш D (D — дата розыгрыша) — в 22:00 UTC суток D, расчёт в 22:05 UTC; сутки для среднего баланса —
  [22:00 UTC D-1, 22:00 UTC D); seed — первый блок с timestamp >= 22:01 UTC D;
  плановые сжигания — 10:00 и 22:00 UTC каждый день (только таймер).

Единицы: балансы и веса — целые базовые единицы токена (raw, с учётом decimals).
Адреса — 0x в нижнем регистре.
"""
from datetime import datetime, timedelta, timezone

import draw as dr

ZERO = "0x" + "0" * 40
DEAD = "0x000000000000000000000000000000000000dead"
BURN_ADDRESS = "0x000000000000000000000000000000000000dEaD"   # адрес сжиганий для показа (чексумма)
BURN_SINKS = {ZERO, DEAD}                                       # нулевой адрес — тоже сжигание
DAY_SECONDS = 86400
DRAW_HOUR = 22                 # розыгрыш и граница суток весов: 22:00 UTC
SEED_DELAY = 60                # seed — первый блок с timestamp >= 22:01 UTC
RUN_DELAY = 300                # расчёт — в 22:05 UTC
BURN_HOURS = (10, 22)          # плановые сжигания: 10:00 и 22:00 UTC

# общие правила выбора (не зависят от сети)
pick, verify, canonical, list_hash, is_day = dr.pick, dr.verify, dr.canonical, dr.list_hash, dr.is_day


def _date(day):
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def day_bounds(day):
    """Сутки весов розыгрыша day в unix-времени: [22:00 UTC day-1, 22:00 UTC day)."""
    end = int(_date(day).replace(hour=DRAW_HOUR).timestamp())
    return end - DAY_SECONDS, end


def draw_at(day):
    """Время розыгрыша day (то, что показывает таймер): 22:00 UTC day = конец суток весов."""
    return day_bounds(day)[1]


def seed_time(day):
    """Seed розыгрыша day: первый блок с timestamp >= 22:01 UTC day."""
    return draw_at(day) + SEED_DELAY


def draw_time(day):
    """Когда розыгрыш day можно считать (и с какого момента ищется выплата): 22:05 UTC day."""
    return draw_at(day) + RUN_DELAY


def day_key(ts):
    """Розыгрыш, в сутки весов которого попадает момент ts: с 22:00 UTC — уже завтрашний."""
    return (datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=24 - DRAW_HOUR)).strftime("%Y-%m-%d")


def prev_day(ts):
    """Последний розыгрыш, чьи сутки весов уже закончились к моменту ts."""
    return (_date(day_key(ts)) - timedelta(days=1)).strftime("%Y-%m-%d")


def next_draw(now):
    """Ближайший розыгрыш > now: (day, время 22:00 UTC)."""
    day = day_key(now)
    return day, draw_at(day)


# ---------- веса ----------
def balances_before(transfers, block):
    """{адрес: баланс} по переводам в блоках < block (переводы в порядке чейна)."""
    bal = {}
    for t in transfers:
        if t["block"] >= block:
            break
        bal[t["frm"]] = bal.get(t["frm"], 0) - t["amount"]
        bal[t["to"]] = bal.get(t["to"], 0) + t["amount"]
    return bal


def time_weights(transfers, start_block, end_block, ts_of, day_start, day_end):
    """Средний по времени баланс каждого адреса за сутки (time-weighted).

    transfers — все переводы токена до конца суток, в порядке чейна (block, log_index);
    start_block / end_block — первый блок с timestamp >= начала / конца суток: переводы в блоках
    < start_block дают баланс на начало суток, в [start_block, end_block) — движения за сутки;
    ts_of — {блок: timestamp} для блоков суток.
    Вес = ∫ баланс dt / длительность суток, целое вниз; нулевые веса отброшены.
    Пример: купил X в 12:00 и держал до конца суток — вес X / 2."""
    span = day_end - day_start
    bal = balances_before(transfers, start_block)
    area, last = {}, {}

    def move(addr, delta, ts):
        area[addr] = area.get(addr, 0) + bal.get(addr, 0) * (ts - last.get(addr, day_start))
        last[addr] = ts
        bal[addr] = bal.get(addr, 0) + delta

    for t in transfers:
        if t["block"] < start_block:
            continue
        if t["block"] >= end_block:
            break
        ts = min(max(ts_of[t["block"]], day_start), day_end)
        move(t["frm"], -t["amount"], ts)
        move(t["to"], t["amount"], ts)
    out = {}
    for a, b in bal.items():
        total = area.get(a, 0) + b * (day_end - last.get(a, day_start))
        w = total // span
        if w > 0:
            out[a] = w
    return out


def eligible(weights, excluded, contracts=()):
    """Участники: ненулевой вес, не в excluded (DEV_WALLETS, инфраструктура, нулевой и dead) и не контракт."""
    skip = {a.lower() for a in excluded} | BURN_SINKS | {a.lower() for a in contracts}
    return {a: w for a, w in weights.items() if w > 0 and a not in skip}


# ---------- сжигания и выплаты ----------
def find_burns(transfers, dev_wallets):
    """Сжигания разработчика: переводы с любого из dev_wallets на нулевой или dead-адрес."""
    devs = {a.lower() for a in dev_wallets}
    return [t for t in transfers if t["frm"] in devs and t["to"] in BURN_SINKS and t["amount"] > 0]


def match_payouts(draws, transfers, dev_wallets, ts_of, used_txs=()):
    """Выплаты: для каждого розыгрыша с победителем и без выплаты — первый перевод токена с любого
    из dev_wallets на кошелёк победителя не раньше расчёта розыгрыша (draw_time, 22:05 UTC). Розыгрыши — по дням
    по порядку; один перевод — выплата только одного розыгрыша (used_txs — уже засчитанные).
    -> {day: перевод}."""
    devs = {a.lower() for a in dev_wallets}
    used = {(tx, li) for tx, li in used_txs}
    out = {}
    for d in sorted(draws, key=lambda d: d["day"]):
        if not d.get("winner") or d.get("payout_tx"):
            continue
        after = draw_time(d["day"])
        for t in transfers:
            key = (t["tx"], t["log_index"])
            if (t["frm"] in devs and t["to"] == d["winner"] and t["amount"] > 0 and key not in used
                    and ts_of.get(t["block"], 0) >= after):
                out[d["day"]] = t
                used.add(key)
                break
    return out


def total_burned(minted, supply, dead_balance):
    """Сожжённый сапплай токена Pons V2 (OpenZeppelin ERC20 + Burnable): burn() шлёт Transfer на 0x0 и
    уменьшает totalSupply; перевод на 0x0 запрещён (revert); перевод на dead оставляет токены в сапплае.
    Поэтому сожжено = выпущено (переводы с 0x0) − totalSupply + баланс dead. balanceOf(0x0) всегда 0."""
    return max(0, int(minted) - int(supply)) + int(dead_balance)


def next_burns(now, n=2):
    """Ближайшие n плановых сжиганий > now (только для таймера): 10:00 и 22:00 UTC каждый день."""
    day0 = int(now) - int(now) % DAY_SECONDS
    out, d = [], day0
    while len(out) < n:
        out += [d + h * 3600 for h in BURN_HOURS if d + h * 3600 > now][:n - len(out)]
        d += DAY_SECONDS
    return out


def next_burn(now):
    return next_burns(now, 1)[0]
