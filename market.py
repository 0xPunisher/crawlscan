"""GeckoTerminal: token header (price, market cap, liquidity, volume) and candles for the chart. Read-only, errors are not fatal.
On HTTP 429 we retry with a growing pause. The header is one request /tokens/{token}:
the free GT tier quickly answers 429, and this response already has everything for the header.
The same response (with include=top_pools) gives pool ages and FDV for the "too established" check before the scan.
The chart (fetch_chart) is not part of the scan: it is served by a separate /api/chart.
Successful GT responses are cached by request path for GT_CACHE_TTL (a shared cache for the pre-scan check,
header and chart); the "too established" verdict -- for ESTABLISHED_TTL (established_get / established_put).
Every GT failure is logged in one line: path, attempt outcomes (http code / timeout), time.
Fallback market source -- DexScreener (public API, no key): if GT did not answer (429, error, timeout),
fetch_market takes price, liquidity, market cap, age and volume from there (field source: "gt" | "dexscreener").
DexScreener has no candles -- the chart comes only from GT. GT_FORCE_FAIL=1 -- every GT request behaves like a 429."""
import json, os, socket, threading, time, urllib.request, urllib.error
from datetime import datetime

GT_API = "https://api.geckoterminal.com/api/v2"
GT = GT_API + "/networks"
GT_BUDGET = 3.0   # seconds for the whole header (all attempts): the scan does not wait longer
GT_FIRST = 1.5    # seconds for GT in fetch_market before switching to DexScreener (enough for 2 attempts on 429)
DS = "https://api.dexscreener.com/tokens/v1"
DS_BUDGET = 3.0   # seconds for DexScreener (all attempts)
DS_CHAIN = {"robinhood": "robinhood", "solana": "solana"}  # GT network -> DexScreener chainId
CHART_BUDGET = 4.0  # seconds for the whole chart (pools + candles + curve history)
CHART_LIMIT = 1000  # candles per request (GT maximum)
CURVE_DEX = "pump-fun"  # pump.fun bonding curve pool: after migration its candles are the start of the history
# timeframe by token age: (age up to, hours; GT timeframe; aggregate; label)
TIMEFRAMES = ((2, "minute", 1, "1m"), (6, "minute", 5, "5m"), (48, "minute", 15, "15m"),
              (240, "hour", 1, "1h"), (None, "hour", 4, "4h"))
GT_CACHE_TTL = 15 * 60       # seconds: successful GT response by request path
GT_CACHE_MAX = 2000          # paths in the cache; old ones are evicted
ESTABLISHED_TTL = 24 * 3600  # seconds: "too established" verdict per token -- a repeat scan does not hit GT

_CACHE, _EST, _lock = {}, {}, threading.Lock()


def clear_cache():
    with _lock:
        _CACHE.clear()
        _EST.clear()


def _key(token, network):
    return network, token.lower() if network == "robinhood" else token


def established_get(token, network="robinhood"):
    """GT data by which the token was deemed too established, if the verdict is younger than ESTABLISHED_TTL, otherwise None."""
    with _lock:
        hit = _EST.get(_key(token, network))
    return dict(hit[1]) if hit and time.time() - hit[0] < ESTABLISHED_TTL else None


def established_put(token, network, market):
    with _lock:
        _EST[_key(token, network)] = (time.time(), dict(market))
        if len(_EST) > GT_CACHE_MAX:
            for k, _ in sorted(_EST.items(), key=lambda kv: kv[1][0])[:len(_EST) - GT_CACHE_MAX]:
                del _EST[k]


def _why(e):
    """Outcome of a failed attempt for the log: http code, timeout or error type."""
    if isinstance(e, urllib.error.HTTPError):
        return f"http {e.code}"
    if isinstance(e, (TimeoutError, socket.timeout)) or isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
        return "timeout"
    return type(e).__name__


def gt_force_fail():
    """GT_FORCE_FAIL=1: all GT requests behave like 429 (to test the fallback path on staging)."""
    return os.environ.get("GT_FORCE_FAIL", "").strip() == "1"


def _get(url, timeout):
    req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "rh-crawler/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _cache_put(key, data):
    with _lock:
        _CACHE[key] = (time.time(), data)
        if len(_CACHE) > GT_CACHE_MAX:
            for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:len(_CACHE) - GT_CACHE_MAX]:
                del _CACHE[k]


def _cache_get(key):
    with _lock:
        hit = _CACHE.get(key)
    return hit[1] if hit and time.time() - hit[0] < GT_CACHE_TTL else None


