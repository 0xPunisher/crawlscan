"""Адаптер сети Solana (токены pump.fun) для rh-crawler.
Единственное место, которое ходит в Solana: JSON-RPC (SOLANA_RPC) с ретраями и общим
лимитером запросов (SOLANA_RPS), батчи вызовов в одном HTTP.
Read-only: ни ключей, ни подписи, ни отправки транзакций.

Соответствие понятий Robinhood → Solana (ключи и типы те же, что у chains/robinhood.py):
- адрес: base58 (32 байта), регистр значим — в нижний регистр НЕ переводим;
- block → слот (int); block_timestamps → {слот: unix_time};
- tx → подпись транзакции (base58);
- eth_in → лампорты (int, 1 SOL = 10**9), а не wei; пара всегда SOL;
- curve → PDA бондинг-кривой pump.fun ["bonding-curve", mint];
- контракт (is_contract) → PDA (точка вне кривой ed25519) или исполняемый аккаунт;
- холдер → владелец токен-аккаунта (owner), а не сам токен-аккаунт;
- transfers → переводы только по токен-аккаунтам топ-холдеров (полной истории
  переводов токена тут нет: её дорого читать). Каждая нога: кто отдал/получил
  (владельцы, по pre/postTokenBalances транзакции), сколько, подпись, слот.

Чем Solana-поток отличается от Robinhood (движок получает всё через token_facts):
- балансы и оборот берутся не из переводов, а из supply_base(token, launch)
  (getTokenLargestAccounts): та же форма, что у detect.supply_base, отдаётся в facts["base"];
  holders_total — нижняя оценка (Alchemy не отдаёт getProgramAccounts по mint);
  точное число, если аккаунтов меньше 20;
- excluded_addresses/market_addresses дополняются PDA, найденными по ходу скана
  (пулы, хранилища, локеры), поэтому их зовут после supply_base/get_token_transfers;
- classify_entries для кошелька без найденного входа отдаёт kind=None, unread=True;
- wallet_distinct_tokens отдаёт None, если в лимит чтения не уложились (не угадываем);
- стая — покупки в пределах PACK_WINDOW слотов, а не в одном блоке.
Фандинг не реализован (USE_FUNDING = False): eth_inflows/outgoing_count — NotImplementedError.
"""
import base64, hashlib, json, os, random, sys, threading, time, urllib.error, urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from env import load_dotenv
load_dotenv()

RPC = os.environ.get("SOLANA_RPC")
RPS = int(os.environ.get("SOLANA_RPS", "8"))  # потолок HTTP-запросов/сек глобально
USE_FUNDING = False
CHAIN = "solana"
PACK_WINDOW = 2            # окно стаи (detect.find_packs): до 2 слотов от первого покупателя

# --- программы и аккаунты ---
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"            # pump.fun (бондинг-кривые)
PUMP_SWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"       # PumpSwap AMM (пулы после миграции)
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

DEX = {  # участие в транзакции = сделка (роутеры ботов вызывают эти программы внутренними инструкциями)
    PUMP: "pump.fun",
    PUMP_SWAP: "PumpSwap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM v4",
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C": "Raydium CPMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj": "Raydium LaunchLab",
    "routeUGWgWzqBWFcrCfv8tritsqukccJPu3q5GPP3xS": "Raydium Route",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9t2ijHo": "Meteora DLMM",
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB": "Meteora DAMM",
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG": "Meteora DAMM v2",
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN": "Meteora DBC",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
    "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma": "OKX DEX",
}
INFRA = {  # не холдеры ни для какого токена; кривая, её аккаунт и PDA рынка — в excluded_addresses
    BURN, SYSTEM, PUMP, PUMP_SWAP, PUMP_MINT_AUTH, PUMP_GLOBAL, PUMP_MIGRATION, PUMP_FEE,
} | set(DEX)

