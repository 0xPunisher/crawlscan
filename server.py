"""rh-crawler HTTP server (standard library, ThreadingHTTPServer).
Read-only: the scan runs in the background, the browser polls for events.

  POST /api/scan {"token": "0x..."}      -> {"job": id}
  GET  /api/events?job=ID&after=N        -> {"events": [...], "done": bool}  (events with i >= N)
  GET  /api/result?job=ID                -> {"done", "result" | "error"}
  GET  /api/chart?token=CA               -> {"token", "chain", "pool", "dex", "timeframe", "candles", "price_usd"[, "stale_at" | "unavailable"]}
                                            GeckoTerminal candles [[ts, o, h, l, c, v]] oldest to newest; not part of the scan
  GET  /api/early?token=CA               -> {"token", "chain", "available", "buyers": [...], "summary": {...}[, "stale_at"]}
                                            first 20 buyers and their current status (early.py); not part of the scan
  GET  /api/recent?limit=12              -> {"items": [{"token", "chain", "launchpad", "ticker", "name", "score", "band", "rug", "ts"}]}
                                            "Recently scanned" feed: latest unique tokens, newest first
  GET  /api/config                       -> {"solana": bool, "alerts": bool, "flap": bool, "bankr": bool, "trade": {"robinhood": template, "solana": template}}
                                            Trade on Axiom link templates ({address}), env TRADE_URL_* (trade.py)
  GET  /api/index?token=CA               -> {"token", "state": ready|building|queued|queue_full|too_large|unavailable|none, "eta_s"[, "position"]} (BANKR_ENABLED)
                                            is the full Bankr holder index ready? (the site rescans when ready)
  GET  /api/replay                       -> {"items": [...], "stats": {...}, "since"} (REPLAY_ENABLED, otherwise 404; cached 60 s)
                                            Rug Replay: DANGER tokens that dropped ≥ 90% after the scan, and shares over 7 days
  GET  /replay                           -> replay.html (REPLAY_ENABLED, otherwise 404)
  GET  /                                 -> index.html
  GET  /favicon.svg, /favicon.png, /apple-touch-icon.png, /favicon.ico  -> icons from static/
  GET  /health
  HEAD — on any GET path: same status and headers, no body

Holder draw (only with DRAW_ENABLED=true, otherwise 404; status always responds):
  GET  /api/draw/status                  -> enabled or not, token, next draw, today's snapshots and weights
  GET  /api/draw/latest                  -> latest draw
  GET  /api/draw/history?limit=30        -> draws, newest first (limit up to 365)
  GET  /api/draw/<YYYY-MM-DD>/participants -> full list of participants with weights
  GET  /api/draw/<YYYY-MM-DD>/verify     -> input data and winner recomputation
  POST /api/draw/<YYYY-MM-DD>/payout     -> admin: X-Admin-Token == ADMIN_TOKEN,
                                            {"prize_amount", "prize_currency", "payout_tx"}; otherwise 403

Token Burn & Holder Rewards, Robinhood (only with REWARDS_ENABLED=true, otherwise 404; status always responds):
  GET  /api/rewards/status               -> token, next draw and scheduled burn, latest burn,
                                            total burned, latest winner (weight, chance, payout), participants
  GET  /api/rewards/history?limit=30     -> draws and burns, newest first (limit up to 365)
  GET  /api/rewards/history?kind=burns|draws&limit=10&before=T -> full history page: {"kind", "total",
                                            "items", "next_before"}; limit 1–50, before — unix time or ISO 8601
  GET  /api/rewards/<YYYY-MM-DD>/participants -> full list of participants with weights
  GET  /api/rewards/<YYYY-MM-DD>/verify  -> input data and winner recomputation

Alerts, subscriptions for the bot (only with ALERTS_ENABLED=true, otherwise 404; X-Alerts-Secret == ALERTS_API_SECRET, otherwise 403):
  POST /api/alerts/watch {"chat_id", "token"}   -> subscribe for WATCH_DAYS days; 409 — already WATCH_LIMIT tokens,
                                                   422 — token too established, 400 — bad address
  POST /api/alerts/unwatch {"chat_id", "token"} -> {"ok", "removed"}
  GET  /api/alerts/list?chat_id=N               -> {"items": [{"token", "chain", "created_at", "expires_at"}], ...}
  POST /api/alerts/test {"chat_id", "token"}    -> test notification to chat_id right away (no cooldown or subscription) from
                                                   the latest token snapshot, or from a fresh scan if there is no snapshot;
                                                   {"ok", "source": "snapshot" | "scan"}; 502 — Telegram refused

Token result is cached for 10 minutes: a repeat scan returns the saved events immediately.
Chart is cached for 10 minutes (without candles — 1 minute); a GeckoTerminal failure gives empty candles, not an error.
/api/rewards/status, /api/draw/status and /api/config are cached in memory for 30 seconds (Cache-Control: public, max-age=30 —
for Cloudflare; the rewards status field now is always current); failures are not cached.
New scans per IP (CF-Connecting-IP) — at most SCAN_RATE_PER_MIN per minute, otherwise 429 rate_limited with Retry-After;
user agent crawlscan-bot — SCAN_RATE_BOT_PER_MIN, a valid X-Alerts-Secret (our bot) — no limit.
The /api/recent feed is cached for 15 seconds (a scan write resets the cache; a read that overlapped a write is not cached);
written after the scan finishes, a database failure does not break the scan (logged as "recent: not saved").
With ALERTS_ENABLED=true a snapshot for alerts is written after the scan (alerts.py, alerts_store.py), also without affecting the scan;
if a key metric changed — a Telegram notification to subscribers (alerts_notify.py, own thread, TG_BOT_TOKEN);
scheduled rechecks of watched tokens — alerts_recheck.py (own thread, yields to live scans).
With OPMEM_ENABLED=true scan and recheck results go to the operator memory queue (opmem.py: own database
memory.db, own writer thread, a token at most once per OPMEM_MIN_INTERVAL_H); the scan does not wait for the write.
With REPLAY_ENABLED=true — Rug Replay (replay.py): its own thread every REPLAY_CHECK_MIN checks the market cap of DANGER tokens from
memory.db against DexScreener (no RPC), /replay and /api/replay; the scan, bot and draw are not touched.
At most 3 concurrent scans (the rest wait for a slot), at most SCAN_QUEUE_MAX in the queue; beyond the queue — 503 busy
with Retry-After (cache and an already running scan of the same token are served as before). Process memory (memguard.py) above the soft
threshold after cache reset — scans up to MAX_CONCURRENT without a queue, above the hard one — 503 busy. Once a minute the log gets
a line with memory, number of active scans and per-minute counters. A database failure in /api/rewards/* and /api/draw/* — 503, the process and scans keep running.
PORT from env, default 8000.
"""
import hashlib, hmac, json, os, re, threading, time, uuid
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import alerts
import alerts_notify
import alerts_recheck
import detect
from chains import bankr, flap, priority
import early
import engine
import market
import memguard
import opmem
import replay
import trade
import draw_service as ds
import rewards_service as rs
from draw_store import Store
from rewards_store import RewardsStore
from recent_store import RecentStore
from alerts_store import AlertsStore
from bot.tg import TelegramError