def _gt(path, budget=GT_BUDGET, base=GT):
    """GET to GeckoTerminal. All attempts (including pauses after 429) take no more than budget seconds:
    the header must not delay the scan. A successful response is cached for GT_CACHE_TTL. Failed attempts
    are logged in one line (both on final failure and on success after retries)."""
    forced = gt_force_fail()
    if not forced and (hit := _cache_get(path)) is not None:
        return hit
    t0 = time.time()
    end = t0 + budget
    last, fails = None, []

    def log(outcome):
        print(f"gt: {outcome} {path} [{', '.join(fails)}] {time.time() - t0:.1f}s"
              + (" (GT_FORCE_FAIL)" if forced else ""), flush=True)

    while True:
        left = end - time.time()
        if left <= 0.2:
            fails.append("no time left")
            log("fail")
            raise last or TimeoutError("geckoterminal: no time left")
        try:
            if forced:
                raise urllib.error.HTTPError(base + path, 429, "GT_FORCE_FAIL", {}, None)
            data = _get(base + path, left)
            if fails:
                log("ok after")
            _cache_put(path, data)
            return data
        except urllib.error.HTTPError as e:
            last = e
            fails.append(_why(e))
            if e.code != 429:
                log("fail")
                raise
        except Exception as e:
            last = e
            fails.append(_why(e))
        # the pause grows: 0.5, 1, 2, 4 s -- on 429 we do not hammer GT with frequent retries
        time.sleep(min(0.5 * 2 ** (len(fails) - 1), 4.0, max(0.0, end - time.time() - 0.2)))


def fetch_market(token, network="robinhood", now=None, budget=GT_BUDGET):
    """{"name", "ticker", "price_usd", "mcap_usd", "fdv_usd", "liquidity_usd", "vol24h_usd", "age_days", "pool",
    "source"}
    or {} on error. GT first (one request /tokens/{token}?include=top_pools, up to GT_FIRST seconds),
    no answer (429, error, timeout) -- DexScreener (_ds_market); if that fails too -- GT once more for the rest of the budget.
    mcap -- market cap, otherwise fdv; liquidity -- sum over pools; age_days -- from the earliest pool (None without dates).
    pool -- address of the most liquid pool (GT) / pair (DexScreener; on Solana and Robinhood it is the same pool address
    as in GT) -- for the chart widget on the frontend. source -- "gt" | "dexscreener". A successful result is cached per token for GT_CACHE_TTL (shared by both sources).
    network -- GT network: "robinhood" | "solana" (Solana addresses are case-sensitive, we do not lowercase them).
    budget -- seconds for all attempts (the engine waits for the answer in the background longer than for the header)."""
    tok = token.lower() if network == "robinhood" else token
    key = ("market", network, tok)
    hit = _cache_get(key)
    if hit is not None and not (hit["source"] == "gt" and gt_force_fail()):
        return dict(hit)
    end = time.time() + budget
    path = f"/{network}/tokens/{tok}?include=top_pools"
    out, why = {}, None
    try:
        out = _gt_market(_gt(path, budget=min(budget, GT_FIRST)), now)
        print(f"market: {tok} gt", flush=True)
    except Exception as e:
        why = _why(e)
    if not out and DS_CHAIN.get(network):
        try:
            out = _ds_market(tok, network, now, min(DS_BUDGET, end - time.time()))
            print(f"market: {tok} gt {why} -> dexscreener", flush=True)
        except Exception as e:
            print(f"market: {tok} gt {why} -> dexscreener failed ({_why(e)})", flush=True)
    if not out and end - time.time() > 0.5:   # neither answered -- GT once more for the remainder (previous behavior)
        try:
            out = _gt_market(_gt(path, budget=end - time.time()), now)
            print(f"market: {tok} gt ok on retry", flush=True)
        except Exception as e:
            print(f"market: {tok} no market data (gt {_why(e)})", flush=True)
    if out:
        _cache_put(key, dict(out))
    return out


NATIVE_WRAPPED = {"robinhood": "0x0bd7d308f8e1639fab988df18a8011f41eacad73"}   # WETH: price of the chain's native coin


