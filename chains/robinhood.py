"""Robinhood Chain adapter (chain id 4663) for rh-crawler.
The only place that talks to the blockchain: JSON-RPC with retries and a shared RPS limiter,
eth_getLogs with an adaptive window (on refusal or hitting the response limit the window is halved),
trade detection by token movement relative to the market (curve, pool, routers).
Read-only: no keys, no signing, no sending transactions.
"""
import os, re, sys, time, json, random, threading, urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from env import load_dotenv
from chains import priority
load_dotenv()

RPC = os.environ["CRAWLER_RPC"]  # Alchemy PAYG endpoint

CHAIN = "robinhood"
PACK_WINDOW = 0           # pack window (detect.find_packs): same block
BUNDLE_WINDOW = 0         # launch_bundle: entry in the launch block
RUG_SNIPERS = True        # probably rug: unsold snipers (first 30 s) count toward the reserve
FRESH_WINDOW = 1_000_000  # window (blocks) for wallet freshness
FRESH_CAP    = 4          # count distinct tokens up to this many, no need beyond
HISTORY_PARALLEL = 4      # wallets read concurrently (engine._RankGate): a history batch = 10 limiter slots,
                          # 2–3 wallets already take all of CRAWLER_RPS; more isn't faster, and the largest don't finish in time

FACTORY  = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
ROUTERS  = {  # router set: received the token FROM a router = bought, sent to a router = sold
    "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f",
    "0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc",
    "0x8876789976decbfcbbbe364623c63652db8c0904",
}
INFRA = {  # not holders for any token; the token's curve and market are in excluded_addresses
    "0x000000000000000000000000000000000000dead",  # burn
    "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e",  # Pons V2 Factory
    "0x267444d099b10fb5ed7c3cc7b7c767adca574952",  # Pons V2 Locker (LP after mint)
    "0x8366a39cc670b4001a1121b8f6a443a643e40951",  # V4 Pool Manager
    "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044",  # Pons V2 Hook
    "0xe33e9e479df8802cb0866d5d05258bec4cf62948",  # LaunchAndBuy
    "0x000000000022d473030f116ddee9f6b43ac78ba3",  # Permit2
    "0xe68d0bbc023de3febda04f413db23ce9c5ea1934",  # Gaslite drop
    "0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # WETH
    "0x0000000000000000000000000000000000000000",
}
LOCKERS = {  # known token-locking services on the chain (any launchpad): a transfer there is a lock, not an exit
    "0x548129a58bc230549df7f9e33f27e77f6779ff0f": "Sablier",   # Sablier Lockup (NFT SAB-LOCKUP)
    "0x37c434ec1c54e360900e3a022247d5e20137c1de": "Sablier",   # Sablier: stream-creation periphery
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CURVE_BUY  = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
LAUNCH_TOPIC = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"  # factory: [token, curve, creator]
LAUNCH_SEARCH_SPAN = 9_999_999  # backward search step for the launch; public RPC allows <= 10M blocks per request

def u256(data, i):
    return int(data[2 + i*64 : 2 + (i+1)*64], 16)

def _is_rate_limited(body):
    """True if the node response = rate-limit / throughput exceeded."""
    def hit(err):
        if not isinstance(err, dict):
            return False
        code = err.get("code")
        msg = str(err.get("message", "")).lower()
        # ONLY real rate-limit signs. Do NOT match "exceeded"/"too many"/
        # "capacity": they appear in the window-size error that get_logs handles
        # by splitting. Otherwise retries spin those requests 5 times -> the scan hangs.
        return code == 429 or any(k in msg for k in (
            "rate limit", "rate-limit", "per second", "too many requests"))
    if isinstance(body, dict) and "error" in body:
        return hit(body["error"])
    if isinstance(body, list):
        return any(isinstance(x, dict) and hit(x.get("error", {})) for x in body)
    return False

RPS = int(os.environ.get("CRAWLER_RPS", "8"))  # global requests/sec ceiling
_LIMIT = priority.Throttle(RPS)                          # shared adapter limiter
_BG_LIMIT = priority.Throttle(priority.background_rps())  # separate limit for background work (BACKGROUND_RPS)
_CRIT_LIMIT = priority.Throttle(priority.critical_rps())  # separate limit for critical work (CRITICAL_RPS)
def _rate_limit(n=1):
    # spreads requests over time so as not to exceed the provider limit (QuickNode 15 rps etc.) and not get 429s.
    # n = request weight: providers count each batch element as a separate call.
    # background (alerts rechecks, early without a scan) yields to live scans and is no faster than BACKGROUND_RPS;
    # critical work (rewards scheduler) doesn't yield, no faster than CRITICAL_RPS
    priority.rate_limit(_LIMIT, _BG_LIMIT, n, crit=_CRIT_LIMIT)


REQUESTS = [0]  # counter of HTTP requests to RPC (including retries)
RATE_LIMITED = [0]  # how many times RPC answered rate-limit / 429 (pauses alerts rechecks)

def _post(payload, _tries=5):
    """POST with retries and backoff on rate-limit/failure: one rejected request
    under load must not bring down the whole scan."""
    last = None
    for a in range(_tries):
        _rate_limit(len(payload) if isinstance(payload, list) else 1)
        REQUESTS[0] += 1
        req = urllib.request.Request(RPC, data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json", "User-Agent": "Mozilla/5.0 rh-crawler"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = json.loads(r.read())
            if _is_rate_limited(body):
                RATE_LIMITED[0] += 1
            if _is_rate_limited(body) and a < _tries - 1:
                last = body; time.sleep(0.35 * (2 ** a) + random.random() * 0.3); continue
            return body
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
            except Exception:
                body = None
            if e.code == 429 or (body is not None and _is_rate_limited(body)):
                RATE_LIMITED[0] += 1
            if (e.code == 429 or (body is not None and _is_rate_limited(body))) and a < _tries - 1:
                last = body if body is not None else e
                time.sleep(0.35 * (2 ** a) + random.random() * 0.3); continue
            if body is not None:
                return body
            raise
        except Exception as e:
            last = e
            if a < _tries - 1:
                time.sleep(0.35 * (2 ** a) + random.random() * 0.3); continue
            raise
    if isinstance(last, Exception):
        raise last
    return last if last is not None else {}

def rpc(method, params):
    d = _post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in d:
        raise RuntimeError(d["error"].get("message", str(d["error"])))
    return d["result"]

def rpc_batch(calls):
    """Many JSON-RPC calls in ONE HTTP request. calls=[(method,params),...].
    Returns the list of results in the original order (None if an element errored)."""
    if not calls:
        return []
    payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p}
               for i, (m, p) in enumerate(calls)]
    resp = _post(payload)
    if isinstance(resp, dict):
        resp = [resp]
    by_id = {r.get("id"): r for r in resp}
    return [by_id.get(i, {}).get("result") for i in range(len(calls))]

