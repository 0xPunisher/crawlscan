"""Token Burn & Holder Rewards: чистые функции без сети, как detect.py.

Сеть — rewards_service.py (через chains/robinhood.py), база — rewards_store.py. Правила — DEV_NOTES.md,
раздел «Rewards & Burns».

Проверяемый выбор победителя (канонический список, list_hash, r, накопленная сумма) и verify —
общие с солановским розыгрышем: draw.pick / draw.verify, они не зависят от сети. Время розыгрыша и
seed — тоже как там: розыгрыш суток D в 00:05 UTC суток D+1, seed — первый блок с timestamp >= 00:01 UTC D+1.

Единицы: балансы и веса — целые базовые единицы токена (raw, с учётом decimals).
Адреса — 0x в нижнем регистре.
"""
from datetime import datetime, timedelta, timezone

import draw as dr

ZERO = "0x" + "0" * 40
DEAD = "0x000000000000000000000000000000000000dead"
BURN_SINKS = {ZERO, DEAD}
DAY_SECONDS = 86400

# общие правила выбора и времени (не зависят от сети)
pick, verify, canonical, list_hash = dr.pick, dr.verify, dr.canonical, dr.list_hash
draw_time, seed_time, day_key, is_day = dr.draw_time, dr.seed_time, dr.day_key, dr.is_day


def day_bounds(day):
    """(начало, конец) UTC-суток day в unix-времени: [00:00 D, 00:00 D+1)."""
    start = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    return start, start + DAY_SECONDS


def prev_day(ts):
    """Прошедшие UTC-сутки относительно ts."""
    return (datetime.fromtimestamp(ts, timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")


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
    из dev_wallets на кошелёк победителя не раньше времени розыгрыша (draw_time). Розыгрыши — по дням
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


def next_burn(now, interval_hours=12):
    """Следующее плановое сжигание (только для таймера): ближайший момент > now на сетке
    00:00 UTC + k * interval_hours."""
    step = int(interval_hours * 3600)
    day0 = int(now) - int(now) % DAY_SECONDS
    k = (int(now) - day0) // step + 1
    return day0 + k * step
