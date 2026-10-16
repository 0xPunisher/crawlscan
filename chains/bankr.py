"""Bankr launchpad adapter on Robinhood Chain (only with BANKR_ENABLED=true; off by default).

Bankr (bankr.bot, an AI agent) launches tokens via Doppler: Airlock mints the token and immediately puts it into a
Uniswap V4 multicurve pool (no bonding curve); the pool lives in the shared V4 PoolManager (same as Pons), the pool hook is
the Doppler multicurve initializer (holds LP forever), the Bankr module is an anti-snipe fee (80% → ~1% over ~14 s)
plus part of every trade back into LP. Same chain as Pons (chains/robinhood.py): RPC, limiter, request counter,
receipt and block-time caches are shared.

Bankr token: address ends in ba3 (Bankr vanity; over 3M blocks all 293 Doppler tokens with ba3 are Bankr's,
other Doppler integrators use other suffixes), no RPC; then one eth_call Airlock.getAssetData(token):
integrator is Bankr. Launch, pool (PoolKey), dev, fee recipients and vesting come from the mint transaction receipt.
Dump impact and probably rug come from V4Quoter quotes (eth_call, no trades): for a fresh token the PoolManager holds
unsold tokens of the upper multicurve ranges, and a pool-balance formula understates the drop by tens of times.
Dev vesting (15% of supply on the token contract for most launches): the contract is not a holder, the remainder is attributed
to the beneficiary (the dev); the not-yet-unlocked part is excluded from q for impact (can't be sold now).
Research and measurements: scripts/probe_bankr.py (local), "Bankr" section in DEV_NOTES."""
import math, os, random, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

from chains import flap, priority
from chains import robinhood as ch
import detect as d

CHAIN = ch.CHAIN
REQUESTS = ch.REQUESTS            # one HTTP counter shared with the Robinhood adapter
PACK_WINDOW = ch.PACK_WINDOW
BUNDLE_WINDOW = ch.BUNDLE_WINDOW
RUG_SNIPERS = ch.RUG_SNIPERS
RPS = ch.RPS
HISTORY_PARALLEL = ch.HISTORY_PARALLEL

SUFFIX = "ba3"
AIRLOCK = "0xeb7c034704ef8dcd2d32324c1545f62fb4ad0862"      # Doppler Airlock: token mint and pool
INTEGRATOR = "0xf60633d02690e2a15a54ab919925f3d038df163e"   # Bankr integrator in getAssetData
INITIALIZER = "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"  # V4 multicurve initializer = pool hook, holds LP
HOOK = "0x9982538f41f2ae29ddb9d3d9307010052984fdbb"         # Bankr module: anti-snipe fee, part of the fee into LP
MIGRATOR = "0xba2f330edb16cd8056f5988d8ce19bbc63475a0e"     # migrator (empty: the pool doesn't migrate)
PROTOCOL = "0x21e2ce70511e4fe542a97708e89520471daa7a66"     # recipient of 5% of LP fees (protocol)
FEE_RECIPIENTS = {PROTOCOL,                                 # Bankr module fee recipients (1/3 and 2/3)
                  "0x042455f9990098e11592be1fbd72e6dc68419b13", "0x5f8da8f88ec81e27f2e22fcb9ca5d926c595e508"}
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"    # Uniswap V4Quoter (deployments/4663.md)
PM = ch.V4_POOL_MGR
ROUTERS = flap.ROUTERS | {  # pass-through contracts (token in and out in one transaction, balance ~0), CHOP 2026-10-08
    "0x0da5899dbb36bd741885d4228f04cd9c6304fc56",
    "0x6aa80dbbed9ae5ab45fbf61f9644fada3b29326e",
    "0x0005ea38eb0a69d1253508ebdbdb9ea8cb26b5ef",
    "0x36dc95f1f088e11c0066dc19173f745655002bc2",
    "0xec9aa0a6c2372131a0502aba6188269ab52cacb5",
    "0x48a097df16c7844a33b1c3d11ab353457846e13f",
    "0x5399d94d2cab7c252a6034042e1917a0e5e17a18",
}
MARKET = ROUTERS | {PM, INITIALIZER, HOOK}   # sent here = sold (detect.sellers)
INFRA = {AIRLOCK, INTEGRATOR, MIGRATOR} | FEE_RECIPIENTS
ZERO = "0x" + "0" * 40

SEL_ASSET_DATA = "0x1652e7b7"      # getAssetData(address) -> (numeraire, timelock, governance, migrator, initializer,
                                   #   poolOrHook, migrationPool, numTokensToSell, totalSupply, integrator)
SEL_AVAILABLE = "0x4c869795"       # computeAvailableVestedAmount(address): claimable from vesting right now
SEL_QUOTE = "0xaa9d21cb"           # quoteExactInputSingle(((c0,c1,fee,spacing,hooks),zeroForOne,amount,hookData))
SEL_TRY_AGGREGATE = "0xbce38bd7"   # Multicall3.tryAggregate(bool,(address,bytes)[])
INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"   # PoolManager: [id, c0, c1]
V4_SWAP = ch.V4_SWAP_TOPIC                                                           # [id, sender]: a0, a1, ...
BENEFICIARIES = "0x5be4f748347693e0500df872d81f7d96bce1b98e6f5adff0cfddfe3e9e415f20"  # initializer: [(address, share)]
HOOK_BENEFICIARIES = "0x0c90f8fcadd900399eb6c30bc91ec4531380b92bc2c4c364675528b1d30601e2"
VESTING = "0x6b467f0a76daac5283d2251b6e7660fed01ea99dbd70aa7092f3318731690c9d"      # token: [beneficiary, id], amount
WAD = 10 ** 18

# quotes for dump impact: q = share of circulating supply; each point is a pair (q, q × 1.01) = marginal price after selling q
QUOTE_GRID = (0.0005, 0.001, 0.002, 0.0035, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.2,
              0.3, 0.45, 0.65, 1.0)
QUOTE_REF = 1e-5                   # reference point: price before the sale
QUOTE_STEP = 101                   # q × 101 / 100
QUOTE_GAS = 150_000_000            # 40 quotes with the hook take ~25–30M gas (measured on CHOP/AUREON: 24M for 32)
ENTRY_TOL = 0.06                   # buyer received no more than the swap output (+6%: rounding, router tax) ...
ENTRY_MIN = 0.15                   # ... and no less than 15% (anti-snipe up to 80% + router fee up to ~5%)