def native_usd(network="robinhood", budget=GT_BUDGET):
    """ETH price in USD (the chain's WETH) or None: GT /simple/.../token_price (cache GT_CACHE_TTL); no answer --
    the most liquid WETH pair on DexScreener. For the price of Flap tokens on the curve (Portal knows the price in ETH,
    GeckoTerminal does not know the curve)."""
    weth = NATIVE_WRAPPED.get(network)
    if not weth:
        return None
    end = time.time() + budget
    try:
        d = _gt(f"/simple/networks/{network}/token_price/{weth}", budget=min(budget, GT_FIRST), base=GT_API)
        v = (((d.get("data") or {}).get("attributes") or {}).get("token_prices") or {}).get(weth)
        if v not in (None, "") and float(v) > 0:
            return float(v)
    except Exception:
        pass
    try:
        key = ("native", network)
        if (hit := _cache_get(key)) is not None:
            return hit
        out = _ds_market(weth, network, budget=max(0.3, min(DS_BUDGET, end - time.time())))
        if out.get("price_usd"):
            _cache_put(key, out["price_usd"])
        return out.get("price_usd")
    except Exception:
        return None


def _gt_market(d, now=None):
    a = (d.get("data") or {}).get("attributes") or {}
    f = lambda v: float(v) if v not in (None, "") else None
    born = [_ts((p.get("attributes") or {}).get("pool_created_at")) for p in d.get("included") or []
            if p.get("type") == "pool"]
    born = [b for b in born if b]
    pools = [(f((p.get("attributes") or {}).get("reserve_in_usd")) or 0.0, (p.get("attributes") or {}).get("address"))
             for p in d.get("included") or [] if p.get("type") == "pool"]
    pools = [p for p in pools if p[1]]
    # liquidity -- sum of reserve_in_usd of pools from the response (top_pools), like DexScreener (sum of pairs); the token field
    # total_reserve_in_usd can be much smaller than the sum of its own pools ($CrawlScan 2026-10-08: $7.8K with a $43.8K pool).
    # Pools have no reserves -- fallback: total_reserve_in_usd
    reserves = [f((p.get("attributes") or {}).get("reserve_in_usd")) for p in d.get("included") or []
                if p.get("type") == "pool"]
    reserves = [r for r in reserves if r is not None]
    return {"name": a.get("name"), "ticker": a.get("symbol"), "price_usd": f(a.get("price_usd")),
            "mcap_usd": f(a.get("market_cap_usd")) or f(a.get("fdv_usd")), "fdv_usd": f(a.get("fdv_usd")),
            "liquidity_usd": sum(reserves) if reserves else f(a.get("total_reserve_in_usd")),
            "vol24h_usd": f((a.get("volume_usd") or {}).get("h24")),
            "age_days": round(((now or time.time()) - min(born)) / 86400, 1) if born else None,
            "pool": max(pools)[1] if pools else None, "source": "gt"}


def _ds(url, budget):
    """GET to DexScreener: retry on 429 and network errors within budget seconds, pauses 0.5 -> 1 s."""
    end, last, n = time.time() + budget, None, 0
    while True:
        left = end - time.time()
        if left <= 0.2:
            raise last or TimeoutError("dexscreener: no time left")
        try:
            return _get(url, left)
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429:
                raise
        except Exception as e:
            last = e
        n += 1
        time.sleep(min(0.5 * n, max(0.0, end - time.time() - 0.2)))


def _ds_market(tok, network, now=None, budget=DS_BUDGET):
    """Token market from DexScreener /tokens/v1/{chain}/{token} (up to 30 pairs): same shape as GT.
    liquidity and 24h volume -- sum over all pairs; price and market cap (marketCap, otherwise fdv) -- from the most liquid pair
    where the token is base (pair price = base price); age -- from the earliest pairCreatedAt. No pairs -- LookupError."""
    chain = DS_CHAIN[network]
    pairs = [p for p in _ds(f"{DS}/{chain}/{tok}", budget) or [] if p.get("chainId") == chain]
    if not pairs:
        raise LookupError("no pairs")
    same = (lambda x: (x or "").lower() == tok.lower()) if network == "robinhood" else (lambda x: x == tok)
    f = lambda v: float(v) if v not in (None, "") else None
    liq = lambda p: f((p.get("liquidity") or {}).get("usd")) or 0.0
    base = [p for p in pairs if same((p.get("baseToken") or {}).get("address"))]
    best = max(base, key=liq) if base else None
    me = (best or pairs[0]).get("baseToken" if best else "quoteToken") or {}
    if not best and not same(me.get("address")):
        me = {}
    born = [p["pairCreatedAt"] / 1000 for p in pairs if isinstance(p.get("pairCreatedAt"), (int, float))]
    vols = [f((p.get("volume") or {}).get("h24")) for p in pairs]
    txt = lambda v: (v or "").strip() or None
    return {"name": txt(me.get("name")), "ticker": txt(me.get("symbol")),
            "price_usd": f(best.get("priceUsd")) if best else None,
            "mcap_usd": (f(best.get("marketCap")) or f(best.get("fdv"))) if best else None,
            "fdv_usd": f(best.get("fdv")) if best else None,
            "liquidity_usd": sum(liq(p) for p in pairs) or None,
            "vol24h_usd": sum(v for v in vols if v) if any(v is not None for v in vols) else None,
            "age_days": round(((now or time.time()) - min(born)) / 86400, 1) if born else None,
            "pool": max(pairs, key=liq).get("pairAddress"), "source": "dexscreener"}


