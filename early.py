"""Early buyers: the first 20 unique buyers of a token after launch and what they have done with it now.
Separate from the scan (GET /api/early, the frontend calls it after the verdict): the scan does not wait or slow down.

Network is the adapter (early_buyers: who bought first, never changes; early_status: balances and exits now),
statuses and summary are detect.early_report (pure functions). This module holds the cache, limits and budget:
  - buyers and their entries are cached per token for BUYERS_TTL (a day), statuses for STATUS_TTL (10 min);
  - failures are not cached: the last successful response younger than STALE_TTL is returned with stale_at;
  - at most MAX_CONCURRENT computations at once, one per token (others wait for it);
  - BUDGET seconds per computation: out of time means partial (some exits not parsed);
  - TOO ESTABLISHED (verdict in the market cache): not computed.
"""
import threading, time

import detect as d
import engine
import market
from chains import bankr, flap, priority

N = 20                    # buyers
BUDGET = 10.0             # seconds per computation (network)
BUYERS_TTL = 24 * 3600    # seconds: buyers and entries never change
STATUS_TTL = 600          # seconds: balances and exits
STALE_TTL = 6 * 3600      # seconds: last successful response on failure
MAX_CONCURRENT = 2        # computations at once (RPC limiter shared with scans)
CACHE_MAX = 500           # tokens in the cache

_BUYERS, _STATUS, _INFLIGHT = {}, {}, {}   # key -> (ts, data); key -> (ts, data, status); key -> Event
_lock = threading.Lock()
_sem = threading.BoundedSemaphore(MAX_CONCURRENT)


def background(chain, token):
    """Computation without a live scan history (the adapter keeps it, but there is no scan younger than SCAN_TTL) is background work:
    it yields to live scans and runs under BACKGROUND_RPS (server: priority.background() instead of live())."""
    if flap.candidate(token) or bankr.candidate(token):   # Flap, Bankr: no scan history for early, computation is live
        return False
    a = engine.CHAINS[chain]
    return hasattr(a, "scan_history") and a.scan_history(token) is None


def budget(a):
    """Computation budget: background gets as many times more as BACKGROUND_RPS is below the adapter limit."""
    return BUDGET * (priority.background_scale(a.RPS) if priority.is_background() else 1.0)


def clear_cache():
    with _lock:
        _BUYERS.clear()
        _STATUS.clear()


def _put(cache, key, value):
    with _lock:
        cache[key] = value
        if len(cache) > CACHE_MAX:
            for k, _ in sorted(cache.items(), key=lambda kv: kv[1][0])[:len(cache) - CACHE_MAX]:
                del cache[k]


def _out(base, hit, flags, stale=False):
    ts, data, status = hit
    out = base | {"available": True, "launch_ts": data["launch"]["ts"], "block": data["launch"]["block"],
                  "updated_at": int(ts)} | d.early_report(data, status, flags)
    if stale:
        out["stale_at"] = int(ts)
    return out


def _compute(a, key, token):
    """(ts, data, status) or None (token not from a launchpad). Exceptions propagate (network failure)."""
    if flap.candidate(token) and flap.detect(token) is not None:   # Flap (FLAP_ENABLED): one HTTP call, Pons needs no requests
        a = flap
    elif bankr.candidate(token) and bankr.detect(token) is not None:   # Bankr (BANKR_ENABLED): ba3 suffix + one HTTP call
        a = bankr
    t0, r0 = time.time(), a.REQUESTS[0]
    deadline = t0 + budget(a)
    with _lock:
        b = _BUYERS.get(key)
    data = b[1] if b and t0 - b[0] < BUYERS_TTL else None
    if data is None:
        data = a.early_buyers(token, N, deadline)
        if data is None:
            return None
        if len(data["buyers"]) >= N:   # the first N will not change; fewer means a fresh token will get more
            _put(_BUYERS, key, (time.time(), data))
    status = a.early_status(token, data["buyers"], deadline, launch=data["launch"])
    hit = (time.time(), data, status)
    _put(_STATUS, key, hit)
    print(f"early: {token} {key[0]} {len(data['buyers'])} buyers, {a.REQUESTS[0] - r0} HTTP, "
          f"{time.time() - t0:.1f}s" + (" (partial)" if any(s["partial"] for s in status["wallets"].values()) else ""),
          flush=True)
    return hit


def get(token, flags=None):
    """/api/early response. token is a CA (ScanError on a bad address propagates, that is a 400).
    flags: {wallet: [flags]} from the token's last scan (top 20), see server.scan_flags."""
    chain, token = engine.chain_of(token)
    base = {"token": token, "chain": chain}
    a = engine.CHAINS[chain]
    if not hasattr(a, "early_buyers"):
        return base | {"available": False, "reason": "not supported for this chain yet"}
    if market.established_get(token, engine.GT_NETWORK[chain]) is not None:
        return base | {"available": False, "reason": "too established"}
    key = (chain, token)
    with _lock:
        hit = _STATUS.get(key)
        if hit and time.time() - hit[0] < STATUS_TTL:
            return _out(base, hit, flags)
        ev = _INFLIGHT.get(key)
        owner = ev is None
        if owner:
            ev = _INFLIGHT[key] = threading.Event()
    if not owner:   # the same token is already being computed: wait for its result
        ev.wait(BUDGET * priority.background_scale(a.RPS) + 5)   # the owner may be computing in the background
        with _lock:
            hit = _STATUS.get(key)
        if hit and time.time() - hit[0] < STATUS_TTL:
            return _out(base, hit, flags)
        return _fallback(base, key, flags, "busy")
    try:
        if not _sem.acquire(timeout=BUDGET):
            return _fallback(base, key, flags, "busy")
        try:
            res = _compute(a, key, token)
        finally:
            _sem.release()
        if res is None:
            return base | {"available": False, "reason": engine.not_launchpad(chain)}
        return _out(base, res, flags)
    except Exception as e:
        print(f"early: {token} failed: {type(e).__name__}: {e}", flush=True)
        if "history too long" in str(e):
            return base | {"available": False, "reason": "launch history too long"}
        return _fallback(base, key, flags, "temporarily unavailable")
    finally:
        with _lock:
            _INFLIGHT.pop(key, None)
        ev.set()


def _fallback(base, key, flags, err):
    """Last successful response younger than STALE_TTL (with stale_at), or the error."""
    with _lock:
        hit = _STATUS.get(key)
    if hit and time.time() - hit[0] < STALE_TTL:
        return _out(base, hit, flags, stale=True)
    return base | {"available": True, "error": err}


def known_share(chain, token):
    """Supply share held by the first N buyers now, from the last successful computation younger than STALE_TTL, no network;
    not computed: None (for alerts snapshots)."""
    with _lock:
        hit = _STATUS.get((chain, token))
    if not hit or time.time() - hit[0] >= STALE_TTL:
        return None
    return d.early_report(hit[1], hit[2])["summary"]["held_share_supply"]   # locked in a locker is not an exit