def block_number():
    return int(rpc("eth_blockNumber", []), 16)

def topic_for(address):
    return '0x'+'0'*24+address[2:].lower()

def addr_from_topic(t):  # 32-byte topic -> address
    return "0x" + t[-40:].lower()

LOG_CAP = 10_000  # public RPC silently truncates the eth_getLogs response at this many logs

def _is_window_error(msg):
    """getLogs window/response size error: fixed by splitting the window."""
    return any(k in msg for k in ("range","too large","too many","limited","response","size","exceed","more than"))

def iter_logs(from_block, to_block, address=None, topics=None):
    """getLogs pages in block order, with an adaptive window: on 'range too large' or hitting LOG_CAP
    (the response may have been silently truncated) the window is halved. The caller parses a page and releases it,
    so only one page of raw logs is in memory, not the whole history."""
    stack = [(from_block, to_block)]
    while stack:
        lo, hi = stack.pop()
        flt = {"fromBlock": hex(lo), "toBlock": hex(hi)}
        if address: flt["address"] = address
        if topics:  flt["topics"] = topics
        try:
            logs = rpc("eth_getLogs", [flt])
        except RuntimeError as e:
            msg = str(e).lower()
            if _is_window_error(msg) and hi > lo:
                logs = None
            else:
                raise
        if logs is not None and (len(logs) < LOG_CAP or hi <= lo):
            yield logs
            logs = None             # page released before the next request
            continue
        logs = None
        mid = (lo + hi) // 2
        stack.append((mid + 1, hi))
        stack.append((lo, mid))     # left half first


def get_logs(from_block, to_block, address=None, topics=None):
    """All logs matching the filter in [from_block, to_block] as one list (iter_logs)."""
    return [lg for page in iter_logs(from_block, to_block, address, topics) for lg in page]

def first_log_block(from_block, to_block, address=None, topics=None):
    """Earliest block with a log matching the filter in [from_block, to_block], or None.
    Splits the window like get_logs, but left half first: the right half is read
    only if the left one is empty."""
    if from_block > to_block:
        return None
    flt = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
    if address: flt["address"] = address
    if topics:  flt["topics"] = topics
    try:
        logs = rpc("eth_getLogs", [flt])
    except RuntimeError as e:
        if _is_window_error(str(e).lower()) and to_block > from_block:
            logs = None
        else:
            raise
    if logs is not None and (len(logs) < LOG_CAP or to_block <= from_block):
        return min((int(lg["blockNumber"], 16) for lg in logs), default=None)
    mid = (from_block + to_block) // 2
    left = first_log_block(from_block, mid, address, topics)
    return left if left is not None else first_log_block(mid + 1, to_block, address, topics)

def parse_transfer(log):
    tp = log.get("topics") or []
    if len(tp) < 3 or tp[0].lower() != TRANSFER_TOPIC:
        return None
    return {"token": log["address"].lower(),
            "frm": addr_from_topic(tp[1]),
            "to":  addr_from_topic(tp[2]),
            "tx":  log["transactionHash"],
            "block": int(log["blockNumber"], 16)}

def token_curve(token, lookback=6000):
    """Address of the token's bonding curve from its launch event (topic[2])."""
    bn = block_number()
    for lg in get_logs(bn - lookback, bn, address=FACTORY):
        tp = lg.get("topics") or []
        if len(tp) >= 3 and addr_from_topic(tp[1]) == token.lower():
            return addr_from_topic(tp[2])
    return None

def classify_leg(t, market):
    """Buy/sell by the leg with the token's market (its curve + global routers).
    Pitfall #1: the signer is always a relayer, so we look at token movement, not tx from."""
    if t["frm"] in market and t["to"] not in market:
        return ("buy", t["to"])     # token came to the trader from the market
    if t["to"] in market and t["frm"] not in market:
        return ("sell", t["frm"])   # trader sent the token to the market
    return (None, None)


V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
V4_POOL_MGR   = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
WETH_ADDR     = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
def _signed(x): return x-(1<<256) if x>=(1<<255) else x

def v4_swap_quote(receipt_logs, tok_amount):
    """Quote side (raw) of the V4 swap whose token side ~= tok_amount
    (6% tolerance: fees/tax), or None if there is no such swap in the logs."""
    best=None; best_err=0.06
    for l in receipt_logs:
        if l["address"].lower()!=V4_POOL_MGR: continue
        tp=l.get("topics") or []
        if not tp or tp[0].lower()!=V4_SWAP_TOPIC: continue
        a0=abs(_signed(u256(l["data"],0))); a1=abs(_signed(u256(l["data"],1)))
        for tok_side,quote_side in ((a0,a1),(a1,a0)):
            if tok_side<=0: continue
            err=abs(tok_side-tok_amount)/tok_amount
            if err<best_err: best_err=err; best=quote_side
    return best

def match_swap_quote(receipt_logs, tok_amount):
    """From the transaction logs, find the V4 swap whose token side ~= tok_amount
    and return that swap's quote side (raw). This picks the right pool
    and determines which of amount0/amount1 is the token and which is the quote.
    Returns 0 if there's no match (not a trade transfer)."""
    if tok_amount<=0: return 0
    best=v4_swap_quote(receipt_logs, tok_amount)
    if best:
        return best
    # FALLBACK: a pool whose swap event we don't decode (not our pool manager / another AMM).
    # Quote side = the largest WETH transfer in the same transaction. DEX-agnostic,
    # so the price is read on unfamiliar pools too instead of falling to None.
    weth = [int(l["data"], 16) for l in receipt_logs
            if (p := parse_transfer(l)) and p["token"] == WETH_ADDR]
    weth = [a for a in weth if a > 0]
    return max(weth) if weth else 0


# ---------------------------------------------------------------------------
# adapter API for rh-crawler
# ---------------------------------------------------------------------------

def market_addresses(curve):
    """Token market: its curve + global routers + V4 pool manager."""
    return {curve.lower()} | ROUTERS | {V4_POOL_MGR}


def excluded_addresses(curve):
    """All addresses not counted as token holders: infrastructure
    (burn, zero, factory, locker, hook, LaunchAndBuy, ...) + the token's market."""
    return INFRA | market_addresses(curve)


_LAUNCH = {}  # token -> get_launch result (never changes)