CACHE_TTL = 600            # seconds: per-token result cache
MAX_CONCURRENT = 3         # concurrent scans by default (env MAX_CONCURRENT)
JOB_TTL = 3600             # seconds: old jobs are removed from memory
ROOT = os.path.dirname(os.path.abspath(__file__))
ICONS = {                  # path -> (file in static/, content-type); .ico serves the same PNG
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/apple-touch-icon.png": ("favicon.png", "image/png"),
    "/favicon.ico": ("favicon.png", "image/png"),
}
ICON_CACHE = "public, max-age=86400"
HTML_CACHE = "no-cache"    # page: the browser stores it but revalidates every time (ETag / Last-Modified → 304)
_PAGE = {}                 # (path, mtime_ns, size) -> (body, etag, last_modified)

CHART_TTL = 600            # seconds: per-token chart cache
CHART_EMPTY_TTL = 60       # seconds: cache for a chart without candles (GT did not respond or the token is too fresh)
CHART_STALE_TTL = 6 * 3600 # seconds: the last successful chart is served (with stale_at) while GT is not responding
CHART_MAX = 500            # charts in memory, old ones are evicted
RECENT_TTL = 15            # seconds: /api/recent feed cache (and max-age for Cloudflare)
STATUS_TTL = 30            # seconds: cache for /api/rewards/status, /api/draw/status, /api/config (and max-age)
STATUS_CACHE = f"public, max-age={STATUS_TTL}"
REPLAY_TTL = 60            # seconds: /api/replay cache (and max-age)
RECENT_DEFAULT = 12        # feed entries by default

JOBS = {}                  # job_id -> {"token", "chain", "events", "done", "result", "error", "ts"}
CHARTS = {}                # token -> {"ok": (ts, successful response) | None, "miss": (ts, empty response) | None}
RECENT = {}                # limit -> (ts, /api/recent response)
_RECENT_GEN = [0]          # feed generation: +1 on every scan write (under _lock)
BY_TOKEN = {}              # token -> job_id of the latest scan
_lock = threading.Lock()


def _env_int(name, default):
    try:
        v = int(os.environ.get(name, "").strip())
        return v if v > 0 else default
    except ValueError:
        return default


MAX_CONCURRENT = _env_int("MAX_CONCURRENT", MAX_CONCURRENT)
_sem = threading.BoundedSemaphore(MAX_CONCURRENT)
SCAN_QUEUE_MAX = 6         # new scans waiting for a slot beyond MAX_CONCURRENT (env SCAN_QUEUE_MAX); beyond that — 503 busy
SCAN_QUEUE_MAX = _env_int("SCAN_QUEUE_MAX", SCAN_QUEUE_MAX)
_ACTIVE = [0]              # scans running or waiting for a slot (under _lock)
_STATS = {"started": 0, "busy": 0}   # new scans and busy refusals since the last memguard line (under _lock)
RETRY_AFTER = 5            # seconds: Retry-After header on 503 busy
BUSY = {"error": "busy", "message": "Scanner is busy, try again in a few seconds"}
# rate of new scans per IP (CF-Connecting-IP): regular client / CrawlScan bot by user agent;
# a request with a valid X-Alerts-Secret (our bot) — no limit
SCAN_RATE_PER_MIN = _env_int("SCAN_RATE_PER_MIN", 10)
SCAN_RATE_BOT_PER_MIN = _env_int("SCAN_RATE_BOT_PER_MIN", 120)
RATE_WINDOW = 60           # seconds
RATE_MAX_CLIENTS = 20_000  # IPs in memory; above that — forget them all (the limit is soft)
RATE_LIMITED = {"error": "rate_limited", "message": "Too many scans from your address, try again in a minute"}
_RATE = {}                 # client -> [times of new scans within RATE_WINDOW]

for _a in engine.CHAINS.values():       # above the memory threshold memguard drops scan history and transfer caches
    if hasattr(_a, "drop_caches"):
        memguard.on_relieve(_a.drop_caches)
memguard.on_relieve(bankr.drop_caches)  # ... and Bankr indexes (a running build is interrupted)
bankr.memory_high = memguard.near       # Bankr index build pauses near the memory threshold
bankr.after_build = memguard.trim       # ... and after it the page memory is returned to the OS


class Busy(Exception):
    """New scan not accepted: queue is full or memory is above MEMORY_SOFT_LIMIT_MB."""


class RateLimited(Exception):
    """Too many new scans from one IP; args[0] — seconds until allowed again."""