# history: ~6–8 transfers per trade (PoolManager ↔ module ↔ initializer, routers), so Flap windows find almost no
# wallets (musebook: 2.4M transfers, 19K holders, the top entered over the token's whole life). Hence:
#   — up to FULL_MAX_LOGS (estimated from the first wave): the whole history in parallel within the scan (CHOP 95K: ~6 s, ~37 HTTP);
#   — more: a balance index, built in the background (full read, BACKGROUND_RPS); later scans use index + tail.
FULL_MAX_LOGS = 150_000            # read the whole history in the scan if the estimate is no larger
READ_CHUNKS = 16                   # first wave: up to this many block ranges in parallel (volume is estimated from them) ...
READ_CHUNK_MIN = 20_000            # ... but no shorter than this many blocks (the index tail is usually one request)
READ_TARGET = 8_000                # an overflowing range is split into sub-ranges of this many logs (by hint density)
WAVE_MAX = 3.0                     # seconds: with a cap, the first wave waits no longer than this (history is large; on dense
                                   # ranges RPC answers "Query timeout" after ~10 s: musebook waited 14 s for the wave)
READ_WORKERS = 12                  # concurrent getLogs requests (a 10K-log response takes ~1.5–2 s; RPS via the shared limiter)
READ_WORKERS_BG = 2                # in the background (BACKGROUND_RPS ~2, short pages ~0.5 s): no need for more in flight
READ_TARGET_BG = 3_000             # background (strict): logs per page, so the in-flight request is short when a live scan starts
READ_CHUNK_MIN_BG = 500            # ... and a background first-wave range is no shorter than this many blocks
INDEX_MAX = 20                     # tokens in the index (LRU) ...
INDEX_MAX_ENTRIES = 500_000        # ... and entries across all tokens (balances + entries + sells + edges; ~270 bytes per entry,
                                   # up to ~135 MB; musebook 2.4M transfers = 179K entries, 46 MB). Larger tokens are not indexed
INDEX_QUEUE_MAX = 5                # tokens waiting to be built (one built at a time, in order; repeats are not added)
ETA_WARMUP_S, ETA_WARMUP_REQ = 60, 50   # ETA from actual speed only after this many seconds of active reading and requests
MEMORY_WAIT_S = 30                 # seconds: memory at the threshold, no new build starts, recheck after this long
INDEX_RETRY_S = 600                # seconds: after a failure or a memory refusal, the build is not retried for this long
INDEX_TOO_LARGE_S = 6 * 3600       # ... after "more than INDEX_MAX_ENTRIES": this long
LOGS_PER_REQUEST = 3_000           # logs per HTTP on a full read (musebook: 2.43M / 820, with timeout splits), for ETA
EDGES_MAX = 64                     # incoming non-infrastructure senders per address in the index (bots have thousands)
for _k in ("INDEX_MAX_ENTRIES",):  # env BANKR_INDEX_MAX_ENTRIES
    try:
        globals()[_k] = max(0, int(os.environ.get("BANKR_" + _k, "").strip() or globals()[_k]))
    except ValueError:
        pass
_SUGGEST = re.compile(r"\[(0x[0-9a-fA-F]+),\s*(0x[0-9a-fA-F]+)\]")
_INDEX = {}                        # token -> {"head", "bal", "first", "sold", "edges", "logs", "n", "built"}; order = LRU
_INDEX_BUILDING = {}               # token -> {"started", "eta_s", "est_total", "stats"}, being built now
_INDEX_FAILED = {}                 # token -> (don't build until this time, reason: "too_large" | "memory" | "error")
_INDEX_QUEUE = {}                  # token -> {"block", "excluded", "est_total", "queued"}; order = queue
_INDEX_LOCK = threading.Lock()
_INDEX_CV = threading.Condition(_INDEX_LOCK)
_WORKER = []                       # build queue thread (single, started on first enqueue)
_GEN = [0]                         # drop_caches increments it: running builds are aborted
memory_high = lambda: False        # server: process memory near the threshold (memguard), the build is aborted
after_build = lambda: None         # server: after a build, gc and malloc_trim (memguard.trim) return pages to the OS

_STATE = {}    # token -> getAssetData (Bankr forever: the integrator never changes)
_LAUNCH = {}   # token -> launch: block, dev, PoolKey, fee recipients, vesting (never changes, kept forever)