def get_launch(token):
    """{"block", "curve", "deployer", "tx"} or None.
    The launch event is searched on the factory backwards in windows of LAUNCH_SEARCH_SPAN.
    deployer by token movement: whoever received tokens from the curve in the launch transaction
    (dev buy). No dev buy: creator from topic[3] of the factory event (that's the factory's
    msg.sender: when launched via a third-party contract, it's that contract)."""
    token = token.lower()
    if token in _LAUNCH:   # launch never changes
        return dict(_LAUNCH[token])
    hi = block_number()
    launch = None
    while hi >= 0 and launch is None:
        lo = max(0, hi - LAUNCH_SEARCH_SPAN)
        for lg in get_logs(lo, hi, address=FACTORY, topics=[LAUNCH_TOPIC, topic_for(token)]):
            if len(lg["topics"]) >= 4 and addr_from_topic(lg["topics"][1]) == token:
                launch = lg
                break
        hi = lo - 1
    if launch is None:
        return None
    curve = addr_from_topic(launch["topics"][2])
    deployer = addr_from_topic(launch["topics"][3])
    rc = rpc("eth_getTransactionReceipt", [launch["transactionHash"]])
    market = market_addresses(curve)
    for l in rc["logs"]:
        p = parse_transfer(l)
        if p and p["token"] == token and p["frm"] == curve and p["to"] not in market and p["to"] not in INFRA:
            deployer = p["to"]
            break
    res = {"block": int(launch["blockNumber"], 16), "curve": curve,
           "deployer": deployer, "tx": launch["transactionHash"]}
    _LAUNCH[token] = res
    return dict(res)


def token_facts(token, launch):
    """Token facts for the engine (shared chain contract): {"supply", "transfers", "excluded",
    "market", "reserve", "reserve_ok", "base"}. base = None: the engine computes balances and circulating supply from transfers
    (detect.supply_base); here we have the full transfer history since launch.
    reserve = tokens in liquidity: curve balance (before migration) + V4 pool manager (after;
    the singleton holds only pools with this token). The locker is not liquidity, not included.
    The Pons curve's virtual add-on is only on the quote side (phantomQuote()); the token
    side of the price is tokenReserve() == the curve's real balance, so we take the real balance.
    reserve_ok = False if neither the curve nor the pool manager holds the token (0): reserve not measured, rather than "no liquidity"."""
    transfers, supply = load_transfers(token, launch["block"])
    remember_scan(token, launch, transfers, supply)
    pools = {launch["curve"].lower(), V4_POOL_MGR}
    reserve = sum(t["amount"] * ((t["to"] in pools) - (t["frm"] in pools)) for t in transfers)
    return {"supply": supply, "transfers": transfers, "excluded": excluded_addresses(launch["curve"]),
            "market": market_addresses(launch["curve"]), "reserve": max(0, reserve), "reserve_ok": reserve > 0,
            "base": None}


# ---------------------------------------------------------------------------
# token transfer history cache between scans (TRANSFER_CACHE_ENABLED, off by default)
# ---------------------------------------------------------------------------
TRANSFER_CACHE_ENABLED = os.environ.get("TRANSFER_CACHE_ENABLED", "").strip().lower() in ("1", "true", "yes")
TRANSFER_CACHE_MAX = 200_000   # transfers across all tokens (LRU by token); ~670 bytes per transfer, up to ~130 MB
TRANSFER_CACHE_LAG = 20        # last blocks near the head are re-read on the next scan (the node may have lagged)
ZERO_ADDR = "0x" + "0" * 40
_TCACHE = {}                   # token -> {"from", "head", "transfers", "n", "last"}; insertion order = LRU
_TCACHE_SIZE = [0]
_TCACHE_LOCK = threading.Lock()
TCACHE_STATS = {"hit": 0, "miss": 0, "invalid": 0, "evicted": 0}

try:
    TRANSFER_CACHE_MAX = int(os.environ.get("TRANSFER_CACHE_MAX", "").strip() or TRANSFER_CACHE_MAX)
except ValueError:
    pass


def _key(t):
    return (t["block"], t["log_index"])


def _supply_matches(transfers, supply):
    """History is complete: minted (from 0x0) minus burned (to 0x0) == totalSupply (ERC20 + Burnable Pons V2)."""
    return sum(t["amount"] for t in transfers if t["frm"] == ZERO_ADDR) - \
        sum(t["amount"] for t in transfers if t["to"] == ZERO_ADDR) == supply


def _tcache_drop(token):
    e = _TCACHE.pop(token, None)
    if e is not None:
        _TCACHE_SIZE[0] -= e["n"]
    return e


def _tcache_get(token, from_block):
    """Verified cache entry or None. A corrupted one (wrong launch, length or last transfer mismatch,
    order broken, transfer after the read head) is deleted. Lists in the cache are never mutated: check without a lock."""
    with _TCACHE_LOCK:
        e = _TCACHE.pop(token, None)
        if e is not None:
            _TCACHE[token] = e            # to the end: recently used
    if e is None:
        return None
    trs = e["transfers"]
    if (e["from"] == from_block and len(trs) == e["n"] and (_key(trs[-1]) if trs else None) == e["last"]
            and (not trs or trs[-1]["block"] <= e["head"]) and all(_key(a) < _key(b) for a, b in zip(trs, trs[1:]))):
        return e
    with _TCACHE_LOCK:
        if _TCACHE.get(token) is e:
            _tcache_drop(token)
    TCACHE_STATS["invalid"] += 1
    return None


def _tcache_put(token, from_block, transfers, head):
    """History up to the read head. Above TRANSFER_CACHE_MAX: not stored; beyond that, least recently
    used tokens are evicted. If a parallel scan already stored a fresher history, keep it."""
    with _TCACHE_LOCK:
        old = _TCACHE.get(token)
        if old is not None and old["head"] > head:
            return
        _tcache_drop(token)
        if len(transfers) > TRANSFER_CACHE_MAX:
            return
        _TCACHE[token] = {"from": from_block, "head": head, "transfers": transfers, "n": len(transfers),
                          "last": _key(transfers[-1]) if transfers else None}
        _TCACHE_SIZE[0] += len(transfers)
        while _TCACHE_SIZE[0] > TRANSFER_CACHE_MAX:
            _tcache_drop(next(iter(_TCACHE)))   # least recently used
            TCACHE_STATS["evicted"] += 1


def tcache_clear():
    with _TCACHE_LOCK:
        _TCACHE.clear()
        _TCACHE_SIZE[0] = 0


def cached_transfers(token, from_block):
    """The token's whole history from the cache (what the last scan read) or None."""
    e = _tcache_get(token.lower(), from_block)
    return e and e["transfers"]


