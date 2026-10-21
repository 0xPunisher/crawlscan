"""Solana chain adapter (pump.fun tokens) for rh-crawler.
The only place that talks to Solana: JSON-RPC (SOLANA_RPC) with retries and a shared
request limiter (SOLANA_RPS), batched calls in one HTTP request.
Read-only: no keys, no signing, no sending transactions.

Mapping of Robinhood → Solana concepts (keys and types are the same as in chains/robinhood.py):
- address: base58 (32 bytes), case-sensitive — we do NOT lowercase it;
- block → slot (int); block_timestamps → {slot: unix_time};
- tx → transaction signature (base58);
- eth_in → lamports (int, 1 SOL = 10**9), not wei; the pair is always SOL;
- curve → PDA of the pump.fun bonding curve ["bonding-curve", mint];
- contract (is_contract) → PDA (point off the ed25519 curve) or an executable account;
- holder → owner of the token account (owner), not the token account itself;
- transfers → transfers only over the top holders' token accounts (the full transfer
  history of the token is not available here: it is expensive to read). Each leg: who sent/received
  (owners, from the transaction's pre/postTokenBalances), how much, signature, slot.

How the Solana flow differs from Robinhood (the engine gets everything via token_facts):
- balances and turnover come not from transfers but from supply_base(token, launch)
  (getTokenLargestAccounts): the same shape as detect.supply_base, returned in facts["base"];
  holders_total is a lower bound (Alchemy does not serve getProgramAccounts by mint);
  exact if there are fewer than 20 accounts;
- excluded_addresses/market_addresses are extended with PDAs found during the scan
  (pools, vaults, lockers), so they are called after supply_base/get_token_transfers;
- classify_entries returns kind=None, unread=True for a wallet with no entry found;
- wallet_distinct_tokens returns None if it did not fit in the read limit (we do not guess);
- a pack is buys within PACK_WINDOW slots, not within one block.
Funding is not implemented (USE_FUNDING = False): eth_inflows/outgoing_count — NotImplementedError.
"""
import base64, hashlib, json, os, random, sys, threading, time, urllib.error, urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from env import load_dotenv
from chains import priority
load_dotenv()

RPC = os.environ.get("SOLANA_RPC")
RPS = int(os.environ.get("SOLANA_RPS", "8"))  # global cap on HTTP requests/sec
USE_FUNDING = False
CHAIN = "solana"
PACK_WINDOW = 2            # pack window (detect.find_packs): up to 2 slots from the first buyer
BUNDLE_WINDOW = 2          # launch_bundle: entry no later than 2 slots after launch (bundle in the create slot)
RUG_SNIPERS = False        # probably rug: 30-second snipers are ordinary bots, not counted in the reserve

# --- programs and accounts ---
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"            # pump.fun (bonding curves)
PUMP_SWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"       # PumpSwap AMM (pools after migration)
PUMP_MINT_AUTH = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
PUMP_GLOBAL = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
PUMP_MIGRATION = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
PUMP_FEE = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
SYSTEM = "11111111111111111111111111111111"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
METAPLEX = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
BURN = "1nc1nerator11111111111111111111111111111111"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {WSOL,
           "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # USDT

DEX = {  # taking part in a transaction = a trade (bot routers call these programs via inner instructions)
    PUMP: "pump.fun",
    PUMP_SWAP: "PumpSwap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
    "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS": "Raydium Route",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora DAMM",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
    "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma": "OKX DEX",
}
# Shared authorities of pool vaults: PDAs without data (getAccountInfo → null, "system-owned"), so they
# cannot be recognized by owner program. Derived from program seeds (find_pda), verified 2026-10-07.
VAULT_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Raydium AMM v4",   # [b"amm authority"]
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL": "Raydium CPMM",     # [b"vault_and_lp_mint_auth_seed"]
    "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC": "Meteora DAMM v2",  # [b"pool_authority"]
}
INFRA = {  # not holders for any token; the curve, its account and the market PDA go into excluded_addresses
    BURN, SYSTEM, PUMP, PUMP_SWAP, PUMP_MINT_AUTH, PUMP_GLOBAL, PUMP_MIGRATION, PUMP_FEE,
} | set(DEX)

SIG_PAGE = 1000            # standard getSignaturesForAddress limit
LAUNCH_PAGE = 10000        # Alchemy serves up to 10000 per page; if refused, fall back to SIG_PAGE
LAUNCH_MAX_PAGES = 10      # curve history deeper than this — we do not look for the launch
HOLDER_MAX_PAGES = 2       # holder's token account history deeper than this — we do not look for the entry (unread)
HOLDER_TX_CAP = 30         # token account transactions we read: 10 oldest (entry) + 20 newest (sells)
HISTORY_TX_CAP = 100       # wallet transactions before entry we read for wallet_distinct_tokens
HISTORY_CHUNK = 25         # ... in chunks, with early exit
TX_BATCH = 50              # getTransaction calls per HTTP request
EARLY_TX_CHUNK = 60        # early buyers: oldest curve transactions per round
EARLY_TX_MAX = 300         # ... at most this many when searching for the first buyers
EARLY_OUT_CAP = 20         # transactions of a buyer's token account (newest) for parsing exits
TX_CACHE_MAX = 50_000      # transactions in the process cache
BUY_MATCH = 0.06           # matching tolerance: market sent ≈ wallet received (fees, tax)
TXOPT = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}