def _rate_take(client, now):
    """Count a new client scan (client = (key, per-minute limit) or None — no limit). Under _lock.
    Limit exhausted — RateLimited(seconds until freed)."""
    if client is None:
        return
    key, limit = client
    hits = [t for t in _RATE.get(key, ()) if now - t < RATE_WINDOW]
    if len(hits) >= limit:
        _RATE[key] = hits
        raise RateLimited(max(1, int(RATE_WINDOW - (now - hits[0])) + 1))
    if key not in _RATE and len(_RATE) >= RATE_MAX_CLIENTS:
        _RATE.clear()
    hits.append(now)
    _RATE[key] = hits


def _run(job_id, token):
    try:
        with priority.live():   # live scan: background alerts rechecks wait and do not start
            _run_job(job_id, token)
    finally:
        with _lock:
            _ACTIVE[0] -= 1


def _run_job(job_id, token):
    job = JOBS[job_id]

    def emit(e):
        with _lock:
            job["events"].append(e)

    with _sem:
        try:
            res = engine.scan(token, emit)
            with _lock:
                job["result"] = res
        except engine.ScanError as e:
            err = str(e)
        except Exception as e:  # RPC failure etc.: return the text, the process lives on
            err = f"scan failed: {e}"
        else:
            err = None
        with _lock:
            if err:
                job["error"] = err
                job["events"].append({"i": len(job["events"]), "t": int((time.time() - job["ts"]) * 1000),
                                      "type": "error", "spider": None, "wallet": None, "detail": err,
                                      "chain": job["chain"]})
            job["done"] = True
    if not err:
        opmem.record(res)       # queue only (OPMEM_ENABLED), the write happens in its own thread
        record_recent(res)
        record_snapshot(res)


def _reuse(token, now):
    """job_id of a fresh cache entry or a running scan of the token, otherwise None (under _lock)."""
    jid = BY_TOKEN.get(token)
    job = JOBS.get(jid)
    if job and (not job["done"] or (job["result"] and now - job["ts"] < CACHE_TTL and not _index_ready(job["result"]))):
        return jid
    return None


def _index_ready(result):
    """Partial Bankr scan (holder index was building), but the index is now ready — cache is stale: a new scan will be full."""
    ps = result.get("partial_scan")
    return bool(ps) and bankr.index_status(result["token"])["state"] == "ready"


def _busy(why):
    with _lock:
        _STATS["busy"] += 1
    raise Busy(why)


def scan_stats():
    """For the once-a-minute memguard line: (active scans, line tail); counters are reset."""
    with _lock:
        active, started, busy = _ACTIVE[0], _STATS["started"], _STATS["busy"]
        _STATS["started"] = _STATS["busy"] = 0
    return active, f", last min: {started} started, {busy} busy"


def start_scan(token, client=None):
    """job_id: fresh cache for the token or an already running scan of the same token, otherwise a new one.
    New only if there is room in the queue and memory is below the threshold, otherwise Busy; and if client (key, per-minute limit)
    has not exhausted the new-scan limit, otherwise RateLimited. Cache hits and joining a running scan do not use the limit."""
    chain, token = engine.chain_of(token)
    now = time.time()
    with _lock:
        for jid in [j for j, v in JOBS.items() if v["done"] and now - v["ts"] > JOB_TTL]:
            if BY_TOKEN.get(JOBS[jid]["token"]) == jid:
                del BY_TOKEN[JOBS[jid]["token"]]
            del JOBS[jid]
        jid = _reuse(token, now)
        if jid:
            return jid
        active = _ACTIVE[0]
    if active >= MAX_CONCURRENT + SCAN_QUEUE_MAX:
        _busy("queue")
    if memguard.over(live=active, cap=MAX_CONCURRENT):   # outside _lock: cache reset and gc do not block other requests
        _busy("memory")
    with _lock:
        jid = _reuse(token, now)
        if jid:
            return jid
        if _ACTIVE[0] >= MAX_CONCURRENT + SCAN_QUEUE_MAX:
            _STATS["busy"] += 1
            raise Busy("queue")
        _rate_take(client, now)
        _ACTIVE[0] += 1
        _STATS["started"] += 1
        jid = uuid.uuid4().hex[:12]
        JOBS[jid] = {"token": token, "chain": chain, "events": [], "done": False, "result": None, "error": None,
                     "ts": now}
        BY_TOKEN[token] = jid
    try:
        threading.Thread(target=_run, args=(jid, token), daemon=True).start()
    except RuntimeError:   # can't start new thread: same as a full queue
        with _lock:
            _ACTIVE[0] -= 1
            JOBS.pop(jid, None)
            if BY_TOKEN.get(token) == jid:
                del BY_TOKEN[token]
        raise Busy("thread") from None
    return jid


def scan_flags(token):
    """{wallet: ["operator", "virgin"]} from the latest finished scan of the token (top-20 only, the ones the scan checked);
    no scan — None. operator — wallet in an operator of 2+ wallets."""
    with _lock:
        job = JOBS.get(BY_TOKEN.get(token))
        res = job and job["done"] and job["result"]
    if not res or not res.get("holders"):
        return None
    multi = {w for o in res.get("operators") or [] if len(o.get("wallets") or []) > 1 for w in o["wallets"]}
    return {h["wallet"]: [f for f, on in (("operator", h["wallet"] in multi),
                                          ("virgin", (h.get("signals") or {}).get("virgin"))) if on]
            for h in res["holders"]}