# Pons scan history cap (SCAN_MAX_LOGS): above it, HistoryTooLarge, and the engine answers "token history too large".
# Counted during paged reading (get_token_transfers inside _log_cap): nothing beyond the cap is read or kept.
SCAN_MAX_LOGS = 150_000        # ~100 MB of parsed transfers; 0 = no cap
try:
    SCAN_MAX_LOGS = max(0, int(os.environ.get("SCAN_MAX_LOGS", "").strip() or SCAN_MAX_LOGS))
except ValueError:
    pass
_CAP = threading.local()


class HistoryTooLarge(Exception):
    """Token history is longer than SCAN_MAX_LOGS transfers."""


class _log_cap:
    """Log cap for get_token_transfers in this thread (0 = no cap)."""
    def __init__(self, n):
        self.n = n

    def __enter__(self):
        self.prev = getattr(_CAP, "n", 0)
        _CAP.n = self.n

    def __exit__(self, *exc):
        _CAP.n = self.prev


def load_transfers(token, from_block):
    """(transfers from from_block to head, totalSupply) for a scan. Cache off: as before, the whole history via getLogs.
    On, with a verified token history: only the tail is read, from block head − TRANSFER_CACHE_LAG of the previous
    read (near the head the node may not have returned everything, those blocks are re-read) to the new head. The join is checked
    against totalSupply; on mismatch, a full read. A full read that doesn't match totalSupply is not cached."""
    token = token.lower()
    with _log_cap(SCAN_MAX_LOGS):
        transfers, supply = _load_transfers(token, from_block)
    if SCAN_MAX_LOGS and len(transfers) > SCAN_MAX_LOGS:
        raise HistoryTooLarge(len(transfers))
    return transfers, supply


def _load_transfers(token, from_block):
    if not TRANSFER_CACHE_ENABLED:
        transfers = get_token_transfers(token, from_block)
        return transfers, token_supply(token)
    head = block_number()
    e = _tcache_get(token, from_block)
    transfers = None
    if e is not None and e["head"] <= head:
        upto = max(from_block - 1, e["head"] - TRANSFER_CACHE_LAG)
        cut = len(e["transfers"])
        while cut and e["transfers"][cut - 1]["block"] > upto:
            cut -= 1
        transfers = e["transfers"][:cut] + get_token_transfers(token, upto + 1, head)
        supply = token_supply(token)
        if _supply_matches(transfers, supply):
            TCACHE_STATS["hit"] += 1
        else:
            transfers = None
            TCACHE_STATS["invalid"] += 1
    if transfers is None:
        TCACHE_STATS["miss"] += e is None
        transfers = get_token_transfers(token, from_block, head)
        supply = token_supply(token)
        if not _supply_matches(transfers, supply):
            return transfers, supply
    _tcache_put(token, from_block, transfers, head)
    return transfers, supply


def get_token_transfers(token, from_block, to_block=None, frm=None, to=None):
    """Token transfers in blocks [from_block, to_block] (to_block=None = up to current), in chain order.
    frm / to: sender / recipient filter: an address or list of addresses (OR), via getLogs topics.
    ts = block time from the log's blockTimestamp field if RPC returns it (Alchemy does), otherwise the key is absent."""
    out = []
    pick = lambda a: None if a is None else [topic_for(x) for x in ([a] if isinstance(a, str) else sorted(a))]
    topics = [TRANSFER_TOPIC, pick(frm), pick(to)]
    while topics[-1] is None:
        topics.pop()
    hi = block_number() if to_block is None else to_block
    if hi < from_block:
        return out
    cap, seen = getattr(_CAP, "n", 0), 0
    same = {}   # one string per address and tx hash: in a long history they repeat thousands of times
    for page in iter_logs(from_block, hi, address=token.lower(), topics=topics):
        seen += len(page)
        if cap and seen > cap:
            raise HistoryTooLarge(seen)
        for lg in page:
            p = parse_transfer(lg)
            if not p or len(lg["data"]) <= 2:
                continue
            t = {"frm": same.setdefault(p["frm"], p["frm"]), "to": same.setdefault(p["to"], p["to"]),
                 "amount": int(lg["data"], 16), "tx": same.setdefault(p["tx"], p["tx"]), "block": p["block"],
                 "log_index": int(lg["logIndex"], 16)}
            if lg.get("blockTimestamp"):
                t["ts"] = int(lg["blockTimestamp"], 16)
            out.append(t)
        page = None
    out.sort(key=lambda t: (t["block"], t["log_index"]))
    return out


_BLOCK_TS = {}  # block -> unix_time (never changes), shared by scan and early buyers


def block_timestamps(blocks, chunk=100):
    """{block: unix_time} via batched eth_getBlockByNumber(..., false); already known ones from the process cache."""
    blocks = sorted(set(blocks))
    out = {b: _BLOCK_TS[b] for b in blocks if b in _BLOCK_TS}
    need = [b for b in blocks if b not in out]
    for i in range(0, len(need), chunk):
        part = need[i:i + chunk]
        for b, blk in zip(part, rpc_batch([("eth_getBlockByNumber", [hex(b), False]) for b in part])):
            if blk:
                out[b] = int(blk["timestamp"], 16)
    if len(_BLOCK_TS) > CACHE_MAX:
        _BLOCK_TS.clear()
    _BLOCK_TS.update(out)
    return out


def token_supply(token):
    return int(rpc("eth_call", [{"to": token.lower(), "data": "0x18160ddd"}, "latest"]), 16)


def token_balance(token, address):
    return int(rpc("eth_call", [{"to": token.lower(), "data": "0x70a08231" + "0" * 24 + address.lower()[2:]}, "latest"]), 16)


def token_decimals(token):
    return int(rpc("eth_call", [{"to": token.lower(), "data": "0x313ce567"}, "latest"]), 16)


def block_info(block):
    """{"number", "hash", "timestamp"} of a block (eth_getBlockByNumber, without transactions)."""
    b = rpc("eth_getBlockByNumber", [hex(block) if isinstance(block, int) else block, False])
    return {"number": int(b["number"], 16), "hash": b["hash"], "timestamp": int(b["timestamp"], 16)}


_AT_TIME = {}  # ts -> block (answer never changes: blocks with timestamp >= ts already exist)