def enabled():
    return os.environ.get("BANKR_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def index_enabled():
    """BANKR_INDEX_ENABLED (off by default): background holder index and build queue. When off, no
    builds at all and no index kept in memory; history larger than a normal scan gives TooActive (TOO ACTIVE)."""
    return os.environ.get("BANKR_INDEX_ENABLED", "false").strip().lower() in ("1", "true", "yes")


class TooActive(Exception):
    """Token history doesn't fit a normal scan and the background index is off: no verdict (TOO ACTIVE)."""

    def __init__(self, est_logs):
        super().__init__(est_logs)
        self.est_logs = est_logs


ACTIVE_TTL = 6 * 3600   # seconds: token was recently TOO ACTIVE, not added to the watchlist
ACTIVE_MAX = 2_000
_ACTIVE = {}            # token -> time of last TOO ACTIVE (order = recency)


def mark_active(token):
    with _INDEX_LOCK:
        _ACTIVE.pop(token, None)
        _ACTIVE[token] = time.time()
        while len(_ACTIVE) > ACTIVE_MAX:
            _ACTIVE.pop(next(iter(_ACTIVE)))


def recently_active(token):
    """Token was recently TOO ACTIVE (in process memory, ACTIVE_TTL)."""
    with _INDEX_LOCK:
        ts = _ACTIVE.get((token or "").lower())
    return ts is not None and time.time() - ts < ACTIVE_TTL


def active_info(token, launch, supply=None):
    """TOO ACTIVE: what is known exactly without transfer history: pool, pair, dev, vesting (total / unlocked) from
    contracts. One HTTP batch: remainder on the token contract and what's available to each beneficiary."""
    token = token.lower()
    st = _STATE.get(token) or {}
    supply = supply if supply is not None else ch.token_supply(token)
    vest = launch.get("vesting") or {}
    total, bens = sum(vest.values()), sorted(vest)
    res = ch.rpc_batch([_call(token, flap.SEL_BALANCE + _arg(token))] + [_call(token, SEL_AVAILABLE + _arg(b))
                                                                            for b in bens]) if bens else []
    in_contract = (_words(res[0]) or [0])[0] if res and res[0] else 0
    available = sum((_words(x) or [0])[0] if x else 0 for x in res[1:])
    left = min(in_contract, total)
    released = total - left
    unlocked = min(total, released + available)
    pair = st.get("numeraire") or (launch["pool_key"]["currency1"] if launch["token_is0"]
                                   else launch["pool_key"]["currency0"])
    is_eth = pair in (ch.WETH_ADDR, ZERO)
    return {"pool_id": launch["pool_id"], "pool_key": launch["pool_key"],
            "pair": {"kind": "eth" if is_eth else "token", "address": pair,
                     "symbol": "ETH" if is_eth else (ch.token_meta(pair).get("symbol") or None)},
            "dev": launch.get("deployer"), "creator": launch.get("creator"),
            "vesting": {"total": total, "unlocked": unlocked, "released": released, "in_contract": left,
                        "beneficiaries": {b: vest[b] for b in bens},
                        "total_share_supply": total / supply if supply else 0.0,
                        "unlocked_share_supply": unlocked / supply if supply else 0.0},
            "impact_source": None, "impact_curve": []}


def candidate(token):
    """Looks like Bankr by address (no RPC): flag on and ba3 suffix."""
    return enabled() and isinstance(token, str) and token.lower().endswith(SUFFIX)


_words, _addr, _call, _arg = flap._words, flap._addr, flap._call, flap._arg


def _signed(x):
    return x - (1 << 256) if x >= 1 << 255 else x


def detect(token):
    """Bankr token -> state (dict) or None. Without the ba3 suffix (or with the flag off): None, no requests.
    One eth_call Airlock.getAssetData: integrator is Bankr. Airlock returns zeros for a foreign address (no revert)."""
    if not candidate(token):
        return None
    token = token.lower()
    if token in _STATE:
        return _STATE[token]
    try:
        w = _words(ch.rpc("eth_call", [{"to": AIRLOCK, "data": SEL_ASSET_DATA + _arg(token)}, "latest"]))
    except RuntimeError as e:
        if "revert" in str(e).lower():
            return None
        raise
    if len(w) < 10 or _addr(w[9]) != INTEGRATOR:
        return None
    st = {"numeraire": _addr(w[0]), "migrator": _addr(w[3]), "initializer": _addr(w[4]),
          "tokens_to_sell": w[7], "total_supply": w[8], "integrator": _addr(w[9])}
    _STATE[token] = st
    return st


def _pairs(data):
    """Beneficiaries event: (offset, n, address, share, ...) -> [(address, share 0..1)]."""
    w = _words(data)
    n = w[1] if len(w) >= 2 else 0
    return [(_addr(w[2 + 2 * i]), w[3 + 2 * i] / WAD) for i in range(n) if 3 + 2 * i < len(w)]


def get_launch(token, st=None):
    """{"block", "tx", "curve", "deployer", "creator", "pool_id", "pool_key", "token_is0", "beneficiaries",
    "hook_beneficiaries", "vesting"} or None. Mint from 0x0: one getLogs by token address; mint receipt and
    transaction: one HTTP batch. PoolKey from PoolManager Initialize. Dev = recipient of the largest share of LP fees
    (excluding the protocol and Bankr module recipients); if none (all to the protocol), tx.from. vesting = {beneficiary:
    tokens} from the token's vesting events. Cached forever (launch and pool never change)."""
    token = token.lower()
    if token in _LAUNCH:
        return dict(_LAUNCH[token])
    flt = {"fromBlock": "0x0", "toBlock": "latest", "address": token,
           "topics": [ch.TRANSFER_TOPIC, ch.topic_for(ZERO)]}
    try:
        logs = ch.rpc("eth_getLogs", [flt])
    except RuntimeError:   # window too large for RPC: same path as Pons
        b = ch.first_log_block(0, ch.block_number(), address=token, topics=flt["topics"])
        logs = [] if b is None else ch.get_logs(b, b, address=token, topics=flt["topics"])
    if not logs:
        return None
    mint = min(logs, key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16)))
    h = mint["transactionHash"]
    rc, tx = ch.rpc_batch([("eth_getTransactionReceipt", [h]), ("eth_getTransactionByHash", [h])])
    key, ben, hook_ben, vest = None, [], [], {}
    for l in (rc or {}).get("logs", []):
        tp, addr = [t.lower() for t in l.get("topics") or []], l["address"].lower()
        if not tp:
            continue
        if addr == PM and tp[0] == INITIALIZE and len(tp) >= 4 and token in (ch.addr_from_topic(tp[2]),
                                                                                 ch.addr_from_topic(tp[3])):
            w = _words(l["data"])   # fee, tickSpacing (int24, sign-extended to 256 bits), hooks, sqrtPriceX96, tick
            key = {"currency0": ch.addr_from_topic(tp[2]), "currency1": ch.addr_from_topic(tp[3]), "fee": w[0],
                   "tick_spacing": _signed(w[1]), "hooks": _addr(w[2])}
            pool_id = tp[1]
        elif addr == INITIALIZER and tp[0] == BENEFICIARIES:
            ben = _pairs(l["data"])
        elif addr == HOOK and tp[0] == HOOK_BENEFICIARIES:
            hook_ben = _pairs(l["data"])
        elif addr == token and tp[0] == VESTING and len(tp) >= 2:
            b = ch.addr_from_topic(tp[1])
            vest[b] = vest.get(b, 0) + int(l["data"], 16)
    if key is None:
        return None
    creator = (tx or {}).get("from", "").lower() or None
    fees = {a for a, _ in hook_ben} | FEE_RECIPIENTS
    devs = sorted((s, a) for a, s in ben if a not in fees)
    deployer = devs[-1][1] if devs else creator
    res = {"block": int(mint["blockNumber"], 16), "tx": h, "curve": PM, "deployer": deployer, "creator": creator,
           "pool_id": pool_id, "pool_key": key, "token_is0": key["currency0"] == token,
           "beneficiaries": ben, "hook_beneficiaries": hook_ben, "vesting": vest}
    _LAUNCH[token] = res
    return dict(res)


def _sets(token, launch):
    """(market, excluded): market (PoolManager, initializer, Bankr module, routers) and all non-holders: market,
    token contract (vesting), Airlock, integrator, migrator, fee recipients (except the dev), chain infrastructure."""
    dev = launch.get("deployer")
    fees = ({a for a, _ in launch.get("beneficiaries") or []} | {a for a, _ in launch.get("hook_beneficiaries") or []}
            | FEE_RECIPIENTS) - {dev}
    return MARKET, ch.INFRA | INFRA | MARKET | {token} | fees


def _quote_data(key, zero_for_one, amount):
    words = [0x20, int(key["currency0"], 16), int(key["currency1"], 16), key["fee"], key["tick_spacing"] % (1 << 256),
             int(key["hooks"], 16), int(zero_for_one), amount, 0x100, 0]
    return SEL_QUOTE + "".join(f"{w:064x}" for w in words)


def _encode_try_aggregate(calls):
    """calldata Multicall3.tryAggregate(false, (address target, bytes callData)[])."""
    agg = flap._encode_aggregate(calls)          # aggregate((address,bytes)[]): same array after the selector
    return SEL_TRY_AGGREGATE + f"{0:064x}" + f"{64:064x}" + agg[10 + 64:]