def get_chart(token):
    """/api/chart response: address via engine.chain_of (ScanError — bad address / Solana disabled),
    candles from market.fetch_chart with cache. Any GT failure is not an exception: unavailable = True (no candles because of
    a failure, not because there are few trades) or the last successful chart younger than
    CHART_STALE_TTL with a stale_at field (unix time when it was fetched), otherwise empty. An empty response does not
    overwrite a successful cache entry."""
    chain, token = engine.chain_of(token)
    now = time.time()

    def fallback(e):
        ok = e.get("ok")
        if ok and now - ok[0] < CHART_STALE_TTL:
            return ok[1] | {"stale_at": int(ok[0])}
        return e["miss"][1]

    with _lock:
        e = CHARTS.get(token) or {"ok": None, "miss": None}
        if e["ok"] and now - e["ok"][0] < CHART_TTL:
            return e["ok"][1]
        if e["miss"] and now - e["miss"][0] < CHART_EMPTY_TTL:
            return fallback(e)
    try:
        c = market.fetch_chart(token, engine.GT_NETWORK[chain])
    except Exception:
        c = {"failed": True}
    out = {"token": token, "chain": chain, "pool": c.get("pool"), "dex": c.get("dex"),
           "timeframe": c.get("timeframe"), "candles": c.get("candles") or [], "price_usd": c.get("price_usd")}
    if c.get("failed") and not out["candles"]:
        out["unavailable"] = True
    with _lock:
        e = CHARTS.get(token) or {"ok": None, "miss": None}
        res = out
        if out["candles"]:
            e = {"ok": (now, out), "miss": None}
        else:
            e = e | {"miss": (now, out)}
            res = fallback(e)
            print(f"chart: {token} no candles from gt -> "
                  + (f"stale cache from {time.strftime('%H:%M', time.gmtime(res['stale_at']))} UTC"
                     if "stale_at" in res else "empty"), flush=True)
        CHARTS[token] = e
        if len(CHARTS) > CHART_MAX:
            last = lambda kv: max(x[0] for x in (kv[1]["ok"], kv[1]["miss"]) if x)
            for t, _ in sorted(CHARTS.items(), key=last)[:len(CHARTS) - CHART_MAX]:
                del CHARTS[t]
    return res


DRAW_PATH = re.compile(r"^/api/draw/(\d{4}-\d{2}-\d{2})/(participants|verify|payout)$")
_stores, _stores_lock = {}, threading.Lock()


def draw_store():
    """Draw store (one per DRAW_DB_PATH) or None if the draw is disabled."""
    if not ds.enabled():
        return None
    path = os.environ.get("DRAW_DB_PATH") or ""
    with _stores_lock:
        if path not in _stores:
            _stores[path] = Store(path or None)
        return _stores[path]


REWARDS_PATH = re.compile(r"^/api/rewards/(\d{4}-\d{2}-\d{2})/(participants|verify)$")
_rw_stores = {}


def rewards_store():
    """Rewards & Burns store (file DRAW_DB_PATH) or None if disabled."""
    if not rs.enabled():
        return None
    path = os.environ.get("DRAW_DB_PATH") or ""
    with _stores_lock:
        if path not in _rw_stores:
            _rw_stores[path] = RewardsStore(path or None)
        return _rw_stores[path]


def history_args(q):
    """kind, limit, before from a /api/rewards/history?kind=burns|draws&limit=10&before=... page request.
    before — unix time (seconds) or ISO 8601; error — ValueError with text for the 400."""
    kind = q["kind"][0]
    if kind not in ("burns", "draws"):
        raise ValueError("kind must be burns or draws")
    try:
        limit = int((q.get("limit") or ["10"])[0])
    except ValueError:
        raise ValueError("limit must be an integer") from None
    raw = (q.get("before") or [""])[0].strip()
    before = None
    if raw:
        try:
            if raw.replace(".", "", 1).isdigit():
                before = int(float(raw))
            else:   # ISO without timezone — UTC
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                before = int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())
        except (ValueError, OverflowError):
            raise ValueError("before must be unix time or ISO 8601") from None
    return kind, limit, before


_recent_stores = {}


def recent_store():
    """Feed store (file DRAW_DB_PATH), one per path."""
    path = os.environ.get("DRAW_DB_PATH") or ""
    with _stores_lock:
        if path not in _recent_stores:
            _recent_stores[path] = RecentStore(path or None)
        return _recent_stores[path]


def record_recent(result):
    """Write a finished scan to the feed. Called after done: the client already has the verdict.
    Any database failure is only logged — the scan does not depend on the feed."""
    try:
        if recent_store().record(result):
            with _lock:
                _RECENT_GEN[0] += 1
                RECENT.clear()
    except Exception as e:
        print(f"recent: not saved: {type(e).__name__}: {e}", flush=True)


_alerts_stores = {}


def alerts_store():
    """Alerts snapshot store (file DRAW_DB_PATH), one per path."""
    path = os.environ.get("DRAW_DB_PATH") or ""
    with _stores_lock:
        if path not in _alerts_stores:
            _alerts_stores[path] = AlertsStore(path or None)
        return _alerts_stores[path]


_notifier = {"obj": None, "warned": False}


def notifier():
    """Notification sender (its own background thread, alerts_notify) or None without TG_BOT_TOKEN — then a single
    warning in the log. The token is never printed."""
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    with _stores_lock:
        if not token:
            if not _notifier["warned"]:
                _notifier["warned"] = True
                print("alerts: TG_BOT_TOKEN is not set, notifications are not sent", flush=True)
            return None
        if _notifier["obj"] is None:
            _notifier["obj"] = alerts_notify.Notifier(alerts_store, alerts_notify.telegram(token)).start()
        return _notifier["obj"]


def notify_message(chat_id, text, markup=None):
    """Service message to a subscriber via the sender queue; without TG_BOT_TOKEN — not sent."""
    n = notifier()
    if n:
        n.push_message(chat_id, text, markup)


