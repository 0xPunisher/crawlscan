"""Token Burn & Holder Rewards: сеть и оркестрация. Чистые правила — rewards.py, база — rewards_store.py.

Сеть — только через chains/robinhood.py (общий лимитер RPS со сканером): история переводов токена,
время блоков, код адресов на блоке, блок по времени (seed), totalSupply и баланс dead.

Настройки (env): REWARDS_ENABLED (true включает, по умолчанию выключено), REWARDS_TOKEN (по умолчанию
токен проекта), DEV_WALLETS (адреса разработчика через запятую), REWARDS_START_DAY (первый розыгрыш, YYYY-MM-DD;
по умолчанию FIRST_DAY), DRAW_DB_PATH (база, общая с розыгрышем Solana).
Расписание фиксированное (UTC), см. rewards.py: розыгрыш 22:00 (расчёт 22:05), сжигания 10:00 и 22:00.
"""
import os, re, sys, threading, time, traceback
from datetime import datetime, timedelta, timezone

import rewards as rw
from chains import priority
from chains import robinhood as ch

DEFAULT_TOKEN = "0x19dCb63C4d2F29A6f077F094a4f858fC790145e1"
FIRST_DAY = "2026-10-06"  # первый розыгрыш: 2026-10-06 22:00 UTC за [2026-10-05 22:00, 2026-10-06 22:00) UTC
CHECK_EVERY = 300       # секунд между проверками сжиганий, выплат и сапплая
TICK = 20               # секунд между проверками планировщика
RETRY_AFTER = 120       # секунд до повтора розыгрыша после ошибки
CONFIRMATIONS = 20      # блоков от головы (~2 с): сжигания и выплаты читаем не по самой голове
CATCH_UP_DAYS = 60      # сколько пропущенных суток планировщик наверстывает
ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")
METHOD = ("weight = time-weighted average balance over the UTC day (integral of balance over time / 86400, floor, "
          "token base units); list_hash = sha256(canonical_list); r = int(sha256(blockhash + list_hash), 16) mod "
          "total_weight (strings, UTF-8); winner = first address, sorted ascending, whose running sum of weights > r. "
          "Day window: 22:00 UTC of the previous day to 22:00 UTC of the draw day. "
          "blockhash: first Robinhood Chain block with timestamp >= seed_time (22:01 UTC of the draw day).")


class NotReady(Exception):
    """Розыгрыш ещё рано проводить (сутки не закончились или seed-блока ещё нет)."""