def _decode_try_aggregate(res):
    """tryAggregate response: (bool success, bytes returnData)[] -> [returnData or None]."""
    h = res[2:]
    word = lambda i: int(h[64 * i:64 * i + 64], 16)
    arr = word(0) // 32
    n = word(arr)
    out = []
    for k in range(n):
        el = arr + 1 + word(arr + 1 + k) // 32
        ok, off = word(el), word(el + 1) // 32
        ln = word(el + off)
        out.append("0x" + h[64 * (el + off + 1):64 * (el + off + 1) + 2 * ln] if ok else None)
    return out


def _quote_call(launch, circulating):
    """(eth_call of quotes in one Multicall3, [q]): reference point and QUOTE_GRID, each with q and q × 1.01."""
    qs = []
    for f in (QUOTE_REF,) + QUOTE_GRID:
        q = max(1, int(circulating * f))
        qs += [q, q * QUOTE_STEP // 100]
    zf1 = launch["token_is0"]          # selling the token: token -> pair
    calls = [(V4_QUOTER, _quote_data(launch["pool_key"], zf1, q)) for q in qs]
    return ("eth_call", [{"to": flap.MULTICALL, "data": _encode_try_aggregate(calls), "gas": hex(QUOTE_GAS)},
                         "latest"]), qs


def impact_curve(qs, raw):
    """Quotes -> [(q, price drop after selling q)] ascending by q; [] means no quotes.
    Drop = marginal price after selling q vs. price before the sale: 1 − p(q) / p0, p(q) = Δout / Δq."""
    if not raw:
        return []
    vals = []
    for r in _decode_try_aggregate(raw):
        w = _words(r) if r else []
        vals.append(w[0] if w else None)
    pr = []
    for i in range(0, len(qs) - 1, 2):
        a, b = vals[i], vals[i + 1]
        pr.append((qs[i], (b - a) / (qs[i + 1] - qs[i]) if a is not None and b is not None and b >= a else None))
    p0 = pr[0][1]
    if not p0:
        return []
    out, worst = [], 0.0
    for q, p in pr[1:]:
        if p is None:
            continue
        worst = max(worst, min(1.0, max(0.0, 1 - p / p0)))   # monotonic: selling more never gives a higher price
        out.append((q, worst))
    return out


def _read_all(token, lo, hi, sink, cap=None, until=None, stats=None, est=None):
    """All token transfers in [lo, hi] in parallel -> (logs, fully read). sink(transfers) is called under a lock, per page.
    First wave: up to READ_CHUNKS equal ranges (no shorter than READ_CHUNK_MIN blocks); a "more than 10K logs" response with hint [lo, b]: read [lo, b],
    split the rest by hint density into sub-ranges of ~READ_TARGET logs (in parallel); without a hint, in half.
    cap: volume estimate (read so far + density × blocks of overflowing first-wave ranges; grows as responses arrive)
    above cap, or the first wave didn't answer within WAVE_MAX: stop, not fully read. until: deadline (unix time); past it, stop, not fully read.
    stats (dict) receives "est_total": estimate of the whole history (estimate from answered first-wave ranges over all ranges);
    along the way, "req" (getLogs HTTP of this read) and "logs".
    Strict background (priority.background(strict=True): index build): short pages (READ_TARGET_BG logs,
    first wave split by estimate est (total logs) into as many ranges): the in-flight request when a live scan starts
    is short; a response arriving during a live scan is processed only after it ends.
    sink may raise StopRead: reading is aborted (not fully read)."""
    if lo > hi:
        return 0, True
    lock, done = threading.Lock(), threading.Event()
    st = {"pending": 0, "logs": 0, "est": 0, "wave": 0, "waves": 0, "answered": 0, "stop": False, "req": 0}
    bg, strict = priority.is_background(), priority.is_strict()
    target = READ_TARGET_BG if strict else READ_TARGET
    pool = ThreadPoolExecutor(max_workers=READ_WORKERS_BG if bg else READ_WORKERS,
                              thread_name_prefix=f"{priority.BG}-bankr" if bg else "bankr")

    def submit(a, b, wave=False):
        with lock:
            st["pending"] += 1
            st["wave"] += wave
            st["waves"] += wave
        pool.submit(run, a, b, wave)

    def finish(wave, est):
        with lock:
            st["est"] += est
            st["wave"] -= wave
            st["answered"] += wave
            if stats is not None and wave and st["answered"] >= max(5, st["waves"] // 20):
                stats["est_live"] = int(st["est"] * st["waves"] / st["answered"])   # from the answered ranges of the wave
            if cap is not None and (st["est"] > cap or st["logs"] > cap):
                st["stop"] = True
            st["pending"] -= 1
            if st["pending"] == 0 or st["stop"]:
                done.set()

    def run(a, b, wave):
        if strict:
            with priority.background(strict=True):   # pool tls: strict wait for live scans before every request
                return _run(a, b, wave)
        return _run(a, b, wave)

    def _run(a, b, wave):
        est = 0
        try:
            if st["stop"] or (until is not None and time.time() > until):
                st["stop"] = True
                return
            flt = {"fromBlock": hex(a), "toBlock": hex(b), "address": token, "topics": [ch.TRANSFER_TOPIC]}
            with lock:
                st["req"] += 1
                if stats is not None:
                    stats["req"] = st["req"]
            try:
                logs = ch.rpc("eth_getLogs", [flt])
            except RuntimeError as e:
                m = _SUGGEST.search(str(e))
                if m and int(m.group(1), 16) == a and a <= int(m.group(2), 16) < b:
                    c = int(m.group(2), 16)
                    dens = flap.PAGE_LOGS / (c - a + 1)
                    est = int(dens * (b - a + 1))
                    k = max(1, math.ceil(dens * (b - c) / target))
                    step = (b - c) // k + 1
                    j = max(1, min(c - a + 1, math.ceil(flap.PAGE_LOGS / target))) if strict else 1   # [a, c] ~ PAGE_LOGS logs
                    sp = (c - a) // j + 1
                    for i in range(j):
                        if a + i * sp <= c:
                            submit(a + i * sp, min(c, a + (i + 1) * sp - 1))
                    for i in range(k):
                        x = c + 1 + i * step
                        if x <= b:
                            submit(x, min(b, x + step - 1))
                elif b > a and ch._is_window_error(str(e).lower()):
                    submit(a, (a + b) // 2)
                    submit((a + b) // 2 + 1, b)
                else:
                    raise
                return
            if len(logs) >= flap.PAGE_LOGS and b > a:   # response may have been truncated: split in half
                submit(a, (a + b) // 2)
                submit((a + b) // 2 + 1, b)
                est = len(logs) * 2
                return
            if strict:
                priority.wait_turn()   # a live scan started while the request was in flight: parse and write after it
            page = [t for t in map(flap._transfer, logs) if t]
            est = len(page)
            with lock:
                st["logs"] += len(page)
                if stats is not None:
                    stats["logs"] = st["logs"]
                if not st["stop"]:
                    sink(page)
        except StopRead:
            st["stop"] = True
        except Exception:
            st["stop"] = True
            raise
        finally:
            finish(wave, est if wave else 0)

    chunks = min(READ_CHUNKS, (hi - lo + 1) // READ_CHUNK_MIN)
    if strict and est:   # background: short ranges by estimate right away (huge ranges hit "Query timeout" after ~10 s)
        chunks = min(math.ceil(est / target), (hi - lo + 1) // READ_CHUNK_MIN_BG)
    step = (hi - lo) // max(1, chunks) + 1
    with lock:
        st["pending"] += 1   # hold while handing out the first wave
    starts = [lo + i * step for i in range(max(1, chunks) if strict and est else READ_CHUNKS) if lo + i * step <= hi]
    if strict and est:
        random.shuffle(starts)   # first wave is a random sample of ranges: on-the-fly volume estimate without bias
    for a in starts:
        submit(a, min(hi, a + step - 1), wave=True)
    finish(False, 0)
    if cap is not None:   # first wave is slow, so history is large: don't wait (without its responses the estimate has nothing to rely on)
        t_wave = time.time() + WAVE_MAX
        while not done.wait(0.05):
            with lock:
                if st["wave"] > 0 and time.time() > t_wave:
                    st["stop"] = True
                    break
            if until is not None and time.time() > until:
                break
    if not st["stop"]:
        done.wait(None if until is None else max(0.0, until - time.time()))
    pool.shutdown(wait=False, cancel_futures=True)
    with lock:
        if stats is not None and st["answered"]:
            stats["est_total"] = int(st["est"] * st["waves"] / st["answered"])
        complete = st["pending"] == 0 and not st["stop"]
        if not complete:
            st["stop"] = True
        return st["logs"], complete


class StopRead(Exception):
    """sink aborts _read_all (index build: memory, cache drop, entry limit)."""


def _key(t):
    return t["block"], t["log_index"]


# in the index a transfer is stored as a tuple (block, log_index, from, to, amount, tx, ts): a third of a dict's size;
# addresses are sys.intern'ed (one string per address across all index fields)
def _pack(t):
    return (t["block"], t["log_index"], sys.intern(t["frm"]), sys.intern(t["to"]), t["amount"], t["tx"], t.get("ts"))


def _unpack(p):
    t = {"frm": p[2], "to": p[3], "amount": p[4], "tx": p[5], "block": p[0], "log_index": p[1]}
    if p[6] is not None:
        t["ts"] = p[6]
    return t


def _index_new():
    return {"bal": {}, "first": {}, "sold": {}, "edges": {}, "head": 0, "logs": 0, "n": 0, "built": time.time()}


def _index_apply(ix, transfers, excluded):
    """Transfers -> index (any order: pages arrive in parallel): balances; first entry of an address (earliest
    received transfer); first sell (earliest transfer to the market); incoming from non-infrastructure addresses
    (direct transfers between holders and distributors, up to EDGES_MAX senders per address, the earliest).
    ix["n"] = entries in the index (balances + entries + sells + edges): memory limit INDEX_MAX_ENTRIES."""
    bal, first, sold, edges = ix["bal"], ix["first"], ix["sold"], ix["edges"]
    n = 0
    for t in transfers:
        f, to = sys.intern(t["frm"]), sys.intern(t["to"])
        k = (t["block"], t["log_index"])
        n += (f not in bal) + (to not in bal)
        bal[f] = bal.get(f, 0) - t["amount"]
        bal[to] = bal.get(to, 0) + t["amount"]
        if to in excluded:
            if to in MARKET and f not in MARKET:
                cur = sold.get(f)
                if cur is None or k < cur[:2]:
                    n += cur is None
                    sold[f] = _pack(t)
            continue
        cur = first.get(to)
        if cur is None or k < cur[:2]:
            n += cur is None
            first[to] = _pack(t)
        if f not in excluded:
            e = edges.get(to)
            if e is None:
                e = edges[to] = {}
            cur = e.get(f)
            if cur is not None and k < cur[:2] or cur is None and len(e) < EDGES_MAX:
                n += cur is None
                e[f] = _pack(t)
    ix["logs"] += len(transfers)
    ix["n"] += n


def _index_size():
    return sum(v["n"] for v in _INDEX.values())


def _index_put(token, ix):
    """Don't call under _INDEX_LOCK. LRU by tokens (INDEX_MAX) and by entries across all tokens (INDEX_MAX_ENTRIES):
    the oldest are evicted; a token above the entry limit is not kept."""
    with _INDEX_LOCK:
        _INDEX.pop(token, None)
        if ix["n"] > INDEX_MAX_ENTRIES:
            return False
        _INDEX[token] = ix
        while len(_INDEX) > INDEX_MAX or _index_size() > INDEX_MAX_ENTRIES:
            _INDEX.pop(next(iter(_INDEX)))
        return True


def drop_caches():
    """Memory protection (server, memguard): drop indexes and abort running builds."""
    with _INDEX_LOCK:
        _INDEX.clear()
        _GEN[0] += 1


def build_index(token, launch_block, excluded, est_total=None):
    """Token index: the whole history since launch (strict background: BACKGROUND_RPS, READ_WORKERS_BG, short pages, paused
    during live scans). Computed page by page; not all transfers are kept in memory (musebook: 2.4M). Aborted with
    (False, reason): more than INDEX_MAX_ENTRIES entries ("too_large"), memory near the threshold or drop_caches
    ("memory"). -> (ready, reason)."""
    head = ch.block_number()
    ix = _index_new()
    with _INDEX_LOCK:
        gen = _GEN[0]
        job = _INDEX_BUILDING.get(token)
    why = []
    stats = job["stats"] if job is not None else {}

    def sink(page):
        if ix["n"] > INDEX_MAX_ENTRIES:
            why.append("too_large")
            raise StopRead
        if _GEN[0] != gen or memory_high():
            why.append("memory")
            raise StopRead
        _index_apply(ix, page, excluded)

    n, complete = _read_all(token, launch_block, head, sink, stats=stats, est=est_total)
    if not complete:
        return False, (why or ["error"])[0]
    ix.update(head=head, built=time.time())
    if not _index_put(token, ix):
        return False, "too_large"
    return True, None


def index_eta(est_total):
    """Seconds to build from the history estimate (without pauses for live scans): HTTP ≈ logs / LOGS_PER_REQUEST under
    BACKGROUND_RPS."""
    rps = priority.background_rps() or RPS
    return int((est_total or FULL_MAX_LOGS) / LOGS_PER_REQUEST / rps) + 5


def _job_left(job, now):
    """Under _INDEX_LOCK: (seconds until the running build finishes, slowdown vs. estimate ≥ 1). Volume: estimate from
    answered first-wave ranges (random sample), otherwise the scan probe. Speed: from active time
    (pauses for live scans don't count: they can't be predicted); while little has been read, from the index_eta estimate."""
    st = job["stats"]
    est = st.get("est_live") or job.get("est_total") or FULL_MAX_LOGS
    logs, req = st.get("logs", 0), st.get("req", 0)
    active = (now - job["started"]) - (priority.live_seconds() - job.get("live0", 0.0))
    if active < ETA_WARMUP_S or logs <= 0 or req < ETA_WARMUP_REQ:   # first come the overflowing ranges (hints without logs)
        return index_eta(est) * max(0.0, 1 - logs / est) if logs else max(0, index_eta(est) - max(0, active)), 1.0
    rate = logs / active                                     # logs per second of active reading
    slow = max(1.0, index_eta(logs) / active) if logs else 1.0
    return max(0, est - logs) / rate, slow


def _index_start(token, launch_block, excluded, est_total=None):
    """Queue the token index for building (thread recheck-bankr-index builds one at a time, in order)
    -> "building" | "queued" | "queue_full" | None (already exists or recently failed: not needed)."""
    if not index_enabled():
        return None
    now = time.time()
    with _INDEX_CV:
        failed = _INDEX_FAILED.get(token)
        if token in _INDEX or failed and failed[0] > now:
            return None
        if token in _INDEX_BUILDING:
            return "building"
        if token in _INDEX_QUEUE:
            return "queued"
        if len(_INDEX_QUEUE) >= INDEX_QUEUE_MAX:
            return "queue_full"
        _INDEX_QUEUE[token] = {"block": launch_block, "excluded": excluded, "est_total": est_total, "queued": now}
        if not _WORKER or not _WORKER[0].is_alive():
            _WORKER[:] = [threading.Thread(target=_index_worker, name=f"{priority.BG}-bankr-index", daemon=True)]
            _WORKER[0].start()
        _INDEX_CV.notify_all()
        return "queued"


def _index_worker():
    """Build queue: one at a time, in order. Memory at the threshold: nothing new starts (wait MEMORY_WAIT_S);
    all requests are strict background (no live scans, BACKGROUND_RPS)."""
    with priority.background(strict=True):
        while True:
            with _INDEX_CV:
                while not _INDEX_QUEUE:
                    _INDEX_CV.wait()
            if memory_high():
                with _INDEX_CV:
                    _INDEX_CV.wait(MEMORY_WAIT_S)
                continue
            priority.wait_turn()   # don't start during a live scan
            with _INDEX_CV:
                if not _INDEX_QUEUE:
                    continue
                token = next(iter(_INDEX_QUEUE))
                job = _INDEX_QUEUE.pop(token)
                if token in _INDEX:
                    continue
                _INDEX_BUILDING[token] = {"started": time.time(), "eta_s": index_eta(job["est_total"]),
                                          "est_total": job["est_total"], "stats": {}, "live0": priority.live_seconds()}
            _index_run(token, job)


def _index_run(token, job):
    t0, ok, why = time.time(), False, "error"
    r0 = ch.REQUESTS[0]
    try:
        ok, why = build_index(token, job["block"], job["excluded"], job["est_total"])
        ix = _INDEX.get(token) or {}
        print(f"bankr: index {token} {'built' if ok else 'stopped (' + why + ')'} in {time.time() - t0:.0f}s"
              + (f", {ix.get('logs')} logs, {ix.get('n')} entries" if ok else "")
              + f", ~{ch.REQUESTS[0] - r0} HTTP (all threads), queue {len(_INDEX_QUEUE)}", flush=True)
    except Exception as e:
        print(f"bankr: index {token} failed: {type(e).__name__}: {e}", flush=True)
    finally:
        try:
            after_build()
        except Exception:
            pass
        with _INDEX_LOCK:
            _INDEX_BUILDING.pop(token, None)
            if not ok:
                _INDEX_FAILED[token] = (time.time() + (INDEX_TOO_LARGE_S if why == "too_large" else INDEX_RETRY_S),
                                        why)


def index_status(token):
    """{"state": "ready" | "building" | "queued" | "queue_full" | "too_large" | "unavailable" | "none", "eta_s"[,
    "position"]}: for the site (rescan once the index is ready) and the partial-scan message. queued: position = place in
    the queue (1 = next), eta_s = remainder of the running build + builds ahead and its own (with the running build's slowdown)."""
    token = token.lower()
    now = time.time()
    with _INDEX_LOCK:
        if token in _INDEX:
            return {"state": "ready", "eta_s": 0}
        job = _INDEX_BUILDING.get(token)
        if job:
            return {"state": "building", "eta_s": max(15, int(_job_left(job, now)[0]))}
        if token in _INDEX_QUEUE:
            left, slow = 0.0, 1.0
            for b in _INDEX_BUILDING.values():
                l, slow = _job_left(b, now)
                left += l
            order = list(_INDEX_QUEUE)
            pos = order.index(token) + 1
            left += sum(index_eta(_INDEX_QUEUE[t]["est_total"]) * slow for t in order[:pos])
            if pos == 1 and not _INDEX_BUILDING and not memory_high():   # nobody ahead: starts after live scans
                return {"state": "building", "eta_s": max(15, int(left))}
            return {"state": "queued", "eta_s": max(15, int(left)), "position": pos}
        failed = _INDEX_FAILED.get(token)
        if failed and failed[0] > now:
            return {"state": "too_large" if failed[1] == "too_large" else "unavailable", "eta_s": None}
        if len(_INDEX_QUEUE) >= INDEX_QUEUE_MAX:
            return {"state": "queue_full", "eta_s": None}
    return {"state": "none", "eta_s": None}


def _from_index(token, ix, supply, excluded):
    """Under _INDEX_LOCK: (top-20 transfers for the engine, base, infrastructure balances). Transfers from the index: first
    entry, first sell, incoming from non-infrastructure addresses, no requests (bots' getLogs run to hundreds of thousands)."""
    bal = ix["bal"]
    held = {a: v for a, v in bal.items() if v > 0 and a not in excluded}
    top = sorted(held, key=lambda a: -held[a])[:flap.HOLDERS_N]
    out = {}
    for a in top:
        for p in [ix["first"].get(a), ix["sold"].get(a)] + list(ix["edges"].get(a, {}).values()):
            if p:
                out[(p[5], p[1])] = p
    base = {"supply": supply, "circulating": sum(held.values()), "holders_total": len(held), "balances": held}
    return [_unpack(p) for p in sorted(out.values())], base, {a: bal.get(a, 0) for a in excluded if a != ZERO}


def history(token, launch_block, supply, excluded, deadline=None):
    """Transfers for a scan: (transfers, base or None, info, infrastructure balances or None), the flap.history contract.
    indexed: index exists; tail after its head, exact balances (top_exact); top-20 transfers from the index (entry,
    sells, direct transfers, distributors), no requests. full: history no larger than FULL_MAX_LOGS: all of it, in parallel
    (base None, as for Pons); the index for later scans is built from it too. Otherwise Flap windows (limited) and a background
    index build."""
    until = (deadline - flap.HISTORY_RESERVE) if deadline is not None else None
    t0 = time.time()
    head = ch.block_number()
    indexing = index_enabled()
    with _INDEX_LOCK:
        ix = _INDEX.get(token) if indexing else None
        start = ix["head"] if ix else None
    if ix is not None:
        tail = []
        n, complete = _read_all(token, start + 1, head, tail.extend, until=until)
        if complete:
            with _INDEX_LOCK:   # a parallel scan may have applied part of the tail already: only blocks after the head
                cur = ix["head"]
                _index_apply(ix, [t for t in tail if t["block"] > cur], excluded)
                ix["head"] = max(cur, head)
                age = round(time.time() - ix["built"])
                transfers, base, infra = _from_index(token, ix, supply, excluded)
                logs = ix["logs"]
            _index_put(token, ix)
            info = {"mode": "indexed", "logs": logs, "tail_logs": n, "index_age_s": age, "holder_logs": len(transfers),
                    "holder_logs_complete": True, "top_exact": True, "coverage": 1.0,
                    "seconds": {"tail": round(time.time() - t0, 1)}}
            return transfers, base, info, infra
    trs, stats = [], {}
    cap = min(FULL_MAX_LOGS, ch.SCAN_MAX_LOGS) if ch.SCAN_MAX_LOGS else FULL_MAX_LOGS   # no higher than the Pons cap
    n, complete = _read_all(token, launch_block, head, trs.extend, cap=cap, until=until, stats=stats)
    if complete:
        trs.sort(key=_key)
        if indexing:   # index off: don't keep it in memory (the next scan reads everything again)
            ix = _index_new()
            _index_apply(ix, trs, excluded)
            ix["head"] = head
            _index_put(token, ix)
        return trs, None, {"mode": "full", "logs": n, "seconds": {"read": round(time.time() - t0, 1)}}, None
    probe_s = round(time.time() - t0, 1)
    trs = None   # don't keep what the probe read: Flap windows read again
    est = max(stats.get("est_total") or 0, n, cap + 1)
    if not indexing:   # doesn't fit a normal scan, no index: no verdict (TOO ACTIVE)
        print(f"bankr: {token} too active for a full scan (~{est} logs, probe {probe_s}s)", flush=True)
        raise TooActive(est)
    started = _index_start(token, launch_block, excluded, est) in ("building", "queued")
    transfers, base, info, infra = flap.history(token, launch_block, supply, excluded, deadline)
    ixs = index_status(token)
    info = dict(info, index=ixs["state"], index_eta_s=ixs["eta_s"], index_started=started, full_probe_logs=n,
                full_probe_s=probe_s, est_logs=est)
    return transfers, base, info, infra


def token_facts(token, launch, deadline=None):
    """Facts for the engine (shared chain contract) + "impact_curve", "locked" and "bankr" (launchpad data).
    History via history(): balance index + tail, the whole history in parallel (up to FULL_MAX_LOGS) or Flap windows (limited).
    Vesting: the remainder on the token contract goes to the dev (to beneficiaries pro rata to allocation), counted in circulating supply and in the
    dev's share; "locked" = not yet unlocked (can't be sold now), the engine subtracts it from q for impact.
    V4Quoter quotes (impact) and vesting availability: one HTTP batch. reserve = PoolManager balance (only for
    info and cross-checking with GT): impact comes from quotes, not from it."""
    token = token.lower()
    st = _STATE.get(token) or detect(token) or {}
    supply = ch.token_supply(token)
    market, excluded = _sets(token, launch)
    transfers, base, hist, infra_bal = history(token, launch["block"], supply, excluded, deadline)
    if base is None:
        base = d.supply_base(transfers, supply, excluded)
        infra = {}
        for t in transfers:
            for a, sign in ((t["to"], 1), (t["frm"], -1)):
                if a in (token, PM):
                    infra[a] = infra.get(a, 0) + sign * t["amount"]
    else:
        infra = infra_bal or {}
        base = dict(base, balances=dict(base["balances"]))
    in_contract = max(0, infra.get(token, 0))
    reserve = max(0, infra.get(PM, 0))
    vest = launch.get("vesting") or {}
    total = sum(vest.values())
    bens = sorted(vest)
    devw = launch.get("deployer")
    owners = sorted(set(bens) | ({devw} if devw else set()))
    if hist.get("mode") == "windowed":
        missing = [b for b in owners if b not in base["balances"]]
        if missing:   # incomplete history: dev/beneficiary may not be among candidates, fetch the wallet balance separately
            for b, v in flap.balances(token, missing).items():   # (windowed circulating supply = supply − infrastructure:
                if v > 0:                                         #  this balance is already in it)
                    base["balances"][b] = v
                    base["holders_total"] += 1
    wallet = {a: base["balances"].get(a, 0) for a in owners}   # in wallets (before attributed vesting): exact
    left = min(in_contract, total)   # the token contract may also hold tokens sent beyond vesting; those aren't the dev's
    remaining = {b: left * vest[b] // total for b in bens} if total else {}
    for b, v in remaining.items():   # vesting remainder goes to the dev: in circulating supply and in their balance
        if v <= 0:
            continue
        if b not in base["balances"]:
            base["holders_total"] += 1
        base["balances"][b] = base["balances"].get(b, 0) + v
        base["circulating"] += v
    qcall, qs = _quote_call(launch, base["circulating"])
    res = ch.rpc_batch([qcall] + [_call(token, SEL_AVAILABLE + _arg(b)) for b in bens])
    curve = impact_curve(qs, res[0])
    available = {b: (_words(r) or [0])[0] if r else 0 for b, r in zip(bens, res[1:])}
    locked = {b: max(0, remaining[b] - available.get(b, 0)) for b in bens if remaining.get(b, 0) > 0}
    released = total - left
    unlocked = min(total, released + sum(available.values()))
    pair = st.get("numeraire") or (launch["pool_key"]["currency1"] if launch["token_is0"]
                                   else launch["pool_key"]["currency0"])
    is_eth = pair in (ch.WETH_ADDR, ZERO)
    pair_info = {"kind": "eth" if is_eth else "token", "address": pair,
                 "symbol": "ETH" if is_eth else (ch.token_meta(pair).get("symbol") or None)}
    circ = base["circulating"]
    dev = None
    if devw:   # dev share from contracts: wallet (balanceOf / full history) + vesting; sellable now = without locked
        dev = {"wallet": devw, "wallet_balance": wallet.get(devw, 0), "vesting": remaining.get(devw, 0),
               "sellable": wallet.get(devw, 0) + min(available.get(devw, 0), remaining.get(devw, 0)),
               "share_supply": (wallet.get(devw, 0) + remaining.get(devw, 0)) / supply if supply else 0.0}
    info = {"pool_id": launch["pool_id"], "pool_key": launch["pool_key"], "pair": pair_info,
            "dev": launch.get("deployer"), "creator": launch.get("creator"),
            "fee_recipients": sorted(excluded & ({a for a, _ in launch.get("beneficiaries") or []}
                                                 | {a for a, _ in launch.get("hook_beneficiaries") or []}
                                                 | FEE_RECIPIENTS)),
            "vesting": {"total": total, "unlocked": unlocked, "released": released, "in_contract": left,
                        "locked": sum(locked.values()), "beneficiaries": {b: vest[b] for b in bens},
                        "total_share_supply": total / supply if supply else 0.0,
                        "unlocked_share_supply": unlocked / supply if supply else 0.0},
            "dev_holding": dev,
            "impact_source": "v4_quoter" if curve else None,
            "impact_curve": [[q / circ if circ else 0.0, i] for q, i in curve],
            "pm_reserve": reserve, "infra": sorted({token, PM, INITIALIZER, HOOK, AIRLOCK}), "history": hist}
    return {"supply": supply, "transfers": transfers, "excluded": excluded, "market": market,
            "reserve": reserve, "reserve_ok": bool(curve), "base": base, "impact_curve": curve,
            "locked": locked, "dev": dev, "bankr": info}


def _user_swaps(logs, launch):
    """V4 Swaps of the token's pool not from the hook/initializer (their own fee swaps) with token output:
    [(tokens from the swap, quote side)]."""
    pid, is0 = launch["pool_id"], launch["token_is0"]
    out = []
    for l in logs:
        tp = [t.lower() for t in l.get("topics") or []]
        if l["address"].lower() != PM or len(tp) < 3 or tp[0] != V4_SWAP or tp[1] != pid:
            continue
        if ch.addr_from_topic(tp[2]) in (HOOK, INITIALIZER):
            continue
        w = _words(l["data"])
        if len(w) < 2:
            continue
        a0, a1 = _signed(w[0]), _signed(w[1])
        tok, quote = (a0, a1) if is0 else (a1, a0)
        if tok > 0:
            out.append((tok, abs(quote)))
    return out


def classify_entries(token, transfers, wallets, chunk=50):
    """First receipt of the token by each wallet (same contract as ch.classify_entries).
    buy: the transaction receipt has a token-pool swap with token output (not the hook's own fee swap): buys
    in the first seconds with the hook's anti-snipe fee are buys too. eth_in = quote side of the swap (ETH pair), if
    the wallet received 15% to 106% of the swap output (anti-snipe up to 80%, router fee).
    vesting: vesting beneficiary (dev); their share is allocated at launch (entry = the launch transaction, even if the tokens
    are still on the contract). fees: first entry to the dev from the initializer/module (LP fees). Otherwise transfer."""
    token = token.lower()
    launch = _LAUNCH.get(token) or get_launch(token) or {}
    st = _STATE.get(token) or {}
    eth_pair = (st.get("numeraire") or ch.WETH_ADDR) in (ch.WETH_ADDR, ZERO)
    vest = set(launch.get("vesting") or {})
    dev = launch.get("deployer")
    want = {w.lower() for w in wallets}
    first = {}
    for t in transfers:
        if t["to"] in want and t["to"] not in first:
            first[t["to"]] = t
    need = sorted({t["tx"] for w, t in first.items() if w not in vest and t["frm"] != token})
    receipts = ch.receipt_logs(need, chunk) if need else {}
    out = {}
    for w in want:
        t = first.get(w)
        if w in vest or (t is not None and t["frm"] == token):
            out[w] = {"kind": "vesting", "tx": launch.get("tx"), "block": launch.get("block"), "via": token,
                      "eth_in": None}
            continue
        if t is None:   # entry not found (windowed history not fully read): no guessing
            out[w] = {"kind": None, "tx": None, "block": None, "via": None, "eth_in": None}
            continue
        if w == dev and t["frm"] in (INITIALIZER, HOOK):
            out[w] = {"kind": "fees", "tx": t["tx"], "block": t["block"], "via": t["frm"], "eth_in": None}
            continue
        swaps = _user_swaps(receipts.get(t["tx"], []), launch) if launch else []
        eth_in = None
        if swaps and eth_pair and t["amount"]:
            ok = [(g, q) for g, q in swaps if ENTRY_MIN * g <= t["amount"] <= (1 + ENTRY_TOL) * g]
            if ok:
                eth_in = min(ok, key=lambda s: abs(s[0] - t["amount"]))[1]
        out[w] = {"kind": "buy" if swaps else "transfer", "tx": t["tx"], "block": t["block"], "via": t["frm"],
                  "eth_in": eth_in}
    return out


def early_buyers(token, n=20, deadline=None):
    """First n buyers after launch (same contract as ch.early_buyers) or None (not Bankr).
    Shared Robinhood core: a buy without a receipt = token straight from PoolManager or a router; otherwise by receipt
    (classify_entries). Bankr infrastructure (vesting, fee recipients) are not buyers."""
    token = token.lower()
    st = _STATE.get(token) or detect(token)
    launch = get_launch(token, st) if st else None
    if launch is None:
        return None
    market, excluded = _sets(token, launch)
    return ch.early_buyers_from(token, n, deadline, launch, excluded, ROUTERS | {PM}, classify_entries)


def early_status(token, buyers, deadline=None, launch=None, out_cap=ch.EARLY_OUT_CAP):
    """What early buyers did with the token (same contract as ch.early_status): going to the market = a sell."""
    return ch.early_status_from(token.lower(), buyers, deadline, MARKET, flap._sell(MARKET), out_cap=out_cap)


# shared with Robinhood: resolved at call time (tests patch ch functions)
def block_timestamps(blocks, *a, **k):
    return ch.block_timestamps(blocks, *a, **k)


def wallet_distinct_tokens(*a, **k):
    return ch.wallet_distinct_tokens(*a, **k)


def is_contract(addresses, *a, **k):
    return ch.is_contract(addresses, *a, **k)


def token_meta(token):
    return ch.token_meta(token)
