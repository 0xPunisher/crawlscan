"""Розыгрыш: сеть и оркестрация. Чистые правила — draw.py, база — draw_store.py.

Сеть:
  - снимок холдеров — Helius DAS getTokenAccounts по mint (HELIUS_RPC), страницы по 1000, курсор;
  - владельцы, decimals, слоты и blockhash — SOLANA_RPC через chains/solana.py (rpc, is_pda);
  - цена для порога — GeckoTerminal (market.fetch_market, сеть solana).

Настройки (env): DRAW_ENABLED (true включает, по умолчанию выключено), DRAW_MINT, HELIUS_RPC,
DRAW_MIN_USD (по умолчанию 10), DRAW_MIN_TOKENS (порог в токенах, если нет цены), DRAW_DB_PATH.
"""
import json, os, sys, threading, time, traceback, urllib.error, urllib.request
from datetime import datetime, timezone

import draw as dr
import market
from chains import solana as sol

DAS_GAP = 0.55          # Helius free: DAS 2 запроса/с
DAS_PAGE = 1000
SLOT_SECONDS = 0.4      # оценка длительности слота для поиска seed-слота
TICK = 20               # секунд между проверками планировщика
RETRY_AFTER = 120       # секунд до повтора после ошибки снимка/розыгрыша


class NotReady(Exception):
    """Розыгрыш ещё рано проводить (seed-слот в будущем)."""