def record_snapshot(result):
    """Snapshot of a finished scan for alerts (only with ALERTS_ENABLED) — for both live scans and rechecks.
    Called after done, like the feed: the client already has the verdict; a database failure is only logged.
    If the diff against the old snapshot is not empty — an event goes to the notification queue (a background thread finds subscribers and sends;
    here — no network and no waiting). TOO_ESTABLISHED — auto-unsubscribe all subscribers of the token with one message."""
    if not alerts.enabled() or result.get("partial_scan") or result.get("band") == detect.TOO_ACTIVE:
        return   # partial Bankr scan: a diff against the full one would be false; TOO ACTIVE — no verdict, nothing to compare
    try:
        snap = alerts.snapshot(result, early.known_share(result.get("chain"), result.get("token")))
        if not snap:
            return
        prev, cur = alerts_store().record(snap)
        if cur["band"] == detect.TOO_ESTABLISHED:
            chats = alerts_store().unwatch_token(cur["token"])
            if chats:
                print(f"alerts: {cur['token']} became too established, unwatched {len(chats)} chats", flush=True)
            for chat_id in chats:
                notify_message(chat_id, *alerts.established_message(cur["token"], cur.get("ticker"), cur.get("chain")))
            return
        changes = alerts.diff(prev, cur)
        if changes:
            n = notifier()
            if n:
                n.push(prev, cur, changes)
    except Exception as e:
        print(f"alerts: snapshot not saved: {type(e).__name__}: {e}", flush=True)


def record_early(body):
    """/api/early computed the block → early buyers' share into the latest token snapshot (only with ALERTS_ENABLED).
    Called after responding to the client; no network; failures are only logged. Stale responses (stale_at) and errors are not written."""
    if not alerts.enabled():
        return
    try:
        if body.get("available") and not body.get("error") and not body.get("stale_at") and body.get("summary"):
            alerts_store().set_early(body["token"], body["summary"].get("held_share_supply", body["summary"]["now_share_supply"]))   # locker is not an exit
    except Exception as e:
        print(f"alerts: early share not saved: {type(e).__name__}: {e}", flush=True)


WATCH_MARKET_BUDGET = 3.0  # seconds for the "too established" check when subscribing to a token that has not been scanned yet
TOO_ESTABLISHED_WATCH = "This token is too established for CrawlScan, so it can't be watched."
TOO_ACTIVE_WATCH = "This token has too many trades for a full scan right now, so it can't be watched."
WATCH_LIMIT_TEXT = f"You can watch up to {alerts.WATCH_LIMIT} tokens. Unwatch one first."


def too_established(chain, token):
    """Can we subscribe: latest snapshot, a too established verdict in cache, or the market (GT / DexScreener, 15 min cache;
    no blockchain calls). Market did not respond — subscribe (a scheduled recheck will unsubscribe)."""
    try:
        _, cur = alerts_store().get(token)
    except Exception:
        cur = None
    if cur and cur.get("band") == detect.TOO_ESTABLISHED:
        return True
    network, limits = engine.GT_NETWORK[chain], engine.established_limits()
    cached = market.established_get(token, network)
    if cached is not None:
        return detect.too_established(cached, limits)
    try:
        gt = market.fetch_market(token, network, budget=WATCH_MARKET_BUDGET)
    except Exception:
        return False
    if detect.too_established(gt, limits):
        market.established_put(token, network, gt)
        return True
    return False


def alerts_secret_ok(header):
    """X-Alerts-Secret == ALERTS_API_SECRET in constant time; secret not set — always no. Never logged."""
    secret = os.environ.get("ALERTS_API_SECRET", "")
    return bool(secret) and hmac.compare_digest((header or "").encode(), secret.encode())


def _chat_id(v):
    if isinstance(v, bool):
        raise ValueError
    if isinstance(v, str):
        v = int(v)
    if not isinstance(v, int):
        raise ValueError
    return v


TEST_SCAN_WAIT = 60   # seconds: wait for a scan for the test notification if there is no token snapshot


def test_snapshot(chain, token):
    """(snapshot, diff, source) for the test notification: the latest token snapshot; if none — a finished scan result
    from memory or a new scan (wait up to TEST_SCAN_WAIT). → (None, None, error text) if there is no result."""
    prev, cur = alerts_store().get(token)
    if cur:
        return cur, alerts.diff(prev, cur), "snapshot"
    try:
        jid = start_scan(token)
    except Busy:
        return None, None, BUSY["message"]
    t0 = time.time()
    while time.time() - t0 < TEST_SCAN_WAIT:
        with _lock:
            job = JOBS.get(jid)
            done, res, err = job["done"], job["result"], job["error"]
        if done:
            if err or not res:
                return None, None, err or "scan failed"
            return alerts.snapshot(res), [], "scan"
        time.sleep(0.2)
    return None, None, "scan timed out"


def alerts_test(body):
    """POST /api/alerts/test → (code, body). Sends immediately, in this thread: no queue, cooldown or subscription check;
    a Telegram 403 does not touch subscriptions."""
    token_env = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not token_env:
        return 503, {"error": "TG_BOT_TOKEN is not set"}
    snap, changes, source = test_snapshot(*engine.chain_of(body.get("token")))
    if snap is None:
        return 409, {"error": f"no result for this token: {source}"}
    text, markup = alerts.message(snap, changes, trade.url(snap.get("chain"), snap["token"]), test=True)
    try:
        alerts_notify.telegram(token_env)(body["chat_id"], text, markup)
    except TelegramError as e:
        return 502, {"error": f"telegram: {e.description}", "code": e.code}
    print(f"alerts: test alert {snap['token']} to {body['chat_id']} ({source})", flush=True)
    return 200, {"ok": True, "token": snap["token"], "source": source, "band": snap.get("band"),
                 "changes": [c["kind"] for c in changes]}