def block_at_time(ts):
    """First block with timestamp >= ts: {"number", "hash", "timestamp"}. Binary search by block number
    (timestamp is non-decreasing). Chain head still earlier than ts: LookupError (too early)."""
    if ts in _AT_TIME:
        return _AT_TIME[ts]
    head = block_info("latest")
    if head["timestamp"] < ts:
        raise LookupError("no block at this time yet")
    lo, hi = 0, head["number"]               # invariant: block hi qualifies; find the first qualifying one
    hi_info = head
    while lo < hi:
        mid = (lo + hi) // 2
        b = block_info(mid)
        if b["timestamp"] >= ts:
            hi, hi_info = mid, b
        else:
            lo = mid + 1
    if hi_info["number"] != hi:
        hi_info = block_info(hi)
    _AT_TIME[ts] = hi_info
    return hi_info


DELEGATION_PREFIX = "0xef0100"   # EIP-7702: delegation code on a regular wallet (EOA), not a contract

def is_contract_at(addresses, block, chunk=100):
    """{addr: is it a contract} at block block (eth_getCode at that block: the answer is deterministic).
    EIP-7702 delegation code (0xef0100 + address) = a regular wallet."""
    addrs = sorted({a.lower() for a in addresses})
    out = {}
    for i in range(0, len(addrs), chunk):
        part = addrs[i:i + chunk]
        for a, code in zip(part, rpc_batch([("eth_getCode", [a, hex(block)]) for a in part])):
            if code is None:
                raise RuntimeError(f"eth_getCode failed for {a}")
            code = code.lower()
            out[a] = code not in ("0x", "0x0", "") and not code.startswith(DELEGATION_PREFIX)
    return out


BATCH_MAX = 10           # eth_getLogs per HTTP batch when scanning a window
_ADDRLESS_SPAN = [None]  # learned getLogs window cap without address (public RPC: 30 000)

def _allowed_span(msg):
    m = re.search(r"only (\d+) are allowed", msg)
    return int(m.group(1)) if m else None