def enabled():
    return os.environ.get("DRAW_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def config():
    def num(name):
        v = (os.environ.get(name) or "").strip()
        return float(v) if v else None
    min_usd = num("DRAW_MIN_USD")
    return {"enabled": enabled(), "mint": (os.environ.get("DRAW_MINT") or "").strip(),
            "min_usd": 10.0 if min_usd is None else min_usd, "min_tokens": num("DRAW_MIN_TOKENS")}


def log(msg):
    print(f"[draw] {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} {msg}", file=sys.stderr, flush=True)


# ---------- сеть ----------
def _helius(method, params, tries=5):
    url = os.environ.get("HELIUS_RPC")
    if not url:
        raise RuntimeError("HELIUS_RPC is not set")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for a in range(tries):
        req = urllib.request.Request(url, data=body, headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503) and a < tries - 1:
                time.sleep(1.0 * (a + 1)); continue
            raise RuntimeError(f"helius {method}: HTTP {e.code}") from None   # без URL: в нём ключ
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if a < tries - 1:
                time.sleep(1.0 * (a + 1)); continue
            raise RuntimeError(f"helius {method}: {type(e).__name__}") from None
        err = d.get("error")
        if err:
            if err.get("code") == 429 and a < tries - 1:
                time.sleep(1.0 * (a + 1)); continue
            raise RuntimeError(f"helius {method}: {err.get('message', err)}")
        return d["result"]
    raise RuntimeError(f"helius {method}: rate limited")


def fetch_token_accounts(mint):
    """Все ненулевые токен-аккаунты mint: ([{owner, amount}], last_indexed_slot)."""
    out, cursor, slot = [], None, None
    while True:
        params = {"mint": mint, "limit": DAS_PAGE, "options": {"showZeroBalance": False}}
        if cursor:
            params["cursor"] = cursor
        res = _helius("getTokenAccounts", params)
        page = res.get("token_accounts") or []
        out += [{"owner": a["owner"], "amount": int(a["amount"])} for a in page]
        slot = res.get("last_indexed_slot", slot)
        cursor = res.get("cursor")
        if not page or not cursor or len(page) < DAS_PAGE:
            return out, slot
        time.sleep(DAS_GAP)


def owner_programs(addresses):
    """{адрес: программа-владелец аккаунта | None (аккаунта нет)} батчами getMultipleAccounts по 100."""
    addrs, out = list(addresses), {}
    for i in range(0, len(addrs), 100):
        part = addrs[i:i + 100]
        res = sol.rpc("getMultipleAccounts", [part, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])["value"]
        out.update({a: (acc or {}).get("owner") for a, acc in zip(part, res)})
    return out


def token_decimals(mint):
    return int(sol.rpc("getTokenSupply", [mint])["value"]["decimals"])


def token_price(mint):
    return market.fetch_market(mint, "solana").get("price_usd")


def seed_block(ts):
    """Первый подтверждённый слот с blockTime >= ts: {"slot", "blockhash", "block_time"}.
    Оценка по текущему слоту (~0.4 с/слот), затем бинарный поиск по getBlocksWithLimit + getBlockTime
    (пропущенные слоты обходятся). ts в будущем или блок ещё не финализирован — NotReady."""
    fin = {"commitment": "finalized"}
    cur = sol.rpc("getSlot", [fin])
    cur_t = sol.rpc("getBlockTime", [cur])
    if cur_t < ts:
        raise NotReady("seed time is in the future")

    def at(slot):   # (подтверждённый слот >= slot, его время) или (None, None)
        b = sol.rpc("getBlocksWithLimit", [slot, 1, fin])
        return (b[0], sol.rpc("getBlockTime", [b[0]])) if b else (None, None)

    lo = max(0, cur - int((cur_t - ts) / SLOT_SECONDS) - 1500)
    s, t = at(lo)
    while t is not None and t >= ts:        # оценка позже ts — шагаем назад
        lo = max(0, lo - 5000)
        s, t = at(lo)
    hi = cur                                # инвариант: время(lo) < ts <= время(hi)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        s, t = at(mid)
        if s is None or s >= hi:
            hi = mid
        elif t < ts:
            lo = s
        else:
            hi = s
    blk = sol.rpc("getBlock", [hi, {"transactionDetails": "none", "rewards": False,
                                    "maxSupportedTransactionVersion": 1, "commitment": "finalized"}])
    return {"slot": hi, "blockhash": blk["blockhash"], "block_time": blk["blockTime"]}


# ---------- снимок ----------
def classify_owners(store, owners):
    """Множество владельцев — обычных кошельков (draw.is_wallet). Проверка через getMultipleAccounts, с кэшем в базе."""
    owners = set(owners)
    cached = store.owners_cached(owners) if store else {}
    need = [o for o in owners if o not in cached]
    if need:
        progs = owner_programs(need)
        rows = [(o, progs.get(o), dr.is_wallet(progs.get(o), not sol.is_pda(o))) for o in need]
        if store:
            store.save_owners(rows)
        cached.update({o: w for o, _, w in rows})
    return {o for o in owners if cached.get(o)}


def take_snapshot(store, mint, now=None, dry_run=False):
    """Снимок холдеров для часа now. Уже есть в базе — не повторяется (идемпотентно).
    -> {"hour", "day", "taken", "holders", "wallets", "balances"} (balances — только кошельки)."""
    now = now or time.time()
    hour, day = dr.hour_key(now), dr.day_key(now)
    if store and not dry_run and store.has_snapshot(hour):
        return {"hour": hour, "day": day, "taken": False}
    accounts, slot = fetch_token_accounts(mint)
    balances = dr.aggregate(accounts)
    wallets = classify_owners(None if dry_run else store, balances)
    eligible = dr.eligible_balances(balances, wallets)
    if store and not dry_run:
        store.save_snapshot(hour, day, mint, slot, len(balances), eligible, taken_at=now)
    return {"hour": hour, "day": day, "taken": True, "slot": slot, "holders": len(balances),
            "wallets": len(eligible), "balances": eligible}


# ---------- розыгрыш ----------
def decide(day, mint, snapshots, cfg, decimals, price, seed=None):
    """Розыгрыш суток по снимкам: (запись для базы, участники). seed — функция ts -> seed_block (сеть)."""
    weights = dr.day_weights(snapshots)
    thr = dr.threshold(decimals, price, cfg.get("min_usd"), cfg.get("min_tokens"))
    parts = dr.apply_threshold(weights, thr["min_raw"])
    sb = (seed or seed_block)(dr.seed_time(day)) if parts else {"slot": None, "blockhash": None}
    res = dr.pick(parts, sb["blockhash"] or "")
    rec = {"day": day, "mint": mint, "status": "done" if res["winner"] else "no_eligible", "snapshots": len(snapshots),
           "list_hash": res["list_hash"], "seed_time": dr.seed_time(day), "seed_slot": sb["slot"], "blockhash": sb["blockhash"],
           "r": res["r"], "winner": res["winner"], "winner_weight": res["winner_weight"], "total_weight": res["total"],
           "participants": res["participants"], "decimals": decimals, "price_usd": thr["price_usd"],
           "threshold_mode": thr["mode"], "threshold_raw": thr["min_raw"], "created_at": int(time.time())}
    return rec, parts


def run_draw(store, mint, day, cfg, now=None):
    """Розыгрыш за сутки day (идемпотентно: уже проведён — возвращает сохранённый).
    None — снимков за сутки нет. NotReady — ещё рано (до 00:05 UTC следующих суток или seed в будущем)."""
    have = store.get_draw(day)
    if have:
        return have
    if (now or time.time()) < dr.draw_time(day):
        raise NotReady("draw time not reached")
    snaps = store.snapshots_of_day(day, mint)
    if not snaps:
        return None
    rec, parts = decide(day, mint, snaps, cfg, token_decimals(mint), token_price(mint))
    store.save_draw(rec, parts)
    return store.get_draw(day)


# ---------- API: форматирование ----------
def draw_json(row, with_participants=None):
    if not row:
        return None
    ints = ("r", "winner_weight", "total_weight", "threshold_raw")
    out = {k: (int(v) if k in ints and v is not None else v) for k, v in row.items()}
    out["status_text"] = "no eligible holders" if row["status"] == "no_eligible" else "winner drawn"
    out["seed_time_iso"] = datetime.fromtimestamp(row["seed_time"], timezone.utc).isoformat()
    if with_participants is not None:
        out["participants_list"] = [{"address": a, "weight": w} for a, w in sorted(with_participants.items())]
    return out


def verify_json(store, day):
    row = store.get_draw(day)
    if not row:
        return None
    parts = store.participants(day)
    v = dr.verify(row, parts)
    return {"day": day, "ok": v["ok"], "checks": v["checks"],
            "inputs": {"list_hash": row["list_hash"], "canonical_list": dr.canonical(parts),
                       "seed_time": row["seed_time"], "seed_slot": row["seed_slot"], "blockhash": row["blockhash"],
                       "total_weight": int(row["total_weight"])},
            "recomputed": {k: v["recomputed"][k] for k in ("list_hash", "total", "r", "winner", "winner_weight")},
            "stored": {"r": int(row["r"]) if row["r"] is not None else None, "winner": row["winner"]},
            "method": ("list_hash = sha256(canonical_list); r = int(sha256(blockhash + list_hash), 16) mod total_weight "
                       "(strings, UTF-8); winner = first address, sorted ascending, whose running sum of weights > r. "
                       "blockhash: first Solana slot with blockTime >= seed_time.")}


def status_json(store, cfg, now=None):
    now = now or time.time()
    out = {"enabled": cfg["enabled"], "mint": cfg["mint"] or None}
    if not cfg["enabled"] or not store:
        return out
    today = dr.day_key(now)
    snaps = store.snapshots_of_day(today, cfg["mint"])
    weights = dr.day_weights(snaps)
    top = sorted(weights.items(), key=lambda kv: -kv[1])[:10]
    out.update({"today": today, "next_draw": datetime.fromtimestamp(dr.draw_time(today), timezone.utc).isoformat(),
                "snapshots_today": len(snaps), "snapshot_hours": [h["hour"] for h in store.snapshot_hours(today, cfg["mint"])],
                "participants_so_far": len(weights), "total_weight_so_far": sum(weights.values()),
                "top_so_far": [{"address": a, "weight": w} for a, w in top],
                "note": "weights so far are averages over today's snapshots; the USD threshold is applied at draw time",
                "min_usd": cfg["min_usd"], "min_tokens": cfg["min_tokens"]})
    return out


# ---------- планировщик ----------
class Scheduler(threading.Thread):
    """Фоновый поток: снимок в каждом UTC-часе (как только час начался), розыгрыш суток в 00:05 UTC
    следующих суток. Ошибки логируются и повторяются позже; поток не падает и не роняет сервер."""

    def __init__(self, store, cfg):
        super().__init__(daemon=True, name="draw-scheduler")
        self.store, self.cfg = store, cfg
        self._stop = threading.Event()
        self._retry = {}   # ключ задачи -> не раньше этого времени

    def stop(self):
        self._stop.set()

    def _due(self, key, now):
        return now >= self._retry.get(key, 0)

    def tick(self, now=None):
        now = now or time.time()
        mint = self.cfg["mint"]
        hour = dr.hour_key(now)
        if not self.store.has_snapshot(hour) and self._due(("snap", hour), now):
            try:
                s = take_snapshot(self.store, mint, now)
                if s["taken"]:
                    log(f"snapshot {hour}: {s['holders']} holders, {s['wallets']} wallets")
            except Exception as e:
                self._retry[("snap", hour)] = now + RETRY_AFTER
                log(f"snapshot {hour} failed: {e}")
        for day in self.store.days_with_snapshots(mint):
            if now < dr.draw_time(day) or self.store.get_draw(day) or not self._due(("draw", day), now):
                continue
            try:
                row = run_draw(self.store, mint, day, self.cfg, now)
                if row:
                    log(f"draw {day}: {row['status']}, {row['participants']} participants, winner {row['winner']}")
            except NotReady:
                self._retry[("draw", day)] = now + 60
            except Exception as e:
                self._retry[("draw", day)] = now + RETRY_AFTER
                log(f"draw {day} failed: {e}")

    def run(self):
        log(f"scheduler started for {self.cfg['mint']}")
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log("tick failed: " + traceback.format_exc(limit=3).replace("\n", " | "))
            self._stop.wait(TICK)