def alerts_api(method, path, body, q):
    """/api/alerts/* → (code, body). The caller checks the switch and the secret."""
    try:
        chat_id = _chat_id((q.get("chat_id") or [None])[0] if method == "GET" else body.get("chat_id"))
    except (ValueError, TypeError, AttributeError):
        return 400, {"error": "need chat_id"}
    store, now = alerts_store(), int(time.time())
    meta = {"limit": alerts.WATCH_LIMIT, "days": alerts.WATCH_DAYS}
    if method == "GET" and path == "/api/alerts/list":
        return 200, {"chat_id": chat_id, "items": store.watches(chat_id, now)} | meta
    if method != "POST" or path not in ("/api/alerts/watch", "/api/alerts/unwatch", "/api/alerts/test"):
        return 404, {"error": "not found"}
    try:
        chain, token = engine.chain_of(body.get("token"))
    except engine.ScanError as e:
        return 400, {"error": str(e)}
    if path == "/api/alerts/test":
        return alerts_test(body | {"chat_id": chat_id})
    if path == "/api/alerts/unwatch":
        removed = store.unwatch(chat_id, token)
        return 200, {"ok": True, "token": token, "removed": removed, "items": store.watches(chat_id, now)} | meta
    if chain == "robinhood" and bankr.recently_active(token):   # TOO ACTIVE: no verdict — nothing to watch
        return 422, {"error": "too active", "message": TOO_ACTIVE_WATCH, "token": token}
    if too_established(chain, token):
        return 422, {"error": "too established", "message": TOO_ESTABLISHED_WATCH, "token": token}
    status, w, items = store.watch(chat_id, token, chain, now)
    if status == "limit":
        return 409, {"error": "watch limit", "message": WATCH_LIMIT_TEXT, "items": items} | meta
    return 200, {"ok": True, "renewed": status == "renewed", **w, "items": items} | meta


_RESP = {}                 # key -> (ts, body): statuses and config for page polling


def cached_response(key, ttl, fn):
    """Response body from the process cache if younger than ttl, otherwise fn() (exceptions are not cached — the caller catches them)."""
    now = time.time()
    with _lock:
        hit = _RESP.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    body = fn()
    with _lock:
        _RESP[key] = (now, body)
    return body


def _fresh_now(body):
    """The cached status has a current now field (the bot computes "in 5h 12m" from it)."""
    return body | {"now": rs._iso(time.time())} if isinstance(body, dict) and "now" in body else body


def scan_client(headers, addr):
    """(key, per-minute limit) for the new-scan limit or None (no limit). IP — CF-Connecting-IP (the site is behind
    Cloudflare), otherwise the connection address. A valid X-Alerts-Secret — our bot, no limit; user agent crawlscan-bot —
    a high limit."""
    if alerts_secret_ok(headers.get("X-Alerts-Secret")):
        return None
    ip = (headers.get("CF-Connecting-IP") or "").strip() or addr
    if (headers.get("User-Agent") or "").startswith("crawlscan-bot"):
        return ("bot:" + ip, SCAN_RATE_BOT_PER_MIN)
    return (ip, SCAN_RATE_PER_MIN)


def get_recent(limit):
    """/api/recent response with RECENT_TTL cache. Database failure — empty feed, not an error."""
    now = time.time()
    with _lock:
        hit = RECENT.get(limit)
        if hit and now - hit[0] < RECENT_TTL:
            return hit[1]
        gen = _RECENT_GEN[0]
    try:
        out = {"items": recent_store().recent(limit)}
    except Exception as e:
        print(f"recent: not read: {type(e).__name__}: {e}", flush=True)
        return {"items": []}
    with _lock:
        if _RECENT_GEN[0] == gen:   # a scan was written while we were reading — this response is already stale, do not cache it
            RECENT[limit] = (now, out)
    return out


