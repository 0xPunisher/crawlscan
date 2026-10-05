"""Адаптер сети Robinhood Chain (chain id 4663) для rh-crawler.
Единственное место, которое ходит в блокчейн: JSON-RPC с ретраями и общим лимитером RPS,
eth_getLogs с адаптивным окном (при отказе или упоре в лимит ответа окно делится пополам),
определение сделки по движению токена относительно рынка (кривая, пул, роутеры).
Read-only: ни ключей, ни подписи, ни отправки транзакций.
"""
import os, re, sys, time, json, random, threading, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from env import load_dotenv
load_dotenv()

RPC = os.environ["CRAWLER_RPC"]  # Alchemy PAYG endpoint

CHAIN = "robinhood"
PACK_WINDOW = 0           # окно стаи (detect.find_packs): тот же блок
FRESH_WINDOW = 1_000_000  # окно (блоков) для свежести кошелька
FRESH_CAP    = 4          # считаем разные токены до стольких, дальше не нужно

FACTORY  = "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
ROUTERS  = {  # набор роутеров: получил токен ОТ роутера = купил, отдал роутеру = продал
    "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f",
    "0x65050a9b7e5075a2ba5ced7b1b64ee66262c40dc",
    "0x8876789976decbfcbbbe364623c63652db8c0904",
}
INFRA = {  # не холдеры ни для какого токена; кривая и рынок токена — в excluded_addresses
    "0x000000000000000000000000000000000000dead",  # burn
    "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e",  # Pons V2 Factory
    "0x267444d099b10fb5ed7c3cc7b7c767adca574952",  # Pons V2 Locker (LP после выпуска)
    "0x8366a39cc670b4001a1121b8f6a443a643e40951",  # V4 Pool Manager
    "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044",  # Pons V2 Hook
    "0xe33e9e479df8802cb0866d5d05258bec4cf62948",  # LaunchAndBuy
    "0x000000000022d473030f116ddee9f6b43ac78ba3",  # Permit2
    "0xe68d0bbc023de3febda04f413db23ce9c5ea1934",  # Gaslite drop
    "0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # WETH
    "0x0000000000000000000000000000000000000000",
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CURVE_BUY  = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
CURVE_SELL = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
LAUNCH_TOPIC = "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607"  # фабрика: [token, curve, creator]
LAUNCH_SEARCH_SPAN = 9_999_999  # шаг поиска запуска назад; публичный RPC пускает <= 10M блоков на запрос

def u256(data, i):
    return int(data[2 + i*64 : 2 + (i+1)*64], 16)

def _is_rate_limited(body):
    """True, если ответ ноды = rate-limit / превышение пропускной способности."""
    def hit(err):
        if not isinstance(err, dict):
            return False
        code = err.get("code")
        msg = str(err.get("message", "")).lower()
        # ТОЛЬКО настоящие признаки rate-limit. НЕ матчим "exceeded"/"too many"/
        # "capacity" — они есть в ошибке размера окна, которую get_logs обрабатывает
        # разбивкой. Иначе ретрай зря крутит эти запросы по 5 раз -> скан висит.
        return code == 429 or any(k in msg for k in (
            "rate limit", "rate-limit", "per second", "too many requests"))
    if isinstance(body, dict) and "error" in body:
        return hit(body["error"])
    if isinstance(body, list):
        return any(isinstance(x, dict) and hit(x.get("error", {})) for x in body)
    return False

RPS = int(os.environ.get("CRAWLER_RPS", "8"))  # потолок запросов/сек глобально
_MIN_GAP = 1.0 / RPS
_rl_lock = threading.Lock()
_next_slot = [0.0]
def _rate_limit(n=1):
    # разносит запросы во времени, чтобы не превышать лимит провайдера (QuickNode 15 rps и т.п.) и не ловить 429.
    # n — вес запроса: провайдеры считают каждый элемент батча отдельным вызовом.
    with _rl_lock:
        now = time.time()
        wait = _next_slot[0] - now
        if wait > 0:
            time.sleep(wait)
        _next_slot[0] = max(now, _next_slot[0]) + _MIN_GAP * n


REQUESTS = [0]  # счётчик HTTP-запросов к RPC (включая ретраи)

def _post(payload, _tries=5):
    """POST с ретраями и бэкоффом при rate-limit/сбое: один отбитый запрос
    во время нагрузки не должен ронять весь скан."""
    last = None
    for a in range(_tries):
        _rate_limit(len(payload) if isinstance(payload, list) else 1)
        REQUESTS[0] += 1
        req = urllib.request.Request(RPC, data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json", "User-Agent": "Mozilla/5.0 rh-crawler"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = json.loads(r.read())
            if _is_rate_limited(body) and a < _tries - 1:
                last = body; time.sleep(0.35 * (2 ** a) + random.random() * 0.3); continue
            return body
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
            except Exception:
                body = None
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
    """Много JSON-RPC вызовов ОДНИМ HTTP-запросом. calls=[(method,params),...].
    Возвращает список result в исходном порядке (None если ошибка на элементе)."""
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

def addr_from_topic(t):  # 32-байтовый топик -> адрес
    return "0x" + t[-40:].lower()

LOG_CAP = 10_000  # публичный RPC молча обрезает ответ eth_getLogs на стольких логах

def _is_window_error(msg):
    """Ошибка размера окна/ответа getLogs: лечится делением окна."""
    return any(k in msg for k in ("range","too large","too many","limited","response","size","exceed","more than"))

def get_logs(from_block, to_block, address=None, topics=None):
    """getLogs с адаптивным окном: при 'range too large' или упоре в LOG_CAP
    (ответ мог быть молча обрезан) делим окно пополам."""
    flt = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
    if address: flt["address"] = address
    if topics:  flt["topics"] = topics
    try:
        logs = rpc("eth_getLogs", [flt])
    except RuntimeError as e:
        msg = str(e).lower()
        if _is_window_error(msg) and to_block > from_block:
            logs = None
        else:
            raise
    if logs is not None and (len(logs) < LOG_CAP or to_block <= from_block):
        return logs
    mid = (from_block + to_block) // 2
    return (get_logs(from_block, mid, address, topics)
            + get_logs(mid + 1, to_block, address, topics))

def first_log_block(from_block, to_block, address=None, topics=None):
    """Самый ранний блок с логом под фильтр в [from_block, to_block] или None.
    Делит окно, как get_logs, но сначала левую половину: правую читаем,
    только если в левой пусто."""
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
    """Адрес бондинг-кривой токена из его события запуска (topic[2])."""
    bn = block_number()
    for lg in get_logs(bn - lookback, bn, address=FACTORY):
        tp = lg.get("topics") or []
        if len(tp) >= 3 and addr_from_topic(tp[1]) == token.lower():
            return addr_from_topic(tp[2])
    return None

def classify_leg(t, market):
    """Купля/продажа по ноге с рынком токена (его кривая + глобальные роутеры).
    Грабли №1: подписант всегда релеер, поэтому смотрим движение токена, не from tx."""
    if t["frm"] in market and t["to"] not in market:
        return ("buy", t["to"])     # токен пришёл трейдеру с рынка
    if t["to"] in market and t["frm"] not in market:
        return ("sell", t["frm"])   # трейдер отдал токен на рынок
    return (None, None)


V4_SWAP_TOPIC = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
V4_POOL_MGR   = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
WETH_ADDR     = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
def _signed(x): return x-(1<<256) if x>=(1<<255) else x

def v4_swap_quote(receipt_logs, tok_amount):
    """Котировочная сторона (raw) V4-свопа, чья сторона токена ~= tok_amount
    (допуск 6%: комиссии/налог), или None, если такого свопа в логах нет."""
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
    """Из логов транзакции найти V4-своп, чья сторона токена ~= tok_amount,
    и вернуть котировочную сторону (raw) этого свопа. Так выбирается нужный пул
    и определяется, какая из amount0/amount1 — токен, а какая — котировка.
    Возвращает 0, если совпадения нет (не торговый перевод)."""
    if tok_amount<=0: return 0
    best=v4_swap_quote(receipt_logs, tok_amount)
    if best:
        return best
    # FALLBACK: пул, чьё событие свопа мы не декодим (не наш pool manager / другой AMM).
    # Сторона котировки = крупнейший перевод WETH в этой же транзакции. DEX-независимо,
    # поэтому цена читается и на незнакомых пулах, а не падает в None.
    weth = [int(l["data"], 16) for l in receipt_logs
            if (p := parse_transfer(l)) and p["token"] == WETH_ADDR]
    weth = [a for a in weth if a > 0]
    return max(weth) if weth else 0


# ---------------------------------------------------------------------------
# API адаптера для rh-crawler
# ---------------------------------------------------------------------------

def market_addresses(curve):
    """Рынок токена: его кривая + глобальные роутеры + V4 pool manager."""
    return {curve.lower()} | ROUTERS | {V4_POOL_MGR}


def excluded_addresses(curve):
    """Все адреса, которые не считаются холдерами токена: инфраструктура
    (burn, нулевой, фабрика, локер, хук, LaunchAndBuy, ...) + рынок токена."""
    return INFRA | market_addresses(curve)


def get_launch(token):
    """{"block", "curve", "deployer", "tx"} или None.
    Событие запуска ищем на фабрике назад окнами по LAUNCH_SEARCH_SPAN.
    deployer — по движению токена: кто получил токены с кривой в транзакции запуска
    (дев-бай). Нет дев-бая — creator из topic[3] события фабрики (это msg.sender
    фабрики: при запуске через сторонний контракт там стоит контракт)."""
    token = token.lower()
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
    return {"block": int(launch["blockNumber"], 16), "curve": curve,
            "deployer": deployer, "tx": launch["transactionHash"]}


def token_facts(token, launch):
    """Факты о токене для движка (общий контракт сетей): {"supply", "transfers", "excluded",
    "market", "base"}. base = None: балансы и оборот движок считает из переводов
    (detect.supply_base) — здесь есть полная история переводов с запуска."""
    transfers = get_token_transfers(token, launch["block"])
    supply = token_supply(token)
    return {"supply": supply, "transfers": transfers, "excluded": excluded_addresses(launch["curve"]),
            "market": market_addresses(launch["curve"]), "base": None}


def get_token_transfers(token, from_block):
    """Все переводы токена от from_block до текущего блока, в порядке чейна."""
    out = []
    for lg in get_logs(from_block, block_number(), address=token.lower(), topics=[TRANSFER_TOPIC]):
        p = parse_transfer(lg)
        if not p or len(lg["data"]) <= 2:
            continue
        out.append({"frm": p["frm"], "to": p["to"], "amount": int(lg["data"], 16),
                    "tx": p["tx"], "block": p["block"], "log_index": int(lg["logIndex"], 16)})
    out.sort(key=lambda t: (t["block"], t["log_index"]))
    return out


def block_timestamps(blocks, chunk=100):
    """{block: unix_time} батчем eth_getBlockByNumber(..., false)."""
    blocks = sorted(set(blocks))
    out = {}
    for i in range(0, len(blocks), chunk):
        part = blocks[i:i + chunk]
        for b, blk in zip(part, rpc_batch([("eth_getBlockByNumber", [hex(b), False]) for b in part])):
            if blk:
                out[b] = int(blk["timestamp"], 16)
    return out


def token_supply(token):
    return int(rpc("eth_call", [{"to": token.lower(), "data": "0x18160ddd"}, "latest"]), 16)


BATCH_MAX = 10           # eth_getLogs в одном HTTP-батче при скане по окну
_ADDRLESS_SPAN = [None]  # выученный потолок окна getLogs без address (публичный RPC: 30 000)

def _allowed_span(msg):
    m = re.search(r"only (\d+) are allowed", msg)
    return int(m.group(1)) if m else None

def _scan_back(topics_list, lo, hi, stop):
    """eth_getLogs без address по окну [lo, hi] от новых блоков к старым, кусками
    по выученному потолку окна, несколько кусков на один HTTP-батч. После каждого
    куска вызывает stop(logs); True — сразу выходим (ранний выход экономит запросы).
    Элемент с ошибкой или упёршийся в LOG_CAP добирается через get_logs."""
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
            continue                    # перечитываем тот же отрезок кусками допустимого размера
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
    """Самый ранний блок < before_block, где кошелёк отправлял или получал
    любой токен (Transfer с кошельком в topic1 или topic2). Нет такого — None."""
    wt = topic_for(wallet.lower())
    first = first_log_block(0, before_block - 1, None, [TRANSFER_TOPIC, wt])
    # получателя ищем только раньше уже найденного блока отправки
    hi = before_block - 1 if first is None else first - 1
    recv = first_log_block(0, hi, None, [TRANSFER_TOPIC, None, wt])
    return recv if recv is not None else first


def wallet_distinct_tokens(wallet, before_block, skip_token, window=FRESH_WINDOW, cap=FRESH_CAP):
    """Свежесть В МОМЕНТ входа: сколько разных токен-контрактов кошелёк торговал
    в блоках [before_block - window, before_block), не считая skip_token.
    window=None — вся история: [0, before_block).
    before_block — блок первого получения сканируемого токена (покупка или перевод),
    skip_token — сканируемый токен. Возвращает min(n, cap): cap значит "cap или больше".
    Торговля = перевод токена кошельку/от кошелька, где контрагент — рынок:
    роутеры, V4 pool manager или кривая Pons (кривые узнаём по CurveBuy/CurveSell
    этого кошелька). Два фильтра (кошелёк в topic1 и в topic2) с OR по topic0
    покрывают и переводы, и события кривых. Окно читаем с конца и останавливаемся,
    как только набрали cap. Кривая skip_token не считается: её события лежат
    в тех же транзакциях, что и переводы skip_token."""
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


_CODE = {}  # addr -> bool, кэш is_contract в памяти процесса

def is_contract(addresses, chunk=100):
    """{addr: есть ли код} батчем eth_getCode, с кэшем в памяти."""
    addrs = {a.lower() for a in addresses}
    need = sorted(addrs - _CODE.keys())
    for i in range(0, len(need), chunk):
        part = need[i:i + chunk]
        for a, code in zip(part, rpc_batch([("eth_getCode", [a, "latest"]) for a in part])):
            if code is not None:
                _CODE[a] = code not in ("0x", "0x0", "")
    return {a: _CODE.get(a, False) for a in addrs}


LAUNCH_PHASES = {0: "curve", 1: "swept", 2: "pool", 3: "rescued"}  # поле phase фабрики


def launched_token(token):
    """factory.getLaunchedToken(token) -> {"pair", "phase"} или None (токен не из этой фабрики).
    pair: 0x0 = нативный ETH, иначе ERC-20 (токенизированная акция и т.п.).
    phase: "curve" — на кривой, "pool" — мигрировал в пул V4 (+ "swept", "rescued").
    Не кэшируется: phase меняется при миграции."""
    r = rpc("eth_call", [{"to": FACTORY, "data": "0x3cf28b5a" + "0" * 24 + token.lower()[2:]}, "latest"])
    if not (len(r) >= 2 + 15 * 64 and u256(r, 14) == 1):
        return None
    return {"pair": addr_from_topic(r[2 + 4 * 64 : 2 + 5 * 64]),
            "phase": LAUNCH_PHASES.get(u256(r, 10), str(u256(r, 10)))}


_PAIR = {}  # token -> pairToken | None, кэш launched_pair (пара не меняется)

def launched_pair(token):
    """pairToken запуска (см. launched_token) или None — токен не из этой фабрики."""
    token = token.lower()
    if token not in _PAIR:
        info = launched_token(token)
        _PAIR[token] = info["pair"] if info else None
    return _PAIR[token]


def classify_entries(token, transfers, wallets, chunk=50):
    """Первое получение токена каждым кошельком из wallets, по транзакции:
    {wallet: {"kind": "buy"|"transfer", "tx", "block", "via", "eth_in"}}.
    buy — в чеке транзакции есть CurveBuy кривой этого токена (кривая = контракт,
    который в этой же транзакции отдал токен) или V4 Swap, чья сторона токена
    совпадает с выходом токена из pool manager. Так покупка через сторонний
    бот-роутер — тоже buy, хотя токен пришёл от промежуточного контракта.
    via — от кого кошелёк получил токен.
    eth_in — котировка, ушедшая на покупку (CurveBuy quoteIn или котировочная
    сторона свопа), сверенная с полученным кошельком количеством (допуск 6%).
    None, если пара не ETH или сверить не удалось (одна покупка разошлась
    нескольким кошелькам). У transfer всегда None."""
    token = token.lower()
    want = {w.lower() for w in wallets}
    first = {}
    for t in transfers:
        if t["to"] in want and t["to"] not in first:
            first[t["to"]] = t
    txs = sorted({t["tx"] for t in first.values()})
    receipts = {}
    for i in range(0, len(txs), chunk):
        part = txs[i:i + chunk]
        for h, rc in zip(part, rpc_batch([("eth_getTransactionReceipt", [h]) for h in part])):
            receipts[h] = (rc or {}).get("logs", [])
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


def eth_transfers(to_block, max_count=1000, **flt):
    """ETH-переводы (alchemy_getAssetTransfers, category external) по фильтру
    toAddress/fromAddress в блоках [0, to_block]. internal на этой сети не
    поддерживается: ETH из контрактов (выводы с бирж через контракт, мосты) не виден.
    Возвращает (список {"from", "to", "block", "amount" (wei), "tx"}, упёрлись_в_max_count)."""
    out, key = [], None
    while True:
        p = {"category": ["external"], "fromBlock": "0x0", "toBlock": hex(to_block),
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
    """Входящие external ETH-переводы кошелька в блоках < before_block:
    [{"from", "block", "amount" (wei)}]. Не больше max_count."""
    rows, _ = eth_transfers(before_block - 1, max_count, toAddress=wallet.lower())
    return [{"from": r["from"], "block": r["block"], "amount": r["amount"]} for r in rows]


def outgoing_count(addr, cap=100):
    """Сколько исходящих external ETH-переводов сделал адрес, min(n, cap).
    Ранний выход на cap: важно только "меньше cap или нет" (хаб — README)."""
    rows, _ = eth_transfers(block_number(), cap, fromAddress=addr.lower())
    return min(len(rows), cap)


def _abi_string(h):
    """Декод string из ответа eth_call (ABI) или bytes32; пусто — ''."""
    if not h or h == "0x":
        return ""
    b = bytes.fromhex(h[2:])
    if len(b) >= 96:
        n = int.from_bytes(b[32:64], "big")
        return b[64:64 + n].decode(errors="replace")
    return b.rstrip(b"\0").decode(errors="replace")


def token_meta(token):
    """{"name", "symbol"} из контракта токена, одним батчем."""
    t = token.lower()
    name, sym = rpc_batch([("eth_call", [{"to": t, "data": "0x06fdde03"}, "latest"]),
                           ("eth_call", [{"to": t, "data": "0x95d89b41"}, "latest"])])
    return {"name": _abi_string(name), "symbol": _abi_string(sym)}