# ---------------------------------------------------------------------------
# base58, ed25519, PDA
# ---------------------------------------------------------------------------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b):
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def b58decode(s):
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def is_address(s):
    """String is a Solana address (base58, 32 bytes)."""
    try:
        return isinstance(s, str) and 32 <= len(s) <= 44 and len(b58decode(s)) == 32
    except ValueError:
        return False


_P = 2 ** 255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P


def on_curve(b):
    """32 bytes is a point on the ed25519 curve (a regular wallet), otherwise a PDA."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    y %= _P
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


_ONC = {}


def is_pda(addr):
    """Address off the ed25519 curve = a program PDA (pool, curve, vault), not a wallet."""
    if addr not in _ONC:
        _ONC[addr] = not on_curve(b58decode(addr))
    return _ONC[addr]


def find_pda(seeds, program):
    """findProgramAddress: the first bump from 255 down that gives a point off the curve."""
    pid = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58encode(h)
    raise ValueError("PDA not found")


def bonding_curve(mint):
    return find_pda([b"bonding-curve", b58decode(mint)], PUMP)


def associated_token_account(owner, mint, token_program=TOKEN):
    return find_pda([b58decode(owner), b58decode(token_program), b58decode(mint)], ATA_PROGRAM)


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------
_LIMIT = priority.Throttle(RPS)                          # the adapter's shared limiter
_BG_LIMIT = priority.Throttle(priority.background_rps())  # own limit for background work (BACKGROUND_RPS)
_CRIT_LIMIT = priority.Throttle(priority.critical_rps())  # own limit for critical work (CRITICAL_RPS)


def _rate_limit():
    # Spreads HTTP requests over time. Weight is one HTTP request, not a batch item: Alchemy Solana
    # limits compute units, and a batch of 100 getTransaction goes through without 429; per-item 429s are retried.
    # background yields to live scans and runs no faster than BACKGROUND_RPS
    priority.rate_limit(_LIMIT, _BG_LIMIT, crit=_CRIT_LIMIT)


REQUESTS = [0]  # counter of HTTP requests to RPC (including retries)
RATE_LIMITED = [0]  # how many times RPC answered rate-limit / 429 (pauses alerts rechecks)


def _is_rate_limited(err):
    if not isinstance(err, dict):
        return False
    msg = str(err.get("message", "")).lower()
    return err.get("code") == 429 or any(k in msg for k in ("rate limit", "per second", "too many requests"))


def _backoff(a):
    time.sleep(0.35 * (2 ** a) + random.random() * 0.3)


def _post(payload, _tries=5):
    """POST with retries and backoff on 429/failure."""
    if not RPC:
        raise RuntimeError("SOLANA_RPC is not set")
    for a in range(_tries):
        _rate_limit()
        REQUESTS[0] += 1
        req = urllib.request.Request(RPC, data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json", "User-Agent": "rh-crawler"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = json.loads(r.read())
            if isinstance(body, dict) and _is_rate_limited(body.get("error")):
                RATE_LIMITED[0] += 1
            if isinstance(body, dict) and _is_rate_limited(body.get("error")) and a < _tries - 1:
                _backoff(a); continue
            return body
        except urllib.error.HTTPError as e:
            if e.code == 429:
                RATE_LIMITED[0] += 1
            if e.code in (429, 502, 503) and a < _tries - 1:
                _backoff(a); continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if a < _tries - 1:
                _backoff(a); continue
            raise


def rpc(method, params):
    d = _post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in d:
        raise RuntimeError(d["error"].get("message", str(d["error"])))
    return d["result"]


def rpc_batch(calls, chunk=TX_BATCH, _tries=4):
    """Many calls batched by chunk per HTTP request. calls=[(method, params)].
    Results in the original order; None means the item failed. Items with 429 are retried."""
    out = [None] * len(calls)
    todo = list(range(len(calls)))
    for a in range(_tries):
        retry = []
        for i in range(0, len(todo), chunk):
            ids = todo[i:i + chunk]
            resp = _post([{"jsonrpc": "2.0", "id": k, "method": calls[k][0], "params": calls[k][1]} for k in ids])
            if not isinstance(resp, list):
                if _is_rate_limited((resp or {}).get("error")):
                    retry += ids
                continue
            for r in resp:
                k = r.get("id")
                if "error" in r:
                    if _is_rate_limited(r["error"]):
                        RATE_LIMITED[0] += 1
                        retry.append(k)
                elif isinstance(k, int) and 0 <= k < len(calls):
                    out[k] = r.get("result")
        if not retry or a == _tries - 1:
            break
        todo = retry
        _backoff(a)
    return out


_TX, _TX_LOCK = {}, threading.Lock()
_SLOT_TS = {}  # slot -> unix_time, filled from transactions


def get_transactions(sigs):
    """{signature: jsonParsed transaction | None}, with an in-memory cache."""
    with _TX_LOCK:
        need = [s for s in dict.fromkeys(sigs) if s not in _TX]
    res = rpc_batch([("getTransaction", [s, TXOPT]) for s in need])
    with _TX_LOCK:
        if len(_TX) > TX_CACHE_MAX:  # long-lived server: the cache does not grow forever
            _TX.clear()
        for s, tx in zip(need, res):
            if tx:
                _TX[s] = tx
                if tx.get("blockTime") is not None:
                    _SLOT_TS[tx["slot"]] = tx["blockTime"]
        return {s: _TX.get(s) for s in sigs}


def signatures(addr, before=None, limit=SIG_PAGE, max_pages=1):
    """Address signature history, newest → oldest: (list, hit_max_pages)."""
    out, cur = [], before
    for _ in range(max_pages):
        p = {"limit": limit}
        if cur:
            p["before"] = cur
        page = rpc("getSignaturesForAddress", [addr, p])
        out += page
        if len(page) < limit:
            return out, False
        cur = page[-1]["signature"]
    return out, True


# ---------------------------------------------------------------------------
# transaction parsing
# ---------------------------------------------------------------------------
def tx_keys(tx):
    return [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]


def tx_programs(tx):
    """All programs of the transaction, including inner instructions."""
    ps = {i.get("programId") for i in tx["transaction"]["message"]["instructions"]}
    for g in tx["meta"].get("innerInstructions") or []:
        ps |= {i.get("programId") for i in g["instructions"]}
    return ps


def is_trade(tx):
    return bool(tx_programs(tx) & DEX.keys())


def token_deltas(tx, mint):
    """{owner: Δ raw units of mint} from pre/postTokenBalances, without zeros."""
    d = defaultdict(int)
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in tx["meta"].get(side) or []:
            if b["mint"] == mint and b.get("owner"):
                d[b["owner"]] += sign * int(b["uiTokenAmount"]["amount"])
    return {k: v for k, v in d.items() if v}


def sol_delta(tx, owner):
    """Δ lamports of the owner + Δ of their WSOL (in lamports)."""
    keys = tx_keys(tx)
    d = 0
    if owner in keys:
        i = keys.index(owner)
        d += tx["meta"]["postBalances"][i] - tx["meta"]["preBalances"][i]
    return d + token_deltas(tx, WSOL).get(owner, 0)


def traded_mints(tx, owner):
    """Mint whose balance for the owner changed in the transaction."""
    ms = {b["mint"] for side in ("preTokenBalances", "postTokenBalances")
          for b in tx["meta"].get(side) or [] if b.get("owner") == owner}
    return {m for m in ms if token_deltas(tx, m).get(owner)}


def _legs(tx, mint):
    """Transfer legs of mint in the transaction: [(frm, to, amount)]. Each recipient
    gets it from the largest sender (by owner); with no sender (mint) — from mint,
    a sender without a recipient (burn) — to BURN."""
    d = token_deltas(tx, mint)
    senders = sorted((o for o, v in d.items() if v < 0), key=lambda o: d[o])
    receivers = [o for o, v in d.items() if v > 0]
    legs = [(senders[0] if senders else mint, r, d[r]) for r in receivers]
    if not receivers:
        legs = [(s, BURN, -d[s]) for s in senders]
    return legs


# ---------------------------------------------------------------------------
# adapter API
# ---------------------------------------------------------------------------
_LAUNCH, _HOLDERS = {}, {}
_SUPPLY = {}       # token -> (ts, supply) from the scan (supply_base): early buyers does not ask for it again
SCAN_TTL = 600     # seconds: how long we trust scan data (= scan result cache in server.py)
_LAUNCH_SIGS = {}  # token -> oldest successful curve signatures (old → new), from get_launch: for early buyers
_PDA_SEEN = set()      # PDA owners and PDA counterparties met during the scan (pools, vaults)
_LABEL = {}            # address -> display label (PumpSwap pool, curve, ...)
_OWNER_PROG = {}       # PDA -> owner program
_ENTRY_SIG = {}        # (wallet, entry slot) -> entry signature, for wallet_distinct_tokens


def _parse_curve(data_b64):
    """BondingCurve: virtual_token_reserves@8 (u64), complete@48 (bool),
    creator@49..81 (not present on all old curves)."""
    b = base64.b64decode(data_b64)
    return {"virtual_token_reserves": int.from_bytes(b[8:16], "little") if len(b) >= 16 else None,
            "complete": bool(b[48]) if len(b) > 48 else None,
            "creator": b58encode(b[49:81]) if len(b) >= 81 else None}


def get_launch(token):
    """{"block" (slot), "curve", "deployer", "tx", "ts", "curve_account", "creator",
    "complete", "virtual_token_reserves", "token_program"} or None (not pump.fun).
    Launch = the oldest transaction of the bonding curve (create). The curve's history is short
    (after migration the curve is not traded), so we do not page to the bottom of mint.
    deployer — recipient of the dev buy in the launch transaction; no dev buy — creator."""
    _HOLDERS.pop(token, None)  # every scan starts with get_launch: top holders and account histories are read anew
    for k in [k for k in _ACC_HIST if k[0] == token]:
        del _ACC_HIST[k]
    curve = bonding_curve(token)
    mint_acc, curve_acc = rpc("getMultipleAccounts", [[token, curve], {"encoding": "base64"}])["value"]
    if not mint_acc or mint_acc["owner"] not in (TOKEN, TOKEN_2022) or not curve_acc or curve_acc["owner"] != PUMP:
        return None
    cs = _parse_curve(curve_acc["data"][0])
    tprog = mint_acc["owner"]
    curve_ata = associated_token_account(curve, token, tprog)

    try:
        hist, cut = signatures(curve, limit=LAUNCH_PAGE, max_pages=LAUNCH_MAX_PAGES)
    except RuntimeError:   # provider does not allow limit > 1000
        hist, cut = signatures(curve, limit=SIG_PAGE, max_pages=LAUNCH_MAX_PAGES)
    if cut or not hist:
        raise RuntimeError("launch not found: bonding curve history too long")
    ok = [s for s in reversed(hist) if not s["err"]]
    _LAUNCH_SIGS[token] = ok[:EARLY_TX_MAX]
    for s in ok[:3]:
        tx = get_transactions([s["signature"]])[s["signature"]]
        ixs = list(tx["transaction"]["message"]["instructions"]) if tx else []
        for g in (tx["meta"].get("innerInstructions") or []) if tx else []:
            ixs += g["instructions"]
        create = next((i for i in ixs if i.get("programId") == PUMP and (i.get("accounts") or [None])[0] == token
                       and len(i["accounts"]) > 5 and i["accounts"][1] == PUMP_MINT_AUTH), None)
        if not create:
            continue
        logs = tx["meta"].get("logMessages") or []
        v2 = any(l.endswith("Instruction: CreateV2") for l in logs)
        creator = cs["creator"] or create["accounts"][5 if v2 else 7]
        dev = sorted(((v, o) for o, v in token_deltas(tx, token).items()
                      if v > 0 and o != curve and not is_pda(o)), reverse=True)
        res = {"block": tx["slot"], "curve": curve, "deployer": dev[0][1] if dev else creator,
               "tx": s["signature"], "ts": tx.get("blockTime") or s["blockTime"], "curve_account": curve_ata,
               "creator": creator, "complete": cs["complete"],
               "virtual_token_reserves": cs["virtual_token_reserves"], "token_program": tprog}
        _LABEL[curve] = "bonding curve"
        _LAUNCH[token] = res
        return res
    raise RuntimeError("launch not found: oldest bonding curve tx is not a pump.fun create")


def launched_token(token):
    """{"pair": WSOL, "phase": "curve" | "pool"} or None (not pump.fun). Not cached."""
    curve = bonding_curve(token)
    acc = rpc("getAccountInfo", [curve, {"encoding": "base64"}])["value"]
    if not acc or acc["owner"] != PUMP:
        return None
    return {"pair": WSOL, "phase": "pool" if _parse_curve(acc["data"][0])["complete"] else "curve"}


def launched_pair(token):
    info = launched_token(token)
    return info["pair"] if info else None


def token_supply(token):
    return int(rpc("getTokenSupply", [token])["value"]["amount"])


def top_accounts(token):
    """Top 20 token accounts: [{"account", "owner", "amount", "pda", "label"}], cached.
    PDA owners (pools, curve, lockers) are remembered as infrastructure."""
    if token in _HOLDERS:
        return _HOLDERS[token]
    largest = rpc("getTokenLargestAccounts", [token])["value"]
    accs = [x["address"] for x in largest]
    infos = rpc("getMultipleAccounts", [accs, {"encoding": "jsonParsed"}])["value"] if accs else []
    rows = []
    for x, info in zip(largest, infos):
        owner = info["data"]["parsed"]["info"]["owner"] if info else None
        rows.append({"account": x["address"], "owner": owner, "amount": int(x["amount"]),
                     "pda": bool(owner) and is_pda(owner)})
    pdas = sorted({r["owner"] for r in rows if r["pda"]} - _LABEL.keys())
    if pdas:  # PDA labels by owner program: in one call
        for a, info in zip(pdas, rpc("getMultipleAccounts", [pdas, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])["value"]):
            prog = info["owner"] if info else None
            _OWNER_PROG[a] = prog
            _LABEL[a] = ({PUMP_SWAP: "PumpSwap pool", PUMP: "pump.fun"}.get(prog) or
                         (f"{VAULT_AUTHORITIES[a]} pool" if a in VAULT_AUTHORITIES else None) or
                         ("system-owned PDA" if prog == SYSTEM else f"PDA of {DEX.get(prog, prog)}"))
    for r in rows:
        if r["owner"] == BURN:
            _LABEL[r["owner"]] = "burn"
        if r["pda"] or r["owner"] in INFRA:
            _PDA_SEEN.add(r["owner"])
        r["label"] = _LABEL.get(r["owner"], "wallet")
    _HOLDERS[token] = rows
    return rows


def supply_base(token, launch):
    """Same shape as detect.supply_base: {"supply", "circulating", "holders_total", "balances"}.
    circulating = supply minus everything held by excluded addresses (curve, pools, burn, PDA) in the top 20;
    balances — wallet owners in the top 20 (several accounts of one owner are summed).
    holders_total — a lower bound: wallets with a non-zero balance in the top 20."""
    supply = token_supply(token)
    _SUPPLY[token] = (time.time(), supply)
    rows = top_accounts(token)
    excluded = excluded_addresses(launch["curve"])
    bal = defaultdict(int)
    infra = 0
    for r in rows:
        if r["owner"] is None or r["owner"] in excluded or r["account"] in excluded:
            infra += r["amount"]
        elif r["amount"] > 0:
            bal[r["owner"]] += r["amount"]
    return {"supply": supply, "circulating": supply - infra, "holders_total": len(bal), "balances": dict(bal)}


def is_pool_owner(owner):
    """Token account owner is a DEX pool: a DEX program PDA or a shared vault authority (VAULT_AUTHORITIES)."""
    return owner in VAULT_AUTHORITIES or _OWNER_PROG.get(owner) in DEX


def token_facts(token, launch):
    """Token facts for the engine (shared chain contract): {"supply", "transfers", "excluded",
    "market", "reserve", "reserve_ok", "base"}. base — top 20 balances (detect.supply_base shape); transfers — only
    over the top 20 holders' token accounts (sells, distributors, direct transfers, entries).
    reserve — liquidity tokens for dump_impact: on the curve — its virtual_token_reserves (the curve
    prices off them; no field — the curve's real balance), after migration (complete) — the real
    balance of the pools: top 20 accounts whose owner is a DEX pool (is_pool_owner: a DEX program PDA or
    a Raydium/Meteora vault authority). reserve_ok — the reserve is reliably known: always on the curve,
    after migration — only if a pool was found (reserve > 0; 0 means "not found", not "no liquidity")."""
    base = supply_base(token, launch)
    transfers = get_token_transfers(token, launch["block"])
    rows = top_accounts(token)
    if launch.get("complete"):
        reserve = sum(r["amount"] for r in rows if r["owner"] != launch["curve"] and is_pool_owner(r["owner"]))
        ok = reserve > 0
    elif launch.get("virtual_token_reserves"):
        reserve, ok = launch["virtual_token_reserves"], True
    else:
        reserve, ok = sum(r["amount"] for r in rows if r["owner"] == launch["curve"]), True
    return {"supply": base["supply"], "transfers": transfers, "excluded": excluded_addresses(launch["curve"]),
            "market": market_addresses(launch["curve"]), "reserve": reserve, "reserve_ok": ok, "base": base}


def market_addresses(curve):
    """Token market: curve + DEX programs + PDAs met in the scan (pools, vaults, routers)."""
    return {curve} | set(DEX) | _PDA_SEEN


def excluded_addresses(curve):
    """Not holders: infrastructure + token market + the curve's token account."""
    out = INFRA | market_addresses(curve)
    for l in _LAUNCH.values():
        if l["curve"] == curve:
            out.add(l["curve_account"])
    return out


_ACC_HIST = {}  # (token, account) -> (signatures old → new, truncated)


def _account_history(token, accounts):
    """History of holders' token accounts: one getSignaturesForAddress batch for all of them."""
    need = [a for a in accounts if (token, a) not in _ACC_HIST]
    res = rpc_batch([("getSignaturesForAddress", [a, {"limit": SIG_PAGE}]) for a in need], chunk=20)
    for a, page in zip(need, res):
        if page is None:
            continue
        cut = len(page) >= SIG_PAGE
        if cut and HOLDER_MAX_PAGES > 1:
            more, cut = signatures(a, before=page[-1]["signature"], max_pages=HOLDER_MAX_PAGES - 1)
            page = page + more
        _ACC_HIST[(token, a)] = ([s for s in reversed(page) if not s["err"]], cut)
    return {a: _ACC_HIST.get((token, a)) for a in accounts}


def get_token_transfers(token, from_block=0, wallets=None):
    """Token transfers over the token accounts of wallets in wallets (default — top 20 owners),
    starting from slot from_block: [{"frm", "to", "amount", "tx", "block" (slot), "log_index"}],
    in chain order. HOLDER_TX_CAP transactions are read per account: the oldest (entry) and the
    newest (sells, transfers); history longer than HOLDER_MAX_PAGES pages — the account is skipped."""
    rows = top_accounts(token)
    want = set(wallets) if wallets is not None else {r["owner"] for r in rows if not r["pda"] and r["owner"]}
    accs = [r["account"] for r in rows if r["owner"] in want]
    hist = _account_history(token, accs)
    pick = {}
    for a in accs:
        h = hist.get(a)
        if not h or h[1]:
            continue
        sigs = h[0]
        chosen = sigs if len(sigs) <= HOLDER_TX_CAP else sigs[:10] + sigs[-(HOLDER_TX_CAP - 10):]
        for s in chosen:
            if s["slot"] >= from_block:
                pick[s["signature"]] = s
    txs = get_transactions(list(pick))
    out, seen = [], set()
    for sig, s in pick.items():
        tx = txs.get(sig)
        if not tx:
            continue
        for i, (frm, to, amt) in enumerate(_legs(tx, token)):
            for a in (frm, to):
                if a and a != token and is_pda(a):
                    _PDA_SEEN.add(a)
            if (frm in want or to in want) and (sig, frm, to) not in seen:
                seen.add((sig, frm, to))
                out.append({"frm": frm, "to": to, "amount": amt, "tx": sig, "block": tx["slot"],
                            "log_index": i, "_ti": s.get("transactionIndex") or 0})
    out.sort(key=lambda t: (t["block"], t.pop("_ti"), t["tx"], t["log_index"]))
    return out


def _buy_lamports(tx, token, wallet, market):
    """Lamports spent on the buy: SOL/WSOL received by the market PDAs that gave out the token
    (curve — in lamports, pool — via its WSOL vault). None if there are several token recipients
    or what the market gave does not match what the wallet received (tolerance BUY_MATCH)."""
    d = token_deltas(tx, token)
    got = d.get(wallet, 0)
    receivers = {o for o, v in d.items() if v > 0 and o not in market}
    if got <= 0 or receivers != {wallet}:
        return None
    sources = [o for o, v in d.items() if v < 0 and o in market]
    out = -sum(d[o] for o in sources)
    if not sources or abs(out - got) > BUY_MATCH * got:
        return None
    paid = sum(max(0, sol_delta(tx, o)) for o in sources)
    return paid or None


def classify_entries(token, transfers, wallets):
    """First receipt of the token by each wallet, per transaction:
    {wallet: {"kind": "buy"|"transfer"|None, "tx", "block" (slot), "via", "eth_in" (lamports), "unread"}}.
    buy — a DEX program takes part in the transaction (DEX, including inner instructions) and the wallet
    received the token; otherwise transfer, via — the sender. eth_in — see _buy_lamports, None for transfer.
    Entry not found (account history not read or truncated) — kind None, unread True."""
    want = set(wallets)
    first = {}
    for t in transfers:
        if t["to"] in want and t["to"] not in first:
            first[t["to"]] = t
    txs = get_transactions([t["tx"] for t in first.values()])
    curve = next((l["curve"] for k, l in _LAUNCH.items() if k == token), None)
    market = market_addresses(curve) if curve else set(DEX) | _PDA_SEEN
    out = {}
    for w in wallets:
        t = first.get(w)
        tx = txs.get(t["tx"]) if t else None
        if not tx:
            out[w] = {"kind": None, "tx": None, "block": None, "via": None, "eth_in": None, "unread": True}
            continue
        buy = is_trade(tx)
        out[w] = {"kind": "buy" if buy else "transfer", "tx": t["tx"], "block": t["block"], "via": t["frm"],
                  "eth_in": _buy_lamports(tx, token, w, market) if buy else None, "unread": False}
        _ENTRY_SIG[(w, t["block"])] = t["tx"]
    return out


def sellers(transfers, market):
    """Wallets that sent the token to the market at least once (like detect.sellers)."""
    return {t["frm"] for t in transfers if t["to"] in market and t["frm"] not in market}


def entry_groups(entries):
    """Buyer groups: {"same_tx": {signature: [wallets]}, "same_slot": {slot: [wallets]}}, ≥ 2 per group."""
    by_tx, by_slot = defaultdict(list), defaultdict(list)
    for w, e in entries.items():
        if e.get("kind") == "buy":
            by_tx[e["tx"]].append(w)
            by_slot[e["block"]].append(w)
    return {"same_tx": {k: sorted(v) for k, v in by_tx.items() if len(v) > 1},
            "same_slot": {k: sorted(v) for k, v in by_slot.items() if len(v) > 1}}


def block_timestamps(blocks):
    """{slot: unix_time}: from already read transactions, the rest via a getBlockTime batch."""
    blocks = {b for b in blocks if b is not None}
    need = sorted(blocks - _SLOT_TS.keys())
    for b, ts in zip(need, rpc_batch([("getBlockTime", [b]) for b in need], chunk=100)):
        if ts is not None:
            _SLOT_TS[b] = ts
    return {b: _SLOT_TS[b] for b in blocks if b in _SLOT_TS}


def wallet_distinct_tokens(wallet, before_block, skip_token, window=None, cap=4):
    """How many distinct mints the wallet traded (a transaction with a DEX program, mint balance changed)
    before entry, not counting skip_token and SOL/WSOL/USDC/USDT. min(n, cap); early exit at cap.
    before_block — entry slot (the entry signature is taken from classify_entries).
    Reads the last HISTORY_TX_CAP transactions before entry; if they do not fit and n < cap — None (unread).
    window is not supported (whole history only)."""
    before = _ENTRY_SIG.get((wallet, before_block))
    if before is None:  # entry not from classify_entries: we do not know the history boundary
        return None
    sigs, cut = signatures(wallet, before=before, limit=SIG_PAGE)
    ok = [s["signature"] for s in sigs if not s["err"]]
    mints = set()
    read = 0
    for i in range(0, min(len(ok), HISTORY_TX_CAP), HISTORY_CHUNK):
        part = ok[i:i + HISTORY_CHUNK]
        txs = get_transactions(part)
        read += len(part)
        for s in part:
            tx = txs.get(s)
            if tx and is_trade(tx):
                mints |= traded_mints(tx, wallet) - STABLES - {skip_token}
        if len(mints) >= cap:
            return cap
    if read < len(ok) or cut:
        return None
    return len(mints)


_PROG = {}


def is_contract(addresses):
    """{addr: PDA or executable account}, with an in-memory cache."""
    addrs = set(addresses)
    need = sorted(a for a in addrs if a not in _PROG and not is_pda(a))
    for i in range(0, len(need), 100):
        part = need[i:i + 100]
        res = rpc("getMultipleAccounts", [part, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])["value"]
        for a, acc in zip(part, res):
            _PROG[a] = bool(acc and acc.get("executable"))
    return {a: is_pda(a) or _PROG.get(a, False) for a in addrs}


def eth_inflows(wallet, before_block, max_count=1000):
    raise NotImplementedError("funding is off on Solana (USE_FUNDING = False)")


def outgoing_count(addr, cap=100):
    raise NotImplementedError("funding is off on Solana (USE_FUNDING = False)")


def _borsh_str(b, i):
    n = int.from_bytes(b[i:i + 4], "little")
    return b[i + 4:i + 4 + n].decode(errors="replace").rstrip("\0"), i + 4 + n


def token_meta(token):
    """{"name", "symbol"}: the tokenMetadata extension for Token-2022, otherwise the Metaplex account."""
    acc = rpc("getAccountInfo", [token, {"encoding": "jsonParsed"}])["value"]
    info = (((acc or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
    for ext in info.get("extensions") or []:
        if ext.get("extension") == "tokenMetadata":
            st = ext.get("state") or {}
            return {"name": st.get("name", ""), "symbol": st.get("symbol", "")}
    meta = find_pda([b"metadata", b58decode(METAPLEX), b58decode(token)], METAPLEX)
    m = rpc("getAccountInfo", [meta, {"encoding": "base64"}])["value"]
    if not m:
        return {"name": "", "symbol": ""}
    b = base64.b64decode(m["data"][0])
    name, i = _borsh_str(b, 1 + 32 + 32)
    sym, _ = _borsh_str(b, i)
    return {"name": name, "symbol": sym}


# ---------------------------------------------------------------------------
# early buyers: first buyers after launch and what they did with the token
# ---------------------------------------------------------------------------
def early_buyers(token, n=20, deadline=None):
    """First n unique buyers from the curve: {"launch": {"block", "ts", "deployer", "curve"}, "buyers": [{"wallet",
    "block", "ts", "tx", "bought", "account"}], "txs_read"} or None (not pump.fun). Does not change over time.
    Buyer — a non-PDA owner whose balance grew in a transaction with a DEX program (the dev buy in create counts too);
    bought — how much they got in the first buy, account — the entry token account. Curve transactions are the oldest
    signatures get_launch already read (EARLY_TX_MAX); not in memory — get_launch again."""
    if token not in _LAUNCH or token not in _LAUNCH_SIGS:
        if get_launch(token) is None:
            return None
    launch, sigs = _LAUNCH[token], _LAUNCH_SIGS[token]
    buyers, seen, pos = [], set(), 0
    while len(buyers) < n and pos < len(sigs) and not (deadline and time.time() > deadline):
        part = sigs[pos:pos + EARLY_TX_CHUNK]
        pos += len(part)
        txs = get_transactions([x["signature"] for x in part])
        for x in part:
            tx = txs.get(x["signature"])
            if not tx or not is_trade(tx):
                continue
            keys, d = tx_keys(tx), token_deltas(tx, token)
            for b in tx["meta"].get("postTokenBalances") or []:
                o = b.get("owner")
                if b["mint"] != token or not o or o in seen or o == launch["curve"] or is_pda(o) or d.get(o, 0) <= 0:
                    continue
                seen.add(o)
                buyers.append({"wallet": o, "block": tx["slot"], "ts": tx.get("blockTime") or x.get("blockTime"),
                               "tx": x["signature"], "bought": d[o], "account": keys[b["accountIndex"]]})
                if len(buyers) >= n:
                    break
            if len(buyers) >= n:
                break
    return {"launch": {"block": launch["block"], "ts": launch["ts"], "deployer": launch["deployer"],
                       "curve": launch["curve"]}, "buyers": buyers, "txs_read": pos}


def early_status(token, buyers, deadline=None, launch=None, out_cap=EARLY_OUT_CAP):
    """What buyers did with the token: {"supply", "wallets": {wallet: {"now", "sold", "moved": {to: amount},
    "burned", "partial"}}}. now — balance of the entry token account (closed — 0; one getMultipleAccounts).
    Whoever holds less than they bought — exits are parsed from the account history (out_cap newest transactions):
    a transaction with a DEX program is a sell, otherwise a transfer to recipients (no recipient or to incinerator — burned).
    partial — history longer than out_cap, did not finish before deadline, or the parsed exits do not explain the balance drop.
    launch — from early_buyers (shared chain contract; not needed here: entry accounts are already known).
    Reused from the scan: launch and curve signatures (_LAUNCH, _LAUNCH_SIGS), transactions (_TX) and supply (_SUPPLY);
    buyers' balances and account histories are read anew: the scan only looks at the top 20, and sells must be fresh."""
    hit = _SUPPLY.get(token)
    supply = hit[1] if hit and time.time() - hit[0] < SCAN_TTL else token_supply(token)
    accs = [b["account"] for b in buyers]
    infos = rpc("getMultipleAccounts", [accs, {"encoding": "jsonParsed"}])["value"] if accs else []
    out = {}
    for b, info in zip(buyers, infos):
        amt = (((info or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("tokenAmount", {}).get("amount")
        out[b["wallet"]] = {"now": int(amt or 0), "sold": 0, "moved": {}, "burned": 0, "partial": False}
    need = [b for b in buyers if out[b["wallet"]]["now"] < b["bought"]]
    if need and deadline and time.time() > deadline:
        for b in need:
            out[b["wallet"]]["partial"] = True
        return {"supply": supply, "wallets": out}
    pages = rpc_batch([("getSignaturesForAddress", [b["account"], {"limit": SIG_PAGE}]) for b in need], chunk=20)
    pick = {}
    for b, page in zip(need, pages):
        sigs = [x["signature"] for x in page or [] if not x["err"] and x["slot"] >= b["block"] and x["signature"] != b["tx"]]
        pick[b["wallet"]] = sigs[:out_cap]
        out[b["wallet"]]["partial"] = page is None or len(page) >= SIG_PAGE or len(sigs) > out_cap
    if deadline and time.time() > deadline:
        for b in need:
            out[b["wallet"]]["partial"] = True
        return {"supply": supply, "wallets": out}
    txs = get_transactions([x for v in pick.values() for x in v])
    for w, sigs in pick.items():
        o = out[w]
        for sig in sigs:
            tx = txs.get(sig)
            if not tx:
                o["partial"] = True
                continue
            d = token_deltas(tx, token)
            gone = -d.get(w, 0)
            if gone <= 0:
                continue
            if is_trade(tx):
                o["sold"] += gone
                continue
            legs = [(r, a) for f, r, a in _legs(tx, token) if f == w and r != w]
            if not legs:   # not the largest sender in the transaction: recipient is the largest receiver
                rec = sorted((v, r) for r, v in d.items() if v > 0 and r != w)
                legs = [(rec[-1][1], rec[-1][0])] if rec else []
            total = sum(a for _, a in legs)
            if not total:  # no recipient: burned
                o["burned"] += gone
            for r, a in legs if total else []:
                part = gone * a // total
                if r == BURN:
                    o["burned"] += part
                else:
                    o["moved"][r] = o["moved"].get(r, 0) + part
    for b in need:   # parsed exits do not explain where the bought amount went (> 1%) — not everything was parsed
        o = out[b["wallet"]]
        if b["bought"] - o["now"] - o["sold"] - o["burned"] - sum(o["moved"].values()) > b["bought"] * 0.01:
            o["partial"] = True
    return {"supply": supply, "wallets": out}