def admin_ok(header):
    """X-Admin-Token matches ADMIN_TOKEN (constant-time comparison). ADMIN_TOKEN not set — always no."""
    token = os.environ.get("ADMIN_TOKEN") or ""
    return bool(token) and bool(header) and hmac.compare_digest(header.encode(), token.encode())


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", cache="no-store", headers=None):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.send_header("cache-control", cache)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(b)

    def _send_page(self, path):
        """index.html with Cache-Control: no-cache, ETag and Last-Modified: after a deploy the browser sees the new
        page right away, an unchanged one gets 304 with no body. If-None-Match takes precedence over If-Modified-Since; a weak ETag (W/, set by
        Cloudflare when compressing) is compared as a strong one."""
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return self._send(200, b"<h1>rh-crawler</h1>", "text/html; charset=utf-8")
        key = (path, st.st_mtime_ns, st.st_size)
        page = _PAGE.get(key)
        if page is None:
            with open(path, "rb") as f:
                body = f.read()
            page = (body, '"' + hashlib.sha256(body).hexdigest()[:20] + '"', formatdate(st.st_mtime, usegmt=True))
            _PAGE.clear()
            _PAGE[key] = page
        body, etag, modified = page
        inm, ims = self.headers.get("If-None-Match"), self.headers.get("If-Modified-Since")
        if inm is not None:
            fresh = any(t.strip().removeprefix("W/") in (etag, "*") for t in inm.split(","))
        else:
            try:
                fresh = ims is not None and int(st.st_mtime) <= parsedate_to_datetime(ims).timestamp()
            except (TypeError, ValueError):
                fresh = False
        self.send_response(304 if fresh else 200)
        self.send_header("cache-control", HTML_CACHE)
        self.send_header("etag", etag)
        self.send_header("last-modified", modified)
        if fresh:
            self.end_headers()
            return
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _job(self, q):
        jid = (q.get("job") or [""])[0]
        with _lock:
            return JOBS.get(jid)

    def _draw_payout(self, day):
        if not admin_ok(self.headers.get("X-Admin-Token")):
            return self._send(403, {"error": "forbidden"})
        store = draw_store()
        if store is None:
            return self._send(404, {"error": "draw is disabled"})
        try:
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            amount, currency, tx = body["prize_amount"], body["prize_currency"], body["payout_tx"]
            if not isinstance(currency, str) or not isinstance(tx, str) or not currency or not tx \
                    or isinstance(amount, bool) or float(amount) < 0:
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            return self._send(400, {"error": "need prize_amount, prize_currency, payout_tx"})
        res = store.set_payout(day, amount, currency, tx)
        if res == "not_found":
            return self._send(404, {"error": "no draw for this day"})
        if res in ("no_winner", "conflict"):
            return self._send(409, {"error": "no winner, prize carries over" if res == "no_winner"
                                    else "payout already recorded with another tx"})
        return self._send(200, ds.draw_json(store.get_draw(day)))

    def _draw_get(self, path, q):
        if path == "/api/draw/status":
            body = cached_response(("draw", os.environ.get("DRAW_DB_PATH")), STATUS_TTL,
                                   lambda: ds.status_json(draw_store(), ds.config()))
            return self._send(200, _fresh_now(body), cache=STATUS_CACHE)
        store = draw_store()
        if store is None:
            return self._send(404, {"error": "draw is disabled"})
        if path == "/api/draw/latest":
            row = store.latest_draw()
            return self._send(200, ds.draw_json(row)) if row else self._send(404, {"error": "no draws yet"})
        if path == "/api/draw/history":
            try:
                limit = min(365, max(1, int((q.get("limit") or ["30"])[0])))
            except ValueError:
                limit = 30
            return self._send(200, {"draws": [ds.draw_json(r) for r in store.history(limit)]})
        m = DRAW_PATH.match(path)
        if not m or m.group(2) == "payout":
            return self._send(404, {"error": "not found"})
        day, what = m.groups()
        row = store.get_draw(day)
        if not row:
            return self._send(404, {"error": "no draw for this day"})
        if what == "participants":
            parts = store.participants(day)
            return self._send(200, {"day": day, "list_hash": row["list_hash"], "total_weight": int(row["total_weight"]),
                                    "participants": [{"address": a, "weight": w} for a, w in sorted(parts.items())]})
        return self._send(200, ds.verify_json(store, day))

    def _rewards_get(self, path, q):
        cfg = rs.config()
        if path == "/api/rewards/status":
            body = cached_response(("rewards", os.environ.get("DRAW_DB_PATH"), cfg["enabled"], cfg["token"]), STATUS_TTL,
                                   lambda: rs.status_json(rewards_store(), cfg))
            return self._send(200, _fresh_now(body), cache=STATUS_CACHE)
        store = rewards_store()
        if store is None:
            return self._send(404, {"error": "rewards are disabled"})
        if path == "/api/rewards/history" and q.get("kind"):
            try:
                return self._send(200, rs.history_page(store, cfg, *history_args(q)))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if path == "/api/rewards/history":
            try:
                limit = min(365, max(1, int((q.get("limit") or ["30"])[0])))
            except ValueError:
                limit = 30
            return self._send(200, rs.history_json(store, cfg, limit))
        m = REWARDS_PATH.match(path)
        if not m:
            return self._send(404, {"error": "not found"})
        day, what = m.groups()
        body = rs.participants_json(store, day) if what == "participants" else rs.verify_json(store, day)
        return self._send(200, body) if body else self._send(404, {"error": "no draw for this day"})

    def _db_guard(self, path, fn, *args):
        """/api/draw/*, /api/rewards/*: database failure (database is locked etc.) — 503, the process and scans keep running."""
        try:
            return fn(*args)
        except Exception as e:
            print(f"db: {path} failed: {type(e).__name__}: {e}", flush=True)
            return self._send(503, {"error": "temporarily unavailable"}, headers={"retry-after": str(RETRY_AFTER)})

    def _alerts(self, method, path, q):
        if not alerts.enabled():
            return self._send(404, {"error": "alerts are disabled"})
        if not alerts_secret_ok(self.headers.get("X-Alerts-Secret")):
            return self._send(403, {"error": "forbidden"})
        body = {}
        if method == "POST":
            try:
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "bad json"})
        try:
            return self._send(*alerts_api(method, path, body, q))
        except Exception as e:   # database failure etc.: the bot will show "unavailable"
            print(f"alerts: {path} failed: {type(e).__name__}: {e}", flush=True)
            return self._send(500, {"error": "alerts temporarily unavailable"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.startswith("/api/alerts/"):
            return self._alerts("POST", u.path, parse_qs(u.query))
        m = DRAW_PATH.match(urlparse(self.path).path)
        if m and m.group(2) == "payout":
            return self._db_guard(u.path, self._draw_payout, m.group(1))
        if urlparse(self.path).path != "/api/scan":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            return self._send(200, {"job": start_scan(body.get("token"),
                                                      scan_client(self.headers, self.client_address[0]))})
        except Busy:
            return self._send(503, BUSY, headers={"retry-after": str(RETRY_AFTER)})
        except RateLimited as e:
            return self._send(429, RATE_LIMITED, headers={"retry-after": str(e.args[0])})
        except engine.ScanError as e:
            return self._send(400, {"error": str(e)})
        except (ValueError, AttributeError):
            return self._send(400, {"error": "bad json"})

    def do_HEAD(self):
        """HEAD = GET without a body: same status and headers (content-length as for GET), the body is not written (_send, _send_page)."""
        return self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/health":
            return self._send(200, {"ok": True})
        if u.path.startswith("/api/draw/"):
            return self._db_guard(u.path, self._draw_get, u.path, q)
        if u.path.startswith("/api/rewards/"):
            return self._db_guard(u.path, self._rewards_get, u.path, q)
        if u.path.startswith("/api/alerts/"):
            return self._alerts("GET", u.path, q)
        if u.path == "/api/config":  # frontend and bot: which chains are enabled (SOLANA_ENABLED), alerts (ALERTS_ENABLED), Trade on Axiom
            env = tuple(os.environ.get(k) for k in ("SOLANA_ENABLED", "ALERTS_ENABLED", "FLAP_ENABLED", "BANKR_ENABLED",
                                                    "REPLAY_ENABLED", "TRADE_URL_ROBINHOOD", "TRADE_URL_SOLANA"))
            body = cached_response(("config",) + env, STATUS_TTL, lambda: {
                "solana": engine.solana_enabled(), "alerts": alerts.enabled(), "flap": flap.enabled(),
                "bankr": bankr.enabled(), "replay": replay.enabled(), "trade": trade.templates()})
            return self._send(200, body, cache=STATUS_CACHE)
        if u.path == "/api/index":   # Bankr: is the full holder index ready (the site rescans when ready)
            token = (q.get("token") or [""])[0]
            if not bankr.enabled() or not bankr.index_enabled():   # background index disabled — there is none
                return self._send(404, {"error": "not found"})
            try:
                chain, ca = engine.chain_of(token)
            except engine.ScanError as e:
                return self._send(400, {"error": str(e)})
            return self._send(200, {"token": ca, **bankr.index_status(ca)} if chain == "robinhood" else
                              {"token": ca, "state": "none", "eta_s": None})
        if u.path in ("/api/replay", "/replay"):   # Rug Replay: disabled — no page and no API
            if not replay.enabled():
                return self._send(404, {"error": "not found"})
            if u.path == "/replay":
                return self._send_page(os.path.join(ROOT, "replay.html"))
            try:
                body = cached_response(("replay",), REPLAY_TTL, lambda: replay.api(replay.db_path()))
            except Exception as e:   # database failure: not cached, the process lives on
                print(f"replay: not read: {type(e).__name__}: {e}", flush=True)
                return self._send(503, {"error": "temporarily unavailable"})
            return self._send(200, body, cache=f"public, max-age={REPLAY_TTL}")
        if u.path == "/api/chart":
            try:
                return self._send(200, get_chart((q.get("token") or [""])[0]))
            except engine.ScanError as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/early":
            try:
                token = (q.get("token") or [""])[0]
                chain, ca = engine.chain_of(token)
                # with live scan history — like a live scan; without it — background (yields to scans, BACKGROUND_RPS)
                bg = priority.background_rps() > 0 and early.background(chain, ca)
                with priority.background() if bg else priority.live():
                    body = early.get(token, scan_flags(ca))
            except engine.ScanError as e:
                return self._send(400, {"error": str(e)})
            self._send(200, body)
            return record_early(body)
        if u.path == "/api/recent":
            try:
                limit = min(50, max(1, int((q.get("limit") or [str(RECENT_DEFAULT)])[0])))
            except ValueError:
                limit = RECENT_DEFAULT
            return self._send(200, get_recent(limit), cache=f"public, max-age={RECENT_TTL}")
        if u.path == "/api/events":
            job = self._job(q)
            if job is None:
                return self._send(404, {"error": "unknown job"})
            try:
                after = max(0, int((q.get("after") or ["0"])[0]))
            except ValueError:
                after = 0
            with _lock:
                return self._send(200, {"events": job["events"][after:], "done": job["done"]})
        if u.path == "/api/result":
            job = self._job(q)
            if job is None:
                return self._send(404, {"error": "unknown job"})
            with _lock:
                if not job["done"]:
                    return self._send(200, {"done": False})
                if job["error"]:
                    return self._send(200, {"done": True, "error": job["error"]})
                return self._send(200, {"done": True, "result": job["result"]})
        if u.path in ICONS:
            name, ctype = ICONS[u.path]
            try:
                with open(os.path.join(ROOT, "static", name), "rb") as f:
                    return self._send(200, f.read(), ctype, ICON_CACHE)
            except FileNotFoundError:
                return self._send(404, {"error": "not found"})
        if u.path in ("/", "/index.html"):
            return self._send_page(os.path.join(ROOT, "index.html"))
        return self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def start_draw_scheduler():
    """Background draw scheduler — only with DRAW_ENABLED=true and DRAW_MINT set."""
    cfg = ds.config()
    if not cfg["enabled"]:
        return None
    if not cfg["mint"]:
        ds.log("DRAW_ENABLED=true, but DRAW_MINT is empty: scheduler not started")
        return None
    sch = ds.Scheduler(draw_store(), cfg)
    sch.start()
    return sch


def start_rewards_scheduler():
    """Rewards & Burns background thread — only with REWARDS_ENABLED=true."""
    cfg = rs.config()
    if not cfg["enabled"]:
        return None
    sch = rs.Scheduler(rewards_store(), cfg)
    sch.start()
    return sch


def recheck_scan(token):
    """Recheck: a regular engine scan without a job, feed or events."""
    return engine.scan(token)


def recheck_done(result):
    """Accepted recheck: alerts snapshot and operator memory (same per-token dedup as for live scans)."""
    opmem.record(result)
    record_snapshot(result)


def start_alerts_rechecker():
    """Scheduled rechecks of watched tokens — only with ALERTS_ENABLED=true."""
    if not alerts.enabled():
        return None
    cfg = alerts_recheck.config()
    print(f"alerts: rechecks every {cfg['recheck_min']} min, max {cfg['max_per_hour']}/hour", flush=True)
    return alerts_recheck.Rechecker(
        alerts_store, recheck_scan, recheck_done, notify_message,
        rate_limited=lambda: sum(a.RATE_LIMITED[0] for a in engine.CHAINS.values()), cfg=cfg,
        memory_high=lambda: memguard.over(live=1)).start()


def start_replay_checker():
    """Rug Replay: background check of DANGER token outcomes — only with REPLAY_ENABLED=true."""
    if not replay.enabled():
        return None
    cfg = replay.config()
    print(f"replay: checks every {cfg['check_min']} min, market cap at scan >= ${cfg['min_mcap']:,.0f}, "
          f"clean max {cfg['max_clean_per_hour']}/hour, db {replay.db_path()}", flush=True)
    if not opmem.enabled():
        print("replay: OPMEM_ENABLED is off, new scans are not recorded: only scans already in memory.db are checked",
              flush=True)
    return replay.Checker(replay.db_path(), cfg).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"rh-crawler on http://0.0.0.0:{port}", flush=True)
    print(memguard.status_line(0), flush=True)
    memguard.start_monitor(scan_stats)
    start_draw_scheduler()
    start_rewards_scheduler()
    start_alerts_rechecker()
    start_replay_checker()
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