def _ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _pool(p, tok, network):
    """Pool from the /tokens/{tok}/pools response: address, dex, liquidity, creation time, token price in USD."""
    a, rel = p.get("attributes") or {}, p.get("relationships") or {}
    base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    same = (lambda x, y: x.lower() == y.lower()) if network == "robinhood" else (lambda x, y: x == y)
    side = "base" if same(base.split("_", 1)[-1], tok) else "quote"
    price = a.get(f"{side}_token_price_usd")
    return {"address": a.get("address"), "dex": ((rel.get("dex") or {}).get("data") or {}).get("id"),
            "liquidity": float(a.get("reserve_in_usd") or 0), "created": _ts(a.get("pool_created_at")),
            "price_usd": float(price) if price not in (None, "") else None}


def timeframe(age_h):
    """(GT timeframe, aggregate, label) by token age in hours; unknown age -- 15m."""
    if age_h is None:
        return TIMEFRAMES[2][1:]
    return next(t[1:] for t in TIMEFRAMES if t[0] is None or age_h < t[0])


def _candles(network, pool, tf, tok, end):
    """[[ts, o, h, l, c, v]] from oldest to newest (GT returns newest first)."""
    left = end - time.time()
    path = (f"/{network}/pools/{pool}/ohlcv/{tf[0]}?aggregate={tf[1]}&limit={CHART_LIMIT}"
            f"&currency=usd&token={tok}")
    rows = ((_gt(path, budget=min(GT_BUDGET, left)).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted([int(r[0])] + [float(x) for x in r[1:6]] for r in rows if len(r) >= 5)


def fetch_chart(token, network="robinhood", now=None):
    """Token price candles (USD) for the chart: {"pool", "dex", "timeframe", "candles", "price_usd",
    "age_h", "stitched", "failed"}, {} (GT does not know the token) or {"failed": True} (GT did not answer). No longer than CHART_BUDGET seconds.
    Pool -- the most liquid of the token's pools. Timeframe -- by age (TIMEFRAMES), age -- from
    creation of the earliest of the pools used. Solana: if the main pool is not the pump.fun curve
    but a curve pool exists (the token migrated), its candles before the first candle of the main pool go at the start
    (stitched). No candles received -- candles = [] and failed = True, the pool and price are returned anyway."""
    end = time.time() + CHART_BUDGET
    tok = token.lower() if network == "robinhood" else token
    try:
        data = _gt(f"/{network}/tokens/{tok}/pools", budget=min(GT_BUDGET, end - time.time())).get("data") or []
        pools = sorted((_pool(p, tok, network) for p in data), key=lambda p: -p["liquidity"])
    except Exception:
        return {"failed": True}
    pools = [p for p in pools if p["address"]]
    if not pools:
        return {}
    main = pools[0]
    curve = None
    if network == "solana" and main["dex"] != CURVE_DEX:
        curve = next((p for p in pools if p["dex"] == CURVE_DEX), None)
    born = [p["created"] for p in (main, curve) if p and p["created"]]
    age_h = ((now or time.time()) - min(born)) / 3600 if born else None
    tf = timeframe(age_h)
    failed = False
    try:
        candles = _candles(network, main["address"], tf, tok, end)
    except Exception:
        candles, failed = [], True
    stitched = False
    if curve and end - time.time() > 0.3:
        try:
            first = candles[0][0] if candles else float("inf")
            early = [c for c in _candles(network, curve["address"], tf, tok, end) if c[0] < first]
            stitched = bool(early)
            candles = early + candles
        except Exception:
            pass
    return {"pool": main["address"], "dex": main["dex"], "timeframe": tf[2], "candles": candles,
            "price_usd": main["price_usd"], "age_h": None if age_h is None else round(age_h, 1),
            "stitched": stitched, "failed": failed}