def _scan_back(topics_list, lo, hi, stop):
    """eth_getLogs without address over window [lo, hi] from newer blocks to older, in chunks
    of the learned window cap, several chunks per HTTP batch. After each
    chunk calls stop(logs); True means exit right away (early exit saves requests).
    An element that errored or hit LOG_CAP is fetched via get_logs."""
    top = hi
    while top >= lo:
        span = _ADDRLESS_SPAN[0] or (hi - lo + 1)
        chunks, t = [], top
        while t >= lo and len(chunks) < max(1, BATCH_MAX // len(topics_list)):
            b = max(lo, t - span + 1)
            chunks.append((b, t)); t = b - 1
        calls = [(a, b, tp) for a, b in chunks for tp in topics_list]
        resp = _post([{"jsonrpc": "2.0", "id": k, "method": "eth_getLogs",
                       "params": [{"fromBlock": hex(a), "toBlock": hex(b), "topics": tp}]}
                      for k, (a, b, tp) in enumerate(calls)])
        by_id = {r.get("id"): r for r in resp} if isinstance(resp, list) else {}
        learned = None
        for k in range(len(calls)):
            err = by_id.get(k, {}).get("error")
            if err and (learned := _allowed_span(str(err.get("message", "")))):
                break
        if learned and learned < span:
            _ADDRLESS_SPAN[0] = learned
            continue                    # re-read the same segment in chunks of an allowed size
        k = 0
        for a, b in chunks:
            logs = []
            for tp in topics_list:
                r = by_id.get(k, {}); k += 1
                res = r.get("result")
                if res is None or len(res) >= LOG_CAP:
                    res = get_logs(a, b, None, tp)
                logs += res
            if stop(logs):
                return
        top = t


def wallet_first_activity(wallet, before_block):
    """Earliest block < before_block where the wallet sent or received
    any token (Transfer with the wallet in topic1 or topic2). None if there is none."""
    wt = topic_for(wallet.lower())
    first = first_log_block(0, before_block - 1, None, [TRANSFER_TOPIC, wt])
    # the recipient side is searched only before the already found send block
    hi = before_block - 1 if first is None else first - 1
    recv = first_log_block(0, hi, None, [TRANSFER_TOPIC, None, wt])
    return recv if recv is not None else first


def wallet_distinct_tokens(wallet, before_block, skip_token, window=FRESH_WINDOW, cap=FRESH_CAP):
    """Freshness AT THE MOMENT of entry: how many distinct token contracts the wallet traded
    in blocks [before_block - window, before_block), not counting skip_token.
    window=None means the whole history: [0, before_block).
    before_block = block of the first receipt of the scanned token (buy or transfer),
    skip_token = the scanned token. Returns min(n, cap): cap means "cap or more".
    Trading = a token transfer to/from the wallet where the counterparty is the market:
    routers, V4 pool manager or a Pons curve (curves are recognized by CurveBuy/CurveSell
    of this wallet). Two filters (wallet in topic1 and in topic2) with OR over topic0
    cover both transfers and curve events. The window is read from the end, stopping
    as soon as cap is reached. The skip_token curve is not counted: its events are
    in the same transactions as the skip_token transfers."""
    w = wallet.lower()
    skip = skip_token.lower()
    wt = topic_for(w)
    kinds = [TRANSFER_TOPIC, CURVE_BUY, CURVE_SELL]
    venues = ROUTERS | {V4_POOL_MGR}
    tokens, curves, moves, skip_txs = set(), {}, [], set()

    def count():
        n_curves = sum(1 for txs in curves.values() if not txs <= skip_txs)
        return max(len(tokens), n_curves)

    def take(logs):
        for lg in logs:
            tp = lg["topics"]
            if tp[0].lower() in (CURVE_BUY, CURVE_SELL):
                curves.setdefault(lg["address"].lower(), set()).add(lg["transactionHash"])
            elif len(tp) >= 3:
                frm, to = addr_from_topic(tp[1]), addr_from_topic(tp[2])
                tok = lg["address"].lower()
                if tok == skip:
                    skip_txs.add(lg["transactionHash"])
                    continue
                moves.append((to if frm == w else frm, tok))
        tokens.update(tok for other, tok in moves if other in venues or other in curves)
        return count() >= cap

    hi = before_block - 1
    lo = 0 if window is None else max(0, before_block - window)
    if hi >= lo:
        _scan_back([[kinds, None, wt], [kinds, wt]], lo, hi, take)
    return min(count(), cap)


_CODE = {}  # addr -> bool, is_contract cache in process memory

def is_contract(addresses, chunk=100):
    """{addr: has code} via batched eth_getCode, with an in-memory cache."""
    addrs = {a.lower() for a in addresses}
    need = sorted(addrs - _CODE.keys())
    for i in range(0, len(need), chunk):
        part = need[i:i + chunk]
        for a, code in zip(part, rpc_batch([("eth_getCode", [a, "latest"]) for a in part])):
            if code is not None:
                _CODE[a] = code not in ("0x", "0x0", "")
    return {a: _CODE.get(a, False) for a in addrs}


LAUNCH_PHASES = {0: "curve", 1: "swept", 2: "pool", 3: "rescued"}  # factory phase field


def launched_token(token):
    """factory.getLaunchedToken(token) -> {"pair", "phase"} or None (token not from this factory).
    pair: 0x0 = native ETH, otherwise ERC-20 (tokenized stock etc.).
    phase: "curve" = on the curve, "pool" = migrated to a V4 pool (+ "swept", "rescued").
    Not cached: phase changes on migration."""
    r = rpc("eth_call", [{"to": FACTORY, "data": "0x3cf28b5a" + "0" * 24 + token.lower()[2:]}, "latest"])
    if not (len(r) >= 2 + 15 * 64 and u256(r, 14) == 1):
        return None
    return {"pair": addr_from_topic(r[2 + 4 * 64 : 2 + 5 * 64]),
            "phase": LAUNCH_PHASES.get(u256(r, 10), str(u256(r, 10)))}


_PAIR = {}  # token -> pairToken | None, launched_pair cache (the pair never changes)

def launched_pair(token):
    """Launch pairToken (see launched_token) or None if the token is not from this factory."""
    token = token.lower()
    if token not in _PAIR:
        info = launched_token(token)
        _PAIR[token] = info["pair"] if info else None
    return _PAIR[token]


CACHE_MAX = 50_000  # entries in process caches (block times, receipts)
_RECEIPTS = {}      # tx -> receipt logs (never change), shared by scan and early buyers


def receipt_logs(txs, chunk=50):
    """{tx: receipt logs} via batched eth_getTransactionReceipt; already read ones from the process cache.
    Receipt not received: empty list (and not cached)."""
    out = {h: _RECEIPTS[h] for h in txs if h in _RECEIPTS}
    need = [h for h in txs if h not in out]
    for i in range(0, len(need), chunk):
        part = need[i:i + chunk]
        for h, rc in zip(part, rpc_batch([("eth_getTransactionReceipt", [h]) for h in part])):
            out[h] = (rc or {}).get("logs", [])
            if rc:
                if len(_RECEIPTS) > CACHE_MAX:
                    _RECEIPTS.clear()
                _RECEIPTS[h] = out[h]
    return out


def classify_entries(token, transfers, wallets, chunk=50):
    """First receipt of the token by each wallet in wallets, by transaction:
    {wallet: {"kind": "buy"|"transfer", "tx", "block", "via", "eth_in"}}.
    buy: the transaction receipt has a CurveBuy of this token's curve (the curve = the contract
    that sent the token in that same transaction) or a V4 Swap whose token side
    matches the token output from the pool manager. So a buy via a third-party
    bot router is a buy too, even though the token came from an intermediate contract.
    via: who the wallet received the token from.
    eth_in: quote spent on the buy (CurveBuy quoteIn or the swap's quote
    side), checked against the amount the wallet received (6% tolerance).
    None if the pair isn't ETH or the check failed (one buy was split
    among several wallets). Always None for transfer."""
    token = token.lower()
    want = {w.lower() for w in wallets}
    first = {}
    for t in transfers:
        if t["to"] in want and t["to"] not in first:
            first[t["to"]] = t
    receipts = receipt_logs(sorted({t["tx"] for t in first.values()}), chunk)
    pair = launched_pair(token)
    eth_pair = pair in ("0x" + "0" * 40, WETH_ADDR)

    out = {}
    for w, t in first.items():
        logs = receipts.get(t["tx"], [])
        moves = [p | {"amount": int(l["data"], 16)} for l in logs
                 if (p := parse_transfer(l)) and p["token"] == token and len(l["data"]) > 2]
        senders = {m["frm"] for m in moves}
        buys = [(u256(l["data"], 1), u256(l["data"], 0)) for l in logs  # (tokensOut, quoteIn)
                if (l.get("topics") or [""])[0].lower() == CURVE_BUY and l["address"].lower() in senders]
        for m in moves:
            if m["frm"] == V4_POOL_MGR and (q := v4_swap_quote(logs, m["amount"])):
                buys.append((m["amount"], q))
        eth_in = None
        if buys and eth_pair:
            tok, quote = min(buys, key=lambda b: abs(b[0] - t["amount"]))
            if abs(tok - t["amount"]) <= 0.06 * t["amount"]:
                eth_in = quote
        out[w] = {"kind": "buy" if buys else "transfer", "tx": t["tx"], "block": t["block"],
                  "via": t["frm"], "eth_in": eth_in}
    return out


def eth_transfers(to_block, max_count=1000, from_block=0, **flt):
    """ETH transfers (alchemy_getAssetTransfers, category external) filtered by
    toAddress/fromAddress in blocks [from_block, to_block]. internal is not supported
    on this chain: ETH from contracts (exchange withdrawals via a contract, bridges) is not visible.
    Returns (list of {"from", "to", "block", "amount" (wei), "tx"}, hit_max_count)."""
    out, key = [], None
    while True:
        p = {"category": ["external"], "fromBlock": hex(from_block), "toBlock": hex(to_block),
             "excludeZeroValue": True, "maxCount": hex(min(1000, max_count - len(out)))} | flt
        if key:
            p["pageKey"] = key
        d = _post({"jsonrpc": "2.0", "id": 1, "method": "alchemy_getAssetTransfers", "params": [p]})
        if d.get("error"):
            raise RuntimeError(d["error"].get("message", str(d["error"])))
        out += [{"from": x["from"].lower(), "to": (x["to"] or "").lower(), "block": int(x["blockNum"], 16),
                 "amount": int((x.get("rawContract") or {}).get("value") or "0x0", 16), "tx": x["hash"]}
                for x in d["result"]["transfers"]]
        key = d["result"].get("pageKey")
        if not key or len(out) >= max_count:
            return out, bool(key)


def eth_inflows(wallet, before_block, max_count=1000):
    """Incoming external ETH transfers of a wallet in blocks < before_block:
    [{"from", "block", "amount" (wei)}]. No more than max_count."""
    rows, _ = eth_transfers(before_block - 1, max_count, toAddress=wallet.lower())
    return [{"from": r["from"], "block": r["block"], "amount": r["amount"]} for r in rows]


def eth_sent(frm, from_block, to_block, to=None, max_count=1000):
    """Native ETH transfers (external) from addresses frm (a list) in blocks [from_block, to_block], one request
    per sender; to: keep only recipients from the list. In token-transfer form (for reward payouts):
    [{"frm", "to", "amount" (wei), "tx", "block", "log_index": -1}], by block. log_index -1: a native transfer
    has no log, the payout key is (tx, -1)."""
    want = None if to is None else {a.lower() for a in to}
    out = []
    for a in sorted({x.lower() for x in frm}):
        rows, _ = eth_transfers(to_block, max_count, from_block=from_block, fromAddress=a)
        out += [{"frm": r["from"], "to": r["to"], "amount": r["amount"], "tx": r["tx"], "block": r["block"],
                 "log_index": -1} for r in rows if want is None or r["to"] in want]
    return sorted(out, key=lambda t: t["block"])


def outgoing_count(addr, cap=100):
    """How many outgoing external ETH transfers an address made, min(n, cap).
    Early exit at cap: only "less than cap or not" matters (hub, see README)."""
    rows, _ = eth_transfers(block_number(), cap, fromAddress=addr.lower())
    return min(len(rows), cap)


def _abi_string(h):
    """Decode a string from an eth_call (ABI) response or bytes32; empty gives ''."""
    if not h or h == "0x":
        return ""
    b = bytes.fromhex(h[2:])
    if len(b) >= 96:
        n = int.from_bytes(b[32:64], "big")
        return b[64:64 + n].decode(errors="replace")
    return b.rstrip(b"\0").decode(errors="replace")


def token_meta(token):
    """{"name", "symbol"} from the token contract, in one batch."""
    t = token.lower()
    name, sym = rpc_batch([("eth_call", [{"to": t, "data": "0x06fdde03"}, "latest"]),
                           ("eth_call", [{"to": t, "data": "0x95d89b41"}, "latest"])])
    return {"name": _abi_string(name), "symbol": _abi_string(sym)}


# ---------------------------------------------------------------------------
# early buyers: first buyers after launch and what they did with the token
# ---------------------------------------------------------------------------
EARLY_WINDOW = 2000      # blocks after launch in the first window; not enough buyers, the window doubles
EARLY_CANDIDATES = 25    # recipients at a time (no more): contract or not, buy or transfer
EARLY_OUT_CAP = 20       # wallet exits not to the market (newest) to check via receipt
BURN_ADDRS = {"0x000000000000000000000000000000000000dead", "0x" + "0" * 40}
SCAN_TTL = 600           # seconds: transfer history from a scan (= scan result cache in server.py)
SCAN_CACHE_MAX_TRANSFERS = 100_000   # transfers across all _SCAN tokens (LRU by token; ~670 bytes each, up to ~65 MB);
                                     # a token longer than the limit is not stored (early buyers reads it itself)
try:
    SCAN_CACHE_MAX_TRANSFERS = max(0, int(os.environ.get("SCAN_CACHE_MAX_TRANSFERS", "").strip()
                                          or SCAN_CACHE_MAX_TRANSFERS))
except ValueError:
    pass
_SCAN = {}               # token -> (ts, launch, transfers, supply); insertion order = LRU
_SCAN_LOCK = threading.Lock()


def _scan_size():
    return sum(len(v[2]) for v in _SCAN.values() if v[2] is not None)


def remember_scan(token, launch, transfers, supply):
    """Full transfer history since launch read by the scan (token_facts): early buyers takes
    buyers, balances and exits from it and reads only the tail after it.
    If the transfer cache is on and the history is in it, only the scan mark is kept here; the history itself comes from the cache (one copy).
    No more than SCAN_CACHE_MAX_TRANSFERS transfers in total: old ones are evicted, a token longer than the limit is not stored."""
    token = token.lower()
    if TRANSFER_CACHE_ENABLED:
        with _TCACHE_LOCK:
            if (_TCACHE.get(token) or {}).get("transfers") is transfers:
                transfers = None
    now = time.time()
    with _SCAN_LOCK:
        _SCAN.pop(token, None)
        for k in [k for k, v in _SCAN.items() if now - v[0] >= SCAN_TTL]:
            del _SCAN[k]
        if transfers is not None and len(transfers) > SCAN_CACHE_MAX_TRANSFERS:
            return
        _SCAN[token] = (now, dict(launch), transfers, supply)
        while _scan_size() > SCAN_CACHE_MAX_TRANSFERS:
            del _SCAN[next(iter(_SCAN))]   # oldest


def drop_caches():
    """Memory protection (server): drop scan history (_SCAN) and the transfer history cache."""
    with _SCAN_LOCK:
        _SCAN.clear()
    tcache_clear()


def scan_history(token):
    """(launch, transfers, supply) from the last scan younger than SCAN_TTL, or None.
    History evicted from the transfer cache or corrupted: None (early buyers reads it itself)."""
    with _SCAN_LOCK:
        hit = _SCAN.get(token.lower())
    if not hit or time.time() - hit[0] >= SCAN_TTL:
        return None
    launch, transfers, supply = hit[1:]
    if transfers is None:
        transfers = cached_transfers(token, launch["block"])
        if transfers is None:
            return None
    return launch, transfers, supply


def _with_tail(token, transfers):
    """Scan history + transfers after it (getLogs from the history's last block, duplicates by (tx, log_index) dropped)."""
    last = max((t["block"] for t in transfers), default=0)
    seen = {(t["tx"], t["log_index"]) for t in transfers if t["block"] == last}
    tail = [t for t in get_token_transfers(token, last) if (t["tx"], t["log_index"]) not in seen]
    return transfers + tail


def early_buyers(token, n=20, deadline=None):
    """First n unique buyers: {"launch": {"block", "ts", "deployer", "curve"}, "buyers": [{"wallet", "block",
    "ts", "tx", "bought"}], "txs_read"} or None (not Pons V2). Doesn't change over time.
    If there was a scan younger than SCAN_TTL, transfers come from its history (remember_scan), no requests; otherwise a window after launch
    (EARLY_WINDOW, doubles while buyers are few), so the cost doesn't depend on the token's history length. Candidates: first non-infrastructure, non-contract recipients. Buy: the token
    came straight from the market (curve, routers, pool manager), no receipt needed; otherwise by the entry receipt (classify_entries:
    CurveBuy / V4 Swap via a third-party bot router). bought = how much
    the wallet received in the first-buy transaction."""
    token = token.lower()
    hist = scan_history(token)
    launch = hist[0] if hist else get_launch(token)
    if launch is None:
        return None
    return early_buyers_from(token, n, deadline, launch, excluded_addresses(launch["curve"]),
                             market_addresses(launch["curve"]), classify_entries, hist)


def early_buyers_from(token, n, deadline, launch, excluded, market, classify, hist=None):
    """early_buyers core for a Robinhood launchpad (Pons, Flap): launch = {"block", "curve", "deployer"},
    excluded = not buyers, market = where the token comes from on a receipt-less buy, classify = the launchpad's
    classify_entries (buy by receipt), hist = scan history (launch, transfers, supply) or None."""
    head = None if hist else block_number()
    lo, span, trs = launch["block"], EARLY_WINDOW, list(hist[1]) if hist else []
    buyers, checked = [], set()
    while True:
        if hist:   # the whole history is already here, don't read windows
            hi = head = 0
        else:
            hi = min(head, launch["block"] + span)
            trs += get_token_transfers(token, lo, hi)
            lo = hi + 1
        order = list(dict.fromkeys(t["to"] for t in trs if t["to"] not in excluded))   # first recipients in order
        fresh = [a for a in order if a not in checked]
        while fresh and len(buyers) < n and not (deadline and time.time() > deadline):
            k = min(EARLY_CANDIDATES, n - len(buyers) + 3)   # calls share the limiter with scans: only what's needed
            part, fresh = fresh[:k], fresh[k:]
            checked |= set(part)
            code = is_contract(part)
            wallets = [a for a in part if not code.get(a)]
            src = {}
            for t in trs:
                if t["to"] in wallets and t["to"] not in src:
                    src[t["to"]] = t["frm"]
            # token came straight from the curve, a Pons router or the pool manager: a buy without a receipt;
            # otherwise (third-party bot router, another wallet) by the entry receipt
            unsure = [a for a in wallets if src.get(a) not in market]
            ent = classify(token, trs, unsure) if unsure else {}
            buyers += [a for a in wallets if a not in ent or ent[a]["kind"] == "buy"]
        if len(buyers) >= n or hi >= head or (deadline and time.time() > deadline):
            break
        span *= 2
    rank = {a: i for i, a in enumerate(order)}
    buyers = sorted(buyers, key=rank.get)[:n]
    first = {}
    for t in trs:
        if t["to"] in buyers and t["to"] not in first:
            first[t["to"]] = t
    blocks = sorted({launch["block"]} | {t["block"] for t in first.values() if "ts" not in t})
    ts = block_timestamps(blocks) if blocks else {}
    out = []
    for a in buyers:
        t = first[a]
        out.append({"wallet": a, "block": t["block"], "ts": t.get("ts", ts.get(t["block"])), "tx": t["tx"],
                    "bought": sum(x["amount"] for x in trs if x["to"] == a and x["tx"] == t["tx"])})
    return {"launch": {"block": launch["block"], "ts": ts.get(launch["block"]), "deployer": launch["deployer"],
                       "curve": launch["curve"]}, "buyers": out, "txs_read": len(trs)}


def early_status(token, buyers, deadline=None, launch=None, out_cap=EARLY_OUT_CAP):
    """What buyers did with the token: {"supply", "wallets": {wallet: {"now", "sold", "moved": {where: how much},
    "burned", "partial"}}}. All wallet entries and exits come from scan history + the tail after it (scan_history),
    otherwise from two getLogs by topics (to / from); the balance is derived from them.
    Exit to the market (curve, routers, pool manager) = a sell; to another address, by receipt (CurveSell or the token
    going into the pool manager in the same transaction = a sell via a third-party router), otherwise a transfer; to burn / 0x0 = burned; to a locker (LOCKERS) = locked.
    partial: more non-market exits than out_cap, or didn't finish before deadline."""
    token = token.lower()
    hist = scan_history(token)
    market = market_addresses(launch["curve"]) if launch and launch.get("curve") else ROUTERS | {V4_POOL_MGR}
    return early_status_from(token, buyers, deadline, market, _pons_sell, hist, out_cap)


def _pons_sell(token, logs):
    """Exit receipt is a Pons sell: CurveSell or the token went into the V4 pool manager in the same transaction."""
    return any((l.get("topics") or [""])[0].lower() == CURVE_SELL for l in logs) or any(
        (p := parse_transfer(l)) and p["token"] == token and p["to"] == V4_POOL_MGR for l in logs)


def early_status_from(token, buyers, deadline, market, is_sell, hist=None, out_cap=EARLY_OUT_CAP):
    """early_status core for a Robinhood launchpad (Pons, Flap): market = exit there is a sell,
    is_sell(token, receipt logs) = a sell via a third-party contract; hist = scan history or None.
    A transfer to a known locker (LOCKERS: Sablier) is a lock: "locked" (how much) and "locked_in" (where), not a transfer."""
    supply = hist[2] if hist else token_supply(token)
    ws = [b["wallet"] for b in buyers]
    out = {w: {"now": 0, "sold": 0, "moved": {}, "burned": 0, "locked": 0, "locked_in": [], "partial": False}
           for w in ws}
    if not ws:
        return {"supply": supply, "wallets": out}
    if hist:   # scan history + the tail after it: one getLogs instead of two over the whole history
        want = set(ws)
        trs = _with_tail(token, hist[1])
        ins = [t for t in trs if t["to"] in want]
        outs = [t for t in trs if t["frm"] in want]
    else:
        start = min(b["block"] for b in buyers)
        ins = get_token_transfers(token, start, to=ws)
        outs = get_token_transfers(token, start, frm=ws)
    for t in ins:
        out[t["to"]]["now"] += t["amount"]
    for t in outs:
        out[t["frm"]]["now"] -= t["amount"]
    check = defaultdict(list)
    for t in outs:
        o = out[t["frm"]]
        if t["to"] in market:
            o["sold"] += t["amount"]
        elif t["to"] in BURN_ADDRS:
            o["burned"] += t["amount"]
        elif t["to"] in LOCKERS:
            o["locked"] += t["amount"]
            if LOCKERS[t["to"]] not in o["locked_in"]:
                o["locked_in"].append(LOCKERS[t["to"]])
        else:
            check[t["frm"]].append(t)
    pick = []
    for w, ts_ in check.items():
        if len(ts_) > out_cap:
            out[w]["partial"] = True
        pick += ts_[-out_cap:]
    txs = sorted({t["tx"] for t in pick})
    sells, checked = set(), set()
    for i in range(0, len(txs), 50):
        if deadline and time.time() > deadline:
            break
        part = txs[i:i + 50]
        for h, logs in receipt_logs(part).items():
            if is_sell(token, logs):
                sells.add(h)
        checked |= set(part)
    for t in pick:
        o = out[t["frm"]]
        if t["tx"] not in checked:   # receipt not checked in time
            o["partial"] = True
        elif t["tx"] in sells:
            o["sold"] += t["amount"]
        else:
            o["moved"][t["to"]] = o["moved"].get(t["to"], 0) + t["amount"]
    return {"supply": supply, "wallets": out}