SIG_PAGE = 1000            # стандартный лимит getSignaturesForAddress
LAUNCH_PAGE = 10000        # Alchemy отдаёт до 10000 за страницу; не пустит — откатываемся на SIG_PAGE
LAUNCH_MAX_PAGES = 10      # история кривой глубже — запуск не ищем
HOLDER_MAX_PAGES = 2       # история токен-аккаунта холдера глубже — вход не ищем (unread)
HOLDER_TX_CAP = 30         # транзакций токен-аккаунта читаем: 10 самых старых (вход) + 20 новых (продажи)
HISTORY_TX_CAP = 100       # транзакций кошелька до входа читаем для wallet_distinct_tokens
HISTORY_CHUNK = 25         # ... кусками, с ранним выходом
TX_BATCH = 50              # getTransaction в одном HTTP
TX_CACHE_MAX = 50_000      # транзакций в кэше процесса
BUY_MATCH = 0.06           # допуск сверки: рынок отдал ≈ кошелёк получил (комиссии, налог)
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
    """Строка — адрес Solana (base58, 32 байта)."""
    try:
        return isinstance(s, str) and 32 <= len(s) <= 44 and len(b58decode(s)) == 32
    except ValueError:
        return False


_P = 2 ** 255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P


def on_curve(b):
    """32 байта — точка кривой ed25519 (обычный кошелёк), иначе PDA."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    y %= _P
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    return x2 == 0 or pow(x2, (_P - 1) // 2, _P) == 1


_ONC = {}


def is_pda(addr):
    """Адрес вне кривой ed25519 = PDA программы (пул, кривая, хранилище), не кошелёк."""
    if addr not in _ONC:
        _ONC[addr] = not on_curve(b58decode(addr))
    return _ONC[addr]


def find_pda(seeds, program):
    """findProgramAddress: первый bump с 255 вниз, дающий точку вне кривой."""
    pid = b58decode(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58encode(h)
    raise ValueError("PDA не найден")


def bonding_curve(mint):
    return find_pda([b"bonding-curve", b58decode(mint)], PUMP)


def associated_token_account(owner, mint, token_program=TOKEN):
    return find_pda([b58decode(owner), b58decode(token_program), b58decode(mint)], ATA_PROGRAM)


# ---------------------------------------------------------------------------
# RPC
# ---------------------------------------------------------------------------
_MIN_GAP = 1.0 / RPS
_rl_lock = threading.Lock()
_next_slot = [0.0]


def _rate_limit():
    # Разносит HTTP-запросы во времени. Вес — один HTTP, а не элемент батча: Alchemy Solana
    # ограничивает compute units, и батч из 100 getTransaction проходит без 429; per-item 429 ретраим.
    with _rl_lock:
        now = time.time()
        wait = _next_slot[0] - now
        if wait > 0:
            time.sleep(wait)
        _next_slot[0] = max(now, _next_slot[0]) + _MIN_GAP


REQUESTS = [0]  # счётчик HTTP-запросов к RPC (включая ретраи)


def _is_rate_limited(err):
    if not isinstance(err, dict):
        return False
    msg = str(err.get("message", "")).lower()
    return err.get("code") == 429 or any(k in msg for k in ("rate limit", "per second", "too many requests"))


def _backoff(a):
    time.sleep(0.35 * (2 ** a) + random.random() * 0.3)


def _post(payload, _tries=5):
    """POST с ретраями и бэкоффом при 429/сбое."""
    if not RPC:
        raise RuntimeError("SOLANA_RPC не задан")
    for a in range(_tries):
        _rate_limit()
        REQUESTS[0] += 1
        req = urllib.request.Request(RPC, data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json", "User-Agent": "rh-crawler"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = json.loads(r.read())
            if isinstance(body, dict) and _is_rate_limited(body.get("error")) and a < _tries - 1:
                _backoff(a); continue
            return body
        except urllib.error.HTTPError as e:
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
    """Много вызовов батчами по chunk в одном HTTP. calls=[(method, params)].
    Результаты в исходном порядке; None — ошибка элемента. Элементы с 429 повторяются."""
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
                        retry.append(k)
                elif isinstance(k, int) and 0 <= k < len(calls):
                    out[k] = r.get("result")
        if not retry or a == _tries - 1:
            break
        todo = retry
        _backoff(a)
    return out


_TX, _TX_LOCK = {}, threading.Lock()
_SLOT_TS = {}  # слот -> unix_time, заполняется из транзакций


def get_transactions(sigs):
    """{подпись: транзакция jsonParsed | None}, с кэшем в памяти."""
    with _TX_LOCK:
        need = [s for s in dict.fromkeys(sigs) if s not in _TX]
    res = rpc_batch([("getTransaction", [s, TXOPT]) for s in need])
    with _TX_LOCK:
        if len(_TX) > TX_CACHE_MAX:  # долгоживущий сервер: кэш не растёт бесконечно
            _TX.clear()
        for s, tx in zip(need, res):
            if tx:
                _TX[s] = tx
                if tx.get("blockTime") is not None:
                    _SLOT_TS[tx["slot"]] = tx["blockTime"]
        return {s: _TX.get(s) for s in sigs}


def signatures(addr, before=None, limit=SIG_PAGE, max_pages=1):
    """История подписей адреса, новые → старые: (список, упёрлись_в_max_pages)."""
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
# разбор транзакции
# ---------------------------------------------------------------------------
def tx_keys(tx):
    return [k["pubkey"] if isinstance(k, dict) else k for k in tx["transaction"]["message"]["accountKeys"]]


def tx_programs(tx):
    """Все программы транзакции, включая внутренние инструкции."""
    ps = {i.get("programId") for i in tx["transaction"]["message"]["instructions"]}
    for g in tx["meta"].get("innerInstructions") or []:
        ps |= {i.get("programId") for i in g["instructions"]}
    return ps


def is_trade(tx):
    return bool(tx_programs(tx) & DEX.keys())


def token_deltas(tx, mint):
    """{владелец: Δ сырых единиц mint} по pre/postTokenBalances, без нулей."""
    d = defaultdict(int)
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in tx["meta"].get(side) or []:
            if b["mint"] == mint and b.get("owner"):
                d[b["owner"]] += sign * int(b["uiTokenAmount"]["amount"])
    return {k: v for k, v in d.items() if v}


def sol_delta(tx, owner):
    """Δ лампортов владельца + Δ его WSOL (в лампортах)."""
    keys = tx_keys(tx)
    d = 0
    if owner in keys:
        i = keys.index(owner)
        d += tx["meta"]["postBalances"][i] - tx["meta"]["preBalances"][i]
    return d + token_deltas(tx, WSOL).get(owner, 0)


def traded_mints(tx, owner):
    """Mint, чей баланс у владельца изменился в транзакции."""
    ms = {b["mint"] for side in ("preTokenBalances", "postTokenBalances")
          for b in tx["meta"].get(side) or [] if b.get("owner") == owner}
    return {m for m in ms if token_deltas(tx, m).get(owner)}


def _legs(tx, mint):
    """Ноги перевода mint в транзакции: [(frm, to, amount)]. Каждый получатель —
    от крупнейшего отдавшего (по владельцам); без отдавшего (минт) — от mint,
    отдавший без получателя (сжёг) — в BURN."""
    d = token_deltas(tx, mint)
    senders = sorted((o for o, v in d.items() if v < 0), key=lambda o: d[o])
    receivers = [o for o, v in d.items() if v > 0]
    legs = [(senders[0] if senders else mint, r, d[r]) for r in receivers]
    if not receivers:
        legs = [(s, BURN, -d[s]) for s in senders]
    return legs


# ---------------------------------------------------------------------------
# API адаптера
# ---------------------------------------------------------------------------
_LAUNCH, _HOLDERS = {}, {}
_PDA_SEEN = set()      # PDA-владельцы и PDA-контрагенты, встреченные по ходу скана (пулы, хранилища)
_LABEL = {}            # адрес -> подпись для вывода (пул PumpSwap, кривая, ...)
_OWNER_PROG = {}       # PDA -> программа-владелец
_ENTRY_SIG = {}        # (кошелёк, слот входа) -> подпись входа, для wallet_distinct_tokens


def _parse_curve(data_b64):
    """BondingCurve: virtual_token_reserves@8 (u64), complete@48 (bool),
    creator@49..81 (есть не у всех старых кривых)."""
    b = base64.b64decode(data_b64)
    return {"virtual_token_reserves": int.from_bytes(b[8:16], "little") if len(b) >= 16 else None,
            "complete": bool(b[48]) if len(b) > 48 else None,
            "creator": b58encode(b[49:81]) if len(b) >= 81 else None}


def get_launch(token):
    """{"block" (слот), "curve", "deployer", "tx", "ts", "curve_account", "creator",
    "complete", "virtual_token_reserves", "token_program"} или None (не pump.fun).
    Запуск = самая старая транзакция бондинг-кривой (create). История кривой короткая
    (после миграции кривая не торгуется), поэтому до дна mint не листаем.
    deployer — получатель дев-бая в транзакции запуска; нет дев-бая — creator."""
    _HOLDERS.pop(token, None)  # каждый скан начинается с get_launch: топ и истории аккаунтов читаем заново
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
    except RuntimeError:   # провайдер не пускает limit > 1000
        hist, cut = signatures(curve, limit=SIG_PAGE, max_pages=LAUNCH_MAX_PAGES)
    if cut or not hist:
        raise RuntimeError("launch not found: bonding curve history too long")
    ok = [s for s in reversed(hist) if not s["err"]]
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
    """{"pair": WSOL, "phase": "curve" | "pool"} или None (не pump.fun). Не кэшируется."""
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
    """Топ-20 токен-аккаунтов: [{"account", "owner", "amount", "pda", "label"}], с кэшем.
    PDA-владельцы (пулы, кривая, локеры) запоминаются как инфраструктура."""
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
    if pdas:  # подписи PDA по программе-владельцу: одним вызовом
        for a, info in zip(pdas, rpc("getMultipleAccounts", [pdas, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])["value"]):
            prog = info["owner"] if info else None
            _OWNER_PROG[a] = prog
            _LABEL[a] = {PUMP_SWAP: "PumpSwap pool", PUMP: "pump.fun", SYSTEM: "system-owned PDA"}.get(prog, f"PDA of {DEX.get(prog, prog)}")
    for r in rows:
        if r["owner"] == BURN:
            _LABEL[r["owner"]] = "burn"
        if r["pda"] or r["owner"] in INFRA:
            _PDA_SEEN.add(r["owner"])
        r["label"] = _LABEL.get(r["owner"], "wallet")
    _HOLDERS[token] = rows
    return rows


def supply_base(token, launch):
    """Та же форма, что detect.supply_base: {"supply", "circulating", "holders_total", "balances"}.
    circulating = сапплай минус всё, что лежит у исключённых (кривая, пулы, burn, PDA) в топ-20;
    balances — владельцы-кошельки топ-20 (несколько аккаунтов одного владельца суммируются).
    holders_total — нижняя оценка: кошельки с ненулевым балансом в топ-20."""
    supply = token_supply(token)
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


def token_facts(token, launch):
    """Факты о токене для движка (общий контракт сетей): {"supply", "transfers", "excluded",
    "market", "reserve", "base"}. base — балансы топ-20 (форма detect.supply_base); transfers — только
    по токен-аккаунтам холдеров топ-20 (продажи, раздатчики, прямые переводы, входы).
    reserve — токены ликвидности для dump_impact: на кривой — её virtual_token_reserves (по ним
    кривая считает цену; нет поля — реальный баланс кривой), после миграции (complete) — реальный
    баланс пулов: аккаунты топ-20, чей владелец — PDA DEX-программы (PumpSwap, Raydium, Meteora, ...)."""
    base = supply_base(token, launch)
    transfers = get_token_transfers(token, launch["block"])
    rows = top_accounts(token)
    if launch.get("complete"):
        reserve = sum(r["amount"] for r in rows
                      if r["owner"] != launch["curve"] and _OWNER_PROG.get(r["owner"]) in DEX)
    elif launch.get("virtual_token_reserves"):
        reserve = launch["virtual_token_reserves"]
    else:
        reserve = sum(r["amount"] for r in rows if r["owner"] == launch["curve"])
    return {"supply": base["supply"], "transfers": transfers, "excluded": excluded_addresses(launch["curve"]),
            "market": market_addresses(launch["curve"]), "reserve": reserve, "base": base}


def market_addresses(curve):
    """Рынок токена: кривая + DEX-программы + PDA, встреченные в скане (пулы, хранилища, роутеры)."""
    return {curve} | set(DEX) | _PDA_SEEN


def excluded_addresses(curve):
    """Не холдеры: инфраструктура + рынок токена + токен-аккаунт кривой."""
    out = INFRA | market_addresses(curve)
    for l in _LAUNCH.values():
        if l["curve"] == curve:
            out.add(l["curve_account"])
    return out


_ACC_HIST = {}  # (token, account) -> (подписи старые → новые, обрезано)


def _account_history(token, accounts):
    """История токен-аккаунтов холдеров: один батч getSignaturesForAddress на все."""
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
    """Переводы токена по токен-аккаунтам кошельков из wallets (по умолчанию — владельцы топ-20),
    начиная со слота from_block: [{"frm", "to", "amount", "tx", "block" (слот), "log_index"}],
    в порядке чейна. Читаются HOLDER_TX_CAP транзакций на аккаунт: самые старые (вход) и самые
    новые (продажи, переводы); история длиннее HOLDER_MAX_PAGES страниц — аккаунт пропускается."""
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
    """Лампорты, ушедшие на покупку: SOL/WSOL, который получили отдавшие токен PDA рынка
    (кривая — лампортами, пул — WSOL-хранилищем). None, если получателей токена несколько
    или отданное рынком не сходится с полученным кошельком (допуск BUY_MATCH)."""
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
    """Первое получение токена каждым кошельком, по транзакции:
    {wallet: {"kind": "buy"|"transfer"|None, "tx", "block" (слот), "via", "eth_in" (лампорты), "unread"}}.
    buy — в транзакции участвует DEX-программа (DEX, включая внутренние инструкции) и кошелёк
    получил токен; иначе transfer, via — отправитель. eth_in — см. _buy_lamports, у transfer None.
    Вход не найден (история аккаунта не прочитана или обрезана) — kind None, unread True."""
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
    """Кошельки, которые хоть раз отдали токен на рынок (как detect.sellers)."""
    return {t["frm"] for t in transfers if t["to"] in market and t["frm"] not in market}


def entry_groups(entries):
    """Группы покупателей: {"same_tx": {подпись: [кошельки]}, "same_slot": {слот: [кошельки]}}, ≥ 2 в группе."""
    by_tx, by_slot = defaultdict(list), defaultdict(list)
    for w, e in entries.items():
        if e.get("kind") == "buy":
            by_tx[e["tx"]].append(w)
            by_slot[e["block"]].append(w)
    return {"same_tx": {k: sorted(v) for k, v in by_tx.items() if len(v) > 1},
            "same_slot": {k: sorted(v) for k, v in by_slot.items() if len(v) > 1}}


def block_timestamps(blocks):
    """{слот: unix_time}: из уже прочитанных транзакций, остальное — getBlockTime батчем."""
    blocks = {b for b in blocks if b is not None}
    need = sorted(blocks - _SLOT_TS.keys())
    for b, ts in zip(need, rpc_batch([("getBlockTime", [b]) for b in need], chunk=100)):
        if ts is not None:
            _SLOT_TS[b] = ts
    return {b: _SLOT_TS[b] for b in blocks if b in _SLOT_TS}


def wallet_distinct_tokens(wallet, before_block, skip_token, window=None, cap=4):
    """Сколько разных mint кошелёк торговал (транзакция с DEX-программой, баланс mint изменился)
    до входа, не считая skip_token и SOL/WSOL/USDC/USDT. min(n, cap); ранний выход на cap.
    before_block — слот входа (подпись входа берётся из classify_entries).
    Читаются HISTORY_TX_CAP последних транзакций до входа; не уложились и n < cap — None (unread).
    window не поддерживается (только вся история)."""
    before = _ENTRY_SIG.get((wallet, before_block))
    if before is None:  # вход не из classify_entries: границу истории не знаем
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
    """{addr: PDA или исполняемый аккаунт}, с кэшем в памяти."""
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
    """{"name", "symbol"}: расширение tokenMetadata у Token-2022, иначе аккаунт Metaplex."""
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