def enabled():
    return os.environ.get("REWARDS_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def config():
    raw = [a.strip().lower() for a in (os.environ.get("DEV_WALLETS") or "").split(",") if a.strip()]
    start = (os.environ.get("REWARDS_START_DAY") or "").strip()
    return {"enabled": enabled(), "token": ((os.environ.get("REWARDS_TOKEN") or "").strip() or DEFAULT_TOKEN).lower(),
            "dev_wallets": sorted({a for a in raw if ADDR_RE.match(a)}),
            "invalid_dev_wallets": [a for a in raw if not ADDR_RE.match(a)],
            "start_day": start if rw.is_day(start) else FIRST_DAY}


def log(msg):
    print(f"[rewards] {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} {msg}", file=sys.stderr, flush=True)


# ---------- сеть ----------
def launch_info(store, token):
    """{"block", "curve"} запуска токена (Pons V2), с кэшем в базе: запуск не меняется."""
    key = "launch:" + token
    have = store.meta(key) if store else None
    if have:
        return have
    ln = ch.get_launch(token)
    if not ln:
        raise RuntimeError(f"{token} is not a Pons V2 token")
    info = {"block": ln["block"], "curve": ln["curve"]}
    if store:
        store.set_meta(key, info)
    return info


def decimals(store, token):
    key = "decimals:" + token
    have = store.meta(key) if store else None
    if have is None:
        have = ch.token_decimals(token)
        if store:
            store.set_meta(key, have)
    return have


def block_at(ts):
    try:
        return ch.block_at_time(ts)
    except LookupError:
        raise NotReady("no block at this time yet") from None


def safe_head():
    return ch.block_number() - CONFIRMATIONS


def timestamps(transfers, blocks=None):
    """{блок: timestamp} для блоков переводов: из поля ts (blockTimestamp лога), остальные — запросом."""
    want = {t["block"] for t in transfers} if blocks is None else set(blocks)
    out = {t["block"]: t["ts"] for t in transfers if "ts" in t and t["block"] in want}
    if want - out.keys():
        out.update(ch.block_timestamps(want - out.keys()))
    return out


# ---------- розыгрыш ----------
def compute_day(token, day, dev_wallets, launch, dec=None):
    """Розыгрыш day целиком из блокчейна (детерминированно). -> (запись для базы, участники, сводка).
    Блоки суток весов: первый блок с timestamp >= 22:00 UTC day-1 и >= 22:00 UTC day; балансы — из всех
    переводов токена с запуска; контракты — по коду на последнем блоке суток."""
    day_start, day_end = rw.day_bounds(day)
    b_start, b_end = block_at(day_start), block_at(day_end)
    last = b_end["number"] - 1
    transfers = ch.get_token_transfers(token, launch["block"], last)
    day_blocks = {t["block"] for t in transfers if t["block"] >= b_start["number"]}
    ts_of = timestamps(transfers, day_blocks)
    if day_blocks - ts_of.keys():
        raise RuntimeError(f"no timestamp for {len(day_blocks - ts_of.keys())} blocks")
    weights = rw.time_weights(transfers, b_start["number"], b_end["number"], ts_of, day_start, day_end)
    excluded = set(ch.excluded_addresses(launch["curve"])) | set(dev_wallets) | rw.BURN_SINKS
    candidates = [a for a in weights if a not in excluded]
    contracts = {a for a, c in ch.is_contract_at(candidates, last).items() if c} if candidates else set()
    parts = rw.eligible(weights, excluded, contracts)
    seed = block_at(rw.seed_time(day)) if parts else None
    res = rw.pick(parts, seed["hash"] if seed else "")
    rec = {"day": day, "token": token, "status": "done" if res["winner"] else "no_eligible",
           "start_block": b_start["number"], "end_block": b_end["number"], "list_hash": res["list_hash"],
           "seed_time": rw.seed_time(day), "seed_block": seed["number"] if seed else None,
           "blockhash": seed["hash"] if seed else None, "r": res["r"], "winner": res["winner"],
           "winner_weight": res["winner_weight"], "total_weight": res["total"], "participants": res["participants"],
           "decimals": dec, "dev_wallets": ",".join(sorted(dev_wallets)), "created_at": int(time.time())}
    info = {"transfers": len(transfers), "day_transfers": sum(1 for t in transfers if t["block"] >= b_start["number"]),
            "holders": len(weights), "contracts": len(contracts),
            "excluded": len([a for a in weights if a in excluded])}
    return rec, parts, info


def run_draw(store, cfg, day, now=None):
    """Розыгрыш day (идемпотентно: уже есть — возвращает сохранённый). Раньше 22:05 UTC day — NotReady."""
    have = store.get_draw(day)
    if have:
        return have
    if (now or time.time()) < rw.draw_time(day):
        raise NotReady("draw time not reached")
    token = cfg["token"]
    rec, parts, _ = compute_day(token, day, cfg["dev_wallets"], launch_info(store, token), decimals(store, token))
    store.save_draw(rec, parts)
    return store.get_draw(day)


def pending_days(store, cfg, now):
    """Розыгрыши, которым пора (после 22:05 UTC) и которых нет в базе: от первого (REWARDS_START_DAY,
    по умолчанию FIRST_DAY) до последнего закончившегося (не больше CATCH_UP_DAYS)."""
    start = cfg["start_day"] or FIRST_DAY
    last = rw.prev_day(now)
    out, d = [], datetime.strptime(max(start, rw.prev_day(now - CATCH_UP_DAYS * 86400)), "%Y-%m-%d")
    while d.strftime("%Y-%m-%d") <= last:
        day = d.strftime("%Y-%m-%d")
        if now >= rw.draw_time(day) and not store.get_draw(day):
            out.append(day)
        d += timedelta(days=1)
    return out


# ---------- сжигания, выплаты, сапплай ----------
def check_burns(store, cfg):
    """Новые сжигания разработчика: переводы с DEV_WALLETS на 0x0 / dead с последнего прочитанного блока.
    Набор DEV_WALLETS поменялся — сжигания пересобираются с запуска токена. -> сколько новых записано."""
    devs, token = cfg["dev_wallets"], cfg["token"]
    if not devs:
        return 0
    launch = launch_info(store, token)
    key = ",".join(devs)
    if store.meta("burn_wallets:" + token) != key:
        store.clear_burns(token)
        store.set_meta("burn_cursor:" + token, launch["block"] - 1)
        store.set_meta("burn_wallets:" + token, key)
    cursor, head = store.meta("burn_cursor:" + token), safe_head()
    if head <= cursor:
        return 0
    found = rw.find_burns(ch.get_token_transfers(token, cursor + 1, head, frm=devs, to=sorted(rw.BURN_SINKS)), devs)
    ts_of = timestamps(found)
    new = store.save_burns(token, found, ts_of)
    store.set_meta("burn_cursor:" + token, head)
    return new


def check_payouts(store, cfg):
    """Выплаты: первый перевод с DEV_WALLETS победителю после времени розыгрыша — токена (getLogs) или нативного ETH
    (alchemy_getAssetTransfers, category external). -> сколько отмечено."""
    devs, token, unpaid = cfg["dev_wallets"], cfg["token"], store.unpaid_draws()
    if not devs or not unpaid:
        return 0
    head = safe_head()
    lo = min(d["end_block"] for d in unpaid)
    if head < lo:
        return 0
    winners = sorted({d["winner"] for d in unpaid})
    trs = [t | {"currency": rw.CURRENCY_TOKEN} for t in ch.get_token_transfers(token, lo, head, frm=devs, to=winners)]
    trs += [t | {"currency": rw.CURRENCY_ETH} for t in ch.eth_sent(devs, lo, head, to=winners)]
    ts_of = timestamps(trs)
    n = 0
    for day, t in rw.match_payouts(unpaid, trs, devs, ts_of, store.payout_keys()).items():
        n += store.set_payout(day, t, ts_of[t["block"]])
    return n


def refresh_supply(store, cfg):
    """Сожжённый сапплай: выпущено (переводы с 0x0) − totalSupply + баланс dead (см. rewards.total_burned)."""
    token = cfg["token"]
    launch = launch_info(store, token)
    decimals(store, token)   # статусу нужны decimals до первого розыгрыша (суммы *_tokens для фронта)
    minted = sum(t["amount"] for t in ch.get_token_transfers(token, launch["block"], None, frm=rw.ZERO))
    supply, dead = ch.token_supply(token), ch.token_balance(token, rw.DEAD)
    val = {"minted": str(minted), "total_supply": str(supply), "dead_balance": str(dead),
           "burned": str(rw.total_burned(minted, supply, dead)), "updated_at": int(time.time())}
    store.set_meta("supply:" + token, val)
    return val


# ---------- API: форматирование ----------
def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else None


def _tokens(raw, dec):
    return None if raw is None or dec is None else raw / 10 ** dec


def _eth(wei):
    return None if wei is None else wei / 10 ** 18


def payout_fields(row, dec):
    """Валюта и сумма выплаты для API: payout_currency ("ETH" | "CRAWLSCAN"; старые выплаты — токеном),
    payout_eth (ETH) или payout_tokens (токены); у другой валюты — None. Нет выплаты — все None."""
    if not row["payout_tx"]:
        return {"payout_currency": None, "payout_eth": None, "payout_tokens": None}
    cur = row["payout_currency"] or rw.CURRENCY_TOKEN
    eth = cur == rw.CURRENCY_ETH
    return {"payout_currency": cur, "payout_eth": _eth(row["payout_amount"]) if eth else None,
            "payout_tokens": None if eth else _tokens(row["payout_amount"], dec)}


def _chance(w, total):
    return w / total if w and total else None


def draw_json(row):
    if not row:
        return None
    out = dict(row)
    out["dev_wallets"] = [a for a in (row["dev_wallets"] or "").split(",") if a]
    out["status_text"] = "no eligible holders" if row["status"] == "no_eligible" else "winner drawn"
    out["chance"] = _chance(row["winner_weight"], row["total_weight"])
    out["payout_status"] = "paid" if row["payout_tx"] else "pending" if row["winner"] else "no_winner"
    out.update(payout_fields(row, row["decimals"]))
    out["seed_time_iso"], out["draw_time_iso"] = _iso(row["seed_time"]), _iso(rw.draw_at(row["day"]))
    out["period_start_iso"], out["period_end_iso"] = (_iso(x) for x in rw.day_bounds(row["day"]))
    out["paid_at_iso"] = _iso(row["paid_at"])
    return out


def burn_json(row, dec):
    return {"tx": row["tx"], "log_index": row["log_index"], "wallet": row["wallet"], "to": row["to_addr"],
            "amount": row["amount"], "amount_tokens": _tokens(row["amount"], dec), "block": row["block"],
            "time": row["ts"], "time_iso": _iso(row["ts"])}


def status_json(store, cfg, now=None):
    now = now or time.time()
    if not cfg["enabled"] or not store:
        return {"enabled": False}
    token = cfg["token"]
    dec = store.meta("decimals:" + token)
    burned, n_burns = store.burned_total(token)
    last_burn = store.burns(token, 1)
    supply = store.meta("supply:" + token)
    latest = store.latest_draw()
    last = None
    if latest:
        last = {"day": latest["day"], "status": latest["status"], "winner": latest["winner"],
                "weight": latest["winner_weight"], "weight_tokens": _tokens(latest["winner_weight"], dec),
                "total_weight": latest["total_weight"], "chance": _chance(latest["winner_weight"], latest["total_weight"]),
                "participants": latest["participants"], "payout_status": draw_json(latest)["payout_status"],
                "payout_amount": latest["payout_amount"], **payout_fields(latest, dec),
                "payout_tx": latest["payout_tx"]}
    burns = rw.next_burns(now, 2)
    day, at = rw.next_draw(now)
    return {"enabled": True, "chain": "robinhood", "token": token, "decimals": dec, "dev_wallets": cfg["dev_wallets"],
            "now": _iso(now), "next_draw": _iso(at), "next_draw_day": day, "next_draw_runs_at": _iso(rw.draw_time(day)),
            "period_start": _iso(rw.day_bounds(day)[0]), "period_end": _iso(at),
            "next_burn": _iso(burns[0]), "next_burns": [_iso(b) for b in burns],
            "burn_schedule_utc": [f"{h:02d}:00" for h in rw.BURN_HOURS], "draw_schedule_utc": f"{rw.DRAW_HOUR:02d}:00",
            "burn_address": rw.BURN_ADDRESS,
            "last_burn": burn_json(last_burn[0], dec) if last_burn else None,
            "burned_by_dev": {"amount": burned, "amount_tokens": _tokens(burned, dec), "count": n_burns},
            "total_burned": None if not supply else {
                "amount": int(supply["burned"]), "amount_tokens": _tokens(int(supply["burned"]), dec),
                "total_supply": int(supply["total_supply"]), "minted": int(supply["minted"]),
                "dead_balance": int(supply["dead_balance"]), "updated_at": supply["updated_at"],
                "method": "minted - totalSupply() + balanceOf(0x...dead)"},
            "last_draw": last, "participants_last": latest["participants"] if latest else 0,
            "draws_count": store.draws_count()}


def history_json(store, cfg, limit=30):
    dec = store.meta("decimals:" + cfg["token"])
    return {"draws": [draw_json(r) for r in store.draws(limit)],
            "burns": [burn_json(r, dec) for r in store.burns(cfg["token"], limit)]}


PAGE_MAX = 50


def history_page(store, cfg, kind, limit=10, before=None):
    """Страница полной истории для сайта: kind "burns" | "draws", новые первыми, limit 1–50, курсор before — unix-время
    (строго раньше): у сжигания — время блока, у розыгрыша — время розыгрыша (22:00 UTC дня). next_before — курсор
    следующей страницы или None. Только база, без запросов к сети."""
    limit = min(PAGE_MAX, max(1, int(limit)))
    if kind == "burns":
        dec = store.meta("decimals:" + cfg["token"])
        rows, more = store.burns_before(cfg["token"], before, limit)
        items = [burn_json(r, dec) for r in rows]
        total = store.burned_total(cfg["token"])[1]
        nxt = rows[-1]["ts"] if more else None
    else:
        # draw_at(day) < before  <=>  day <= дата (before - 22:00 - 1 с)
        max_day = None if before is None else datetime.fromtimestamp(
            before - rw.DRAW_HOUR * 3600 - 1, timezone.utc).date().isoformat()
        rows = store.draws_before(max_day, limit)
        more, rows = len(rows) > limit, rows[:limit]
        items = [dict(draw_json(r), time=rw.draw_at(r["day"])) for r in rows]
        total = store.draws_count()
        nxt = items[-1]["time"] if more else None
    return {"kind": kind, "limit": limit, "before": before, "total": total, "items": items, "next_before": nxt}


def participants_json(store, day):
    row = store.get_draw(day)
    if not row:
        return None
    parts = store.participants(day)
    return {"day": day, "list_hash": row["list_hash"], "total_weight": row["total_weight"], "decimals": row["decimals"],
            "participants": [{"address": a, "weight": w} for a, w in sorted(parts.items())]}


def verify_json(store, day):
    row = store.get_draw(day)
    if not row:
        return None
    parts = store.participants(day)
    v = rw.verify(row, parts)
    return {"day": day, "ok": v["ok"], "checks": v["checks"],
            "inputs": {"list_hash": row["list_hash"], "canonical_list": rw.canonical(parts),
                       "seed_time": row["seed_time"], "seed_block": row["seed_block"], "blockhash": row["blockhash"],
                       "total_weight": row["total_weight"], "start_block": row["start_block"],
                       "end_block": row["end_block"], "dev_wallets": draw_json(row)["dev_wallets"]},
            "recomputed": {k: v["recomputed"][k] for k in ("list_hash", "total", "r", "winner", "winner_weight")},
            "stored": {"r": row["r"], "winner": row["winner"]}, "method": METHOD}


# ---------- планировщик ----------
class Scheduler(threading.Thread):
    """Фоновый поток: розыгрыш в 22:05 UTC за сутки до 22:00 UTC (наверстывает пропущенные), каждые 5 минут —
    сжигания, выплаты и сожжённый сапплай. Ошибки логируются и повторяются позже; поток не падает и не
    роняет сервер; сканер не затрагивается (общий только лимитер RPS адаптера)."""

    def __init__(self, store, cfg):
        super().__init__(daemon=True, name="rewards-scheduler")
        self.store, self.cfg = store, cfg
        self._stop = threading.Event()
        self._retry = {}
        self._next_check = 0

    def stop(self):
        self._stop.set()

    def tick(self, now=None):
        now = now or time.time()
        for day in pending_days(self.store, self.cfg, now):
            if now < self._retry.get(day, 0):
                continue
            try:
                row = run_draw(self.store, self.cfg, day, now)
                log(f"draw {day}: {row['status']}, {row['participants']} participants, winner {row['winner']}")
            except NotReady:
                self._retry[day] = now + 60
            except Exception as e:
                self._retry[day] = now + RETRY_AFTER
                log(f"draw {day} failed: {e}")
        if now >= self._next_check:
            self._next_check = now + CHECK_EVERY
            for name, fn in (("burns", check_burns), ("payouts", check_payouts), ("supply", refresh_supply)):
                try:
                    n = fn(self.store, self.cfg)
                    if name != "supply" and n:
                        log(f"{name}: {n} new")
                except Exception as e:
                    log(f"{name} check failed: {e}")

    def run(self):
        log(f"scheduler started for {self.cfg['token']}, dev wallets: {len(self.cfg['dev_wallets'])}")
        if self.cfg["invalid_dev_wallets"]:
            log(f"ignored invalid DEV_WALLETS entries: {len(self.cfg['invalid_dev_wallets'])}")
        while not self._stop.is_set():
            try:
                with priority.background():   # уступает живым сканам, свой лимит BACKGROUND_RPS
                    self.tick()
            except Exception:
                log("tick failed: " + traceback.format_exc(limit=3).replace("\n", " | "))
            self._stop.wait(TICK)
