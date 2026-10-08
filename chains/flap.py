"""Адаптер лаунчпада Flap на Robinhood Chain (только при FLAP_ENABLED=true; по умолчанию выключен).

Сеть та же, что у Pons (chains/robinhood.py): RPC, лимитер, счётчик запросов, кэши чеков и времени блоков —
общие, все запросы идут через функции robinhood в момент вызова. Отличается только то, что знает лаунчпад:
определение токена, запуск, рынок и инфраструктура, резерв, покупки, налог.

Токен Flap: адрес оканчивается на 8888 (без налога, TOKEN_V2_PERMIT) или 7777 (налоговый, TOKEN_TAXED_V3) —
проверка без RPC; затем один HTTP-батч: Portal.getTokenV8Safe(token) (у чужого токена — revert) вместе с
taxProcessor(), mainPool() токена и TaxTokenHelper.getTaxTokenInfoV2 (получатель налога).
Кривая: токены лежат на Portal, цена — виртуальное x*y=k: (r + reserve)(h + 1e9 − circulating) = k,
поэтому резерв для dump impact — h + 1e9 − circulating (не баланс Portal). После выпуска — пара Uniswap V2
(mainPool), резерв — getReserves. Налог на кривой берётся в ETH (в кривую уходит всё q), после выпуска —
токенами: в пару сразу уходит q × (1 − sellTax), остальное копится на контракте токена.
История большого токена (больше HISTORY_MAX_LOGS переводов) читается окнами в бюджете скана — см. history().
Замеры и события — scripts/probe_flap.py (локально), раздел «Flap» в DEV_NOTES."""
import os, re, time, urllib.error
from concurrent.futures import ThreadPoolExecutor

from chains import priority
from chains import robinhood as ch

CHAIN = ch.CHAIN
REQUESTS = ch.REQUESTS            # один счётчик HTTP с адаптером Robinhood
PACK_WINDOW = ch.PACK_WINDOW
BUNDLE_WINDOW = ch.BUNDLE_WINDOW
RUG_SNIPERS = ch.RUG_SNIPERS
RPS = ch.RPS                      # лимитер общий с Robinhood (фон — тот же BACKGROUND_RPS, chains.priority)
HISTORY_PARALLEL = ch.HISTORY_PARALLEL   # движок читает кошельки окном этой ширины, крупнейшие первыми (как у Pons)

SUFFIXES = ("8888", "7777")       # vanity-суффиксы реализаций: 8888 — без налога, 7777 — налоговый V3
PORTAL = "0x26605f322f7ff986f381bb9a6e3f5dab0beaeb09"
VAULT_PORTAL = "0xe9f7ab7de8fb8756acbb6a1cd13316a43308197b"
TAX_HELPER = "0xb10bd2672ae63735d677164a54b573a016f0203c"
V2_FACTORY = "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f"      # Uniswap V2 Factory: пары после выпуска
SHADOW_FACTORY = "0x0d1ebb179cdbca88d74c923c4255cb2b17474afd"  # «теневые» пары налоговых токенов (зеркало кривой)
LOCKERS = {  # сторонние локеры/вестинг (своего локера Flap на Robinhood нет, docs.flap.sh); залоченное — в "locked"
    "0x548129a58bc230549df7f9e33f27e77f6779ff0f",  # Sablier Lockup (NFT SAB-LOCKUP): TasQ — вся покупка дева
    "0x37c434ec1c54e360900e3a022247d5e20137c1de",  # Sablier: периферия создания стримов (токены идут через неё)
}
INFRA = LOCKERS | {  # не холдеры ни для какого токена Flap (сверх ch.INFRA)
    PORTAL, VAULT_PORTAL, TAX_HELPER, V2_FACTORY, SHADOW_FACTORY,
    "0xd3421b1b616a72bb88993a0cf75709bb8d532cc1",  # Trigger Service
    "0xa4a727e0918cf9b39639fc4cb7d742d39c5352a4",  # получатель комиссии протокола (1%)
}
PORTAL_HELPERS = {  # Portal торгует после выпуска через пару: Portal -> helper -> executor -> пара
    "0xe5f72d6f9ddab579317a4febd2ffb8ec3d73497b",
    "0xcaf681a66d020601342297493863e78c959e5cb2",
}
ROUTERS = ch.ROUTERS | {  # роутеры и исполнители агрегаторов, замеченные на токенах Flap (2026-10-08)
    "0x8f10b468b06c6fd214b65f87778827f7d113f996",
    "0xe492912f37c2a4eca45d42dc67548f4c6cd7ce2b",
    "0xb300000b72deaeb607a12d5f54773d1c19c7028d",
    "0xc0fab674ff7ddf8b891495ba9975b0fe1dcac735",  # исполнитель: trader в TokenBought/TokenSold
    "0x6e2a35a7ad683cf634d91492d73bb7ff774c6919",
}
ZERO = "0x" + "0" * 40
SUPPLY = 10 ** 27                  # maxSupply любого токена Flap: 1e9 токенов, 18 знаков (константа кривой)
STATUS_CURVE, STATUS_DEX = 1, 4    # TokenStatus: Tradable (кривая), DEX (выпущен)

# селекторы и топики (keccak256; стандартной библиотеки keccak нет — посчитаны в scripts/probe_flap.py)
SEL_STATE = "0x62fafcca"           # getTokenV8Safe(address)
SEL_TAX_PROCESSOR = "0xf3635019"   # taxProcessor()
SEL_MAIN_POOL = "0xa5a302d3"       # mainPool()
SEL_TAX_INFO = "0x6c06b371"        # getTaxTokenInfoV2(address)
SEL_TOKEN0 = "0x0dfe1681"          # token0()
SEL_RESERVES = "0x0902f1ac"        # getReserves()
TOKEN_CREATED = "0x504e7f360b2e5fe33cbaaae4c593bc55305328341bf79009e43e0e3b7f699603"  # всё в data, без индексов
TOKEN_BOUGHT = "0xa800a2038683844fac66747f771bfdfae862eb28b16bcfa387afa9fbacce8ff7"   # (ts, token, buyer, amount, eth, fee, postPrice)
V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"       # (amount0In, amount1In, amount0Out, amount1Out)
PAIR_CREATED = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddefde31afa28d0e9"  # data: [pair, n]
STATE_FIELDS = ("status", "reserve", "circulating", "price", "version", "r", "h", "k", "dex_thresh", "quote",
                "native_swap", "extension", "buy_tax", "sell_tax", "pool", "progress", "lp_fee", "dex_id")
ENTRY_TOL = 0.06                   # допуск сверки количества покупки (как у Pons), плюс налог на покупку

# история больших токенов (token_facts): не читать целиком, если больше HISTORY_MAX_LOGS логов
HISTORY_MAX_LOGS = 20_000          # до стольких переводов история читается целиком (~2 страницы RPC по 10K, ~2–4 с)
EARLY_BLOCKS = 20_000              # (а) окно запуска: столько блоков после запуска целиком (~34 мин при ~10 блоков/с):
                                   # снайперы (30 с), бандл (блок запуска), ранние покупатели (EARLY_WINDOW 2000)
RECENT_PAGES = 2                   # свежие страницы (до ~20K переводов) — кандидаты в холдеры, кто торговал недавно
SAMPLE_PAGES = 8                   # выборок по странице, равномерно по жизни токена (кандидаты: киты входят когда угодно)
SAMPLE_LOGS = 10_000               # логов в выборке: потолок — число запросов (лимитер RPS), а не логов: страница целиком
HISTORY_FORCE_WINDOWED = False     # только для сверки и замеров: окнами даже маленькую историю
PROJECT_FACTOR = 2                 # после первой страницы: оценка всей истории по плотности > порога × 2 — сразу окнами
HISTORY_RESERVE = 12.0             # секунд бюджета скана оставляем после истории: чеки входов, история кошельков, detect
HOLDERS_N = 20                     # = detect.TOP_N: по скольким холдерам читаем их переводы целиком
DUST_SHARE = 0.001                 # = detect.DUST_SHARE: меньше этой доли оборота движок холдером в топе не считает
PAGE_LOGS = 10_000                 # потолок логов одного ответа eth_getLogs у RPC
MULTICALL = "0xca11bde05977b3631167028862be2a173976ca11"   # Multicall3: balanceOf пачкой в одном eth_call
SEL_AGGREGATE = "0x252dba42"       # aggregate((address,bytes)[]) -> (uint256 blockNumber, bytes[] returnData)
SEL_BALANCE = "0x70a08231"         # balanceOf(address)
MULTICALL_CHUNK = 500             # balanceOf в одном eth_call (~1.5 млн газа)
MULTICALL_PER_HTTP = 2            # eth_call в одном HTTP: ~170 КБ тела (RPC отвечает 413 на большие запросы)
_SUGGEST = re.compile(r"\[(0x[0-9a-fA-F]+),\s*(0x[0-9a-fA-F]+)\]")   # «this block range should work: [a, b]»

_STATE = {}   # token -> последнее состояние (detect); пул и налог для classify_entries того же скана


def enabled():
    return os.environ.get("FLAP_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def candidate(token):
    """Похож на Flap по адресу (без RPC): флаг включён и суффикс 8888 / 7777."""
    return enabled() and isinstance(token, str) and token.lower().endswith(SUFFIXES)


def _words(h):
    h = (h or "0x")[2:]
    return [int(h[i:i + 64], 16) for i in range(0, len(h) - len(h) % 64, 64)]


def _addr(w):
    return "0x" + f"{w:064x}"[-40:]


def _call(to, data):
    return ("eth_call", [{"to": to, "data": data}, "latest"])


def _arg(addr):
    return "0" * 24 + addr.lower()[2:]


def detect(token):
    """Токен Flap -> состояние (dict) или None. Pons и всё без суффикса — None без единого запроса.
    Один HTTP-батч: getTokenV8Safe (revert или status 0 — не Flap), taxProcessor(), mainPool(),
    getTaxTokenInfoV2 (получатель налога: vault.addr и признак Flap Vault)."""
    if not candidate(token):
        return None
    token = token.lower()
    st_raw, tp_raw, pool_raw, tax_raw = ch.rpc_batch([
        _call(PORTAL, SEL_STATE + _arg(token)), _call(token, SEL_TAX_PROCESSOR), _call(token, SEL_MAIN_POOL),
        _call(TAX_HELPER, SEL_TAX_INFO + _arg(token))])
    w = _words(st_raw)
    if len(w) < len(STATE_FIELDS) or w[0] == 0:
        return None
    st = dict(zip(STATE_FIELDS, w))
    st["quote"], st["pool"] = _addr(st["quote"]), _addr(st["pool"])
    tp, mp, tax = _words(tp_raw), _words(pool_raw), _words(tax_raw)
    st["tax_processor"] = _addr(tp[0]) if tp and tp[0] else None
    st["main_pool"] = _addr(mp[0]) if mp and mp[0] else (st["pool"] if st["pool"] != ZERO else None)
    st["tax_recipient"] = _addr(tax[14]) if len(tax) >= 19 and tax[14] else None
    st["tax_recipient_vault"] = len(tax) >= 19 and tax[18] == 1   # Flap Vault (контракт), а не кошелёк
    _STATE[token] = st
    return st


def get_launch(token, st=None):
    """{"block", "tx", "curve", "deployer", "creator", "dev_buy", "dev_buy_wallet", "shadow_pair"} или None.
    Запуск — выпуск 1B токенов с 0x0 на Portal: один getLogs по адресу токена, затем чек транзакции.
    creator — из TokenCreated (Portal); deployer = creator, а если события нет или creator — инфраструктура
    (VaultPortal, роутер) — получатель токенов от Portal в этой транзакции (покупка дева при запуске).
    dev_buy — сколько токенов получил от Portal deployer в транзакции запуска."""
    token = token.lower()
    flt = {"fromBlock": "0x0", "toBlock": "latest", "address": token,
           "topics": [ch.TRANSFER_TOPIC, ch.topic_for(ZERO)]}
    try:
        logs = ch.rpc("eth_getLogs", [flt])
    except RuntimeError:   # окно слишком большое для RPC — тем же путём, что у Pons
        b = ch.first_log_block(0, ch.block_number(), address=token, topics=flt["topics"])
        logs = [] if b is None else ch.get_logs(b, b, address=token, topics=flt["topics"])
    if not logs:
        return None
    mint = min(logs, key=lambda l: (int(l["blockNumber"], 16), int(l.get("logIndex", "0x0"), 16)))
    tx = mint["transactionHash"]
    rc = ch.receipt_logs([tx]).get(tx, [])
    creator, shadow, bought = None, None, []
    for l in rc:
        tp, addr = l.get("topics") or [], l["address"].lower()
        t0 = tp[0].lower() if tp else ""
        if addr == PORTAL and t0 == TOKEN_CREATED:
            w = _words(l["data"])
            if len(w) >= 4 and _addr(w[3]) == token:
                creator = _addr(w[1])
        elif addr == SHADOW_FACTORY and t0 == PAIR_CREATED:
            shadow = _addr(_words(l["data"])[0])
        elif (p := ch.parse_transfer(l)) and p["token"] == token and p["frm"] == PORTAL:
            bought.append((p["to"], int(l["data"], 16)))
    infra = INFRA | ROUTERS | PORTAL_HELPERS | ch.INFRA
    buyers = [(w, a) for w, a in bought if w not in infra]
    deployer = creator if creator and creator not in infra else (buyers[0][0] if buyers else creator)
    dev_buy = sum(a for w, a in buyers if w == deployer)
    return {"block": int(mint["blockNumber"], 16), "tx": tx, "curve": PORTAL, "deployer": deployer,
            "creator": creator, "dev_buy": dev_buy, "dev_buy_wallet": deployer if dev_buy else None,
            "shadow_pair": shadow}


def _pair_reserve(token, pool):
    """Токенная сторона резерва пары V2: (резерв, токен — token0?) одним HTTP-батчем token0() + getReserves()."""
    t0, res = ch.rpc_batch([_call(pool, SEL_TOKEN0), _call(pool, SEL_RESERVES)])
    w0, r = _words(t0), _words(res)
    if not w0 or len(r) < 2:
        return 0, None
    is0 = _addr(w0[0]) == token
    return (r[0] if is0 else r[1]), is0


def _transfer(lg):
    """Лог Transfer -> перевод (поля как у ch.get_token_transfers) или None."""
    p = ch.parse_transfer(lg)
    if not p or len(lg["data"]) <= 2:
        return None
    t = {"frm": p["frm"], "to": p["to"], "amount": int(lg["data"], 16), "tx": p["tx"], "block": p["block"],
         "log_index": int(lg["logIndex"], 16)}
    if lg.get("blockTimestamp"):
        t["ts"] = int(lg["blockTimestamp"], 16)
    return t


def _page(token, lo, hi, span=None, topics=None):
    """Одна страница переводов токена от блока lo вперёд (не больше PAGE_LOGS логов): (переводы, последний блок).
    span — сколько блоков просить (None — до hi); topics — фильтр после топика Transfer ([from], [to]: OR-списки).
    Ответ «Log response size exceeded» с подсказкой [a, b] — повтор ровно по подсказке; без подсказки — пополам."""
    end = hi if span is None else min(hi, lo + max(0, span))
    flt = {"fromBlock": hex(lo), "toBlock": hex(end), "address": token, "topics": [ch.TRANSFER_TOPIC] + (topics or [])}
    try:
        logs = ch.rpc("eth_getLogs", [flt])
    except RuntimeError as e:
        m = _SUGGEST.search(str(e))
        if m and int(m.group(1), 16) == lo and int(m.group(2), 16) < end:
            end = int(m.group(2), 16)
        elif end > lo and ch._is_window_error(str(e).lower()):
            end = lo + (end - lo) // 2
        else:
            raise
        logs = ch.rpc("eth_getLogs", [dict(flt, toBlock=hex(end))])
    if len(logs) >= PAGE_LOGS and end > lo:   # ответ мог быть обрезан — половину окна
        return _page(token, lo, hi, (end - lo) // 2, topics)
    return [t for t in map(_transfer, logs) if t], end


def _forward(token, lo, hi, max_logs, until, topics=None):
    """Переводы от lo вперёд страницами, пока не дошли до hi, не набрали больше max_logs, оценка всей истории
    по плотности прочитанного не превысила max_logs × PROJECT_FACTOR или не вышло время.
    -> (переводы, последний прочитанный блок, дочитано ли до hi)."""
    out, span, lo0 = [], None, lo
    while lo <= hi:
        page, end = _page(token, lo, hi, span, topics)
        out += page
        # следующее окно — по плотности этой страницы: полная — чуть меньше (без лишнего отказа RPC), редкая — шире
        span = None if end >= hi else (int((end - lo) * 0.8) if len(page) > PAGE_LOGS * 0.7 else (end - lo + 1) * 2)
        lo = end + 1
        projected = len(out) * (hi - lo0 + 1) / max(1, end - lo0 + 1)
        if len(out) > max_logs or projected > max_logs * PROJECT_FACTOR or time.time() > until:
            return out, end, lo > hi
    return out, hi, True


def _recent(token, lo, hi, pages, until):
    """Свежие переводы: до pages страниц назад от hi (не раньше lo). Окно подбирается по плотности логов:
    подсказка RPC идёт вперёд от начала окна, поэтому новое окно — от конца назад той же длины."""
    out, span = [], 200_000
    while pages > 0 and hi >= lo and time.time() < until:
        start = max(lo, hi - span)
        flt = {"fromBlock": hex(start), "toBlock": hex(hi), "address": token, "topics": [ch.TRANSFER_TOPIC]}
        try:
            logs = ch.rpc("eth_getLogs", [flt])
        except RuntimeError as e:
            m = _SUGGEST.search(str(e))
            if not (m or ch._is_window_error(str(e).lower())) or hi - start < 2:
                raise
            got = int(m.group(2), 16) - start if m else (hi - start) // 2
            span = max(1, int(got * 0.8))
            continue
        if len(logs) >= PAGE_LOGS:
            span = max(1, span // 2)
            continue
        out += [t for t in map(_transfer, logs) if t]
        hi, pages = start - 1, pages - 1
    return out


def _sample(token, lo, hi, until):
    """Выборка: ~SAMPLE_LOGS переводов от блока lo (не дальше hi). Окно — по подсказке RPC: она даёт окно на
    PAGE_LOGS логов, берём его долю SAMPLE_LOGS / PAGE_LOGS (второй запрос). Время вышло — пусто."""
    if time.time() > until or lo > hi:
        return []
    flt = {"fromBlock": hex(lo), "toBlock": hex(hi), "address": token, "topics": [ch.TRANSFER_TOPIC]}
    try:
        logs = ch.rpc("eth_getLogs", [flt])
    except RuntimeError as e:
        m = _SUGGEST.search(str(e))
        if not m or int(m.group(1), 16) != lo:
            return _page(token, lo, hi)[0]
        end = lo + max(0, (int(m.group(2), 16) - lo) * min(SAMPLE_LOGS, PAGE_LOGS) // PAGE_LOGS)
        try:
            logs = ch.rpc("eth_getLogs", [dict(flt, toBlock=hex(end))])
        except RuntimeError:   # всё равно велико (логи неравномерны) — обычной страницей
            return _page(token, lo, end)[0]
    return [t for t in map(_transfer, logs) if t]


def _holder_logs(token, launch_block, head, wallets, until):
    """Все переводы кошельков (входящие и исходящие, параллельно) с запуска, до срока. -> (переводы, дочитано)."""
    tops = [ch.topic_for(a) for a in sorted(wallets)]
    (inc, _, d1), (out, _, d2) = _parallel([
        lambda: _forward(token, launch_block, head, float("inf"), until, [None, tops]),
        lambda: _forward(token, launch_block, head, float("inf"), until, [tops])])
    return inc + out, d1 and d2


def _parallel(jobs):
    """Запустить функции в потоках (сеть ждёт ответа RPC; лимитер RPS общий) -> результаты по порядку."""
    # фоновая перепроверка alerts — и её потоки фоновые (chains.priority узнаёт их по имени потока)
    with ThreadPoolExecutor(max_workers=max(1, len(jobs)),
                            thread_name_prefix=f"{priority.BG}-flap" if priority.is_background() else "") as ex:
        return [f.result() for f in [ex.submit(j) for j in jobs]]


def _encode_aggregate(calls):
    """calldata Multicall3.aggregate((address target, bytes callData)[])."""
    n = len(calls)
    body = []
    for target, data in calls:
        b = bytes.fromhex(data[2:])
        pad = b + b"\0" * (-len(b) % 32)
        el = _arg(target) + f"{64:064x}" + f"{len(b):064x}" + pad.hex()
        body.append(el)
    offs, pos = [], 32 * n
    for el in body:
        offs.append(pos)
        pos += len(el) // 2
    return (SEL_AGGREGATE + f"{32:064x}" + f"{n:064x}" + "".join(f"{o:064x}" for o in offs) + "".join(body))


def _decode_aggregate(res):
    """Ответ aggregate: (blockNumber, bytes[] returnData) -> [returnData как hex-строки]."""
    h = res[2:]
    word = lambda i: int(h[64 * i:64 * i + 64], 16)
    arr = word(1) // 32
    n = word(arr)
    out = []
    for k in range(n):
        el = arr + 1 + word(arr + 1 + k) // 32
        ln = word(el)
        out.append("0x" + h[64 * (el + 1):64 * (el + 1) + 2 * ln])
    return out


def _aggregate(calls_chunks):
    """Один HTTP-батч eth_call Multicall3.aggregate по нескольким пачкам. 413 (запрос велик) — по половине."""
    try:
        res = ch.rpc_batch([_call(MULTICALL, _encode_aggregate(c)) for c in calls_chunks])
    except urllib.error.HTTPError as e:
        if e.code != 413 or len(calls_chunks) == 1 and len(calls_chunks[0]) == 1:
            raise
        if len(calls_chunks) == 1:
            c = calls_chunks[0]
            calls_chunks = [c[:len(c) // 2], c[len(c) // 2:]]
        half = len(calls_chunks) // 2
        return _aggregate(calls_chunks[:half]) + _aggregate(calls_chunks[half:])
    out = []
    for r in res:
        if not r:
            raise RuntimeError("multicall balanceOf failed")
        out += _decode_aggregate(r)
    return out


def balances(token, addrs):
    """{адрес: balanceOf} через Multicall3: по MULTICALL_CHUNK вызовов в одном eth_call, по MULTICALL_PER_HTTP
    eth_call в одном HTTP (у RPC потолок размера запроса), HTTP-запросы параллельно."""
    addrs = sorted(set(addrs))
    calls = [(token, SEL_BALANCE + _arg(a)) for a in addrs]
    chunks = [calls[i:i + MULTICALL_CHUNK] for i in range(0, len(calls), MULTICALL_CHUNK)]
    groups = [chunks[i:i + MULTICALL_PER_HTTP] for i in range(0, len(chunks), MULTICALL_PER_HTTP)]
    vals = [v for part in _parallel([lambda g=g: _aggregate(g) for g in groups]) for v in part] if groups else []
    return {a: int(v, 16) if len(v) > 2 else 0 for a, v in zip(addrs, vals)}


def history(token, launch_block, supply, excluded, deadline=None):
    """Переводы токена для скана: (transfers, base или None, сведения об истории, балансы инфраструктуры или None).
    История до HISTORY_MAX_LOGS переводов — целиком (base None: балансы движок считает из переводов, как у Pons).
    Больше — окнами, не целиком:
      (а) первые EARLY_BLOCKS блоков после запуска целиком (снайперы, бандл, ранние покупатели);
      кандидаты в холдеры — все адреса из прочитанного: окно запуска, SAMPLE_PAGES выборок по ~SAMPLE_LOGS переводов,
      равномерно по жизни токена (киты входят когда угодно), и RECENT_PAGES свежих страниц — параллельно;
      балансы кандидатов и инфраструктуры — balanceOf через Multicall3 (оборот = сапплай − инфраструктура);
      (б) по топ-HOLDERS_N холдерам — их переводы с запуска (getLogs с фильтром from / to = кошельки, OR; входящие
      и исходящие параллельно, страницами от запуска): вход (покупка / перевод), продажи, прямые переводы между
      холдерами и общие раздатчики. Не дочитали к сроку — holder_logs_complete False. Затем расширение: балансы
      контрагентов топа (связанные кошельки); вошедшие в топ — их переводы тоже.
    Топ точный (top_exact), если непрочитанный остаток оборота меньше баланса 20-го холдера или порога пыли
    (что больше): ни один невиданный кошелёк не может войти в топ движка; иначе движок помечает скан limited.
    Время: всё до deadline − HISTORY_RESERVE (deadline — от начала скана).
    Кэш истории переводов между сканами (ch.load_transfers, TRANSFER_CACHE_ENABLED) здесь не используется: окнами
    читается не вся история, а выборка (его «история + хвост» и сверка с totalSupply к ней неприменимы), а целиком —
    до HISTORY_MAX_LOGS переводов, 1–2 страницы getLogs: хвост сэкономил бы 0–1 запрос. remember_scan у Flap нет."""
    until = (deadline - HISTORY_RESERVE) if deadline is not None else float("inf")
    t_start = time.time()
    head = ch.block_number()
    trs, last, done = _forward(token, launch_block, head, HISTORY_MAX_LOGS, until)
    if done and not HISTORY_FORCE_WINDOWED:
        return trs, None, {"mode": "full", "logs": len(trs)}, None
    early_end = launch_block + EARLY_BLOCKS
    early_done = last >= early_end
    if not early_done:   # (а) окно запуска дочитываем целиком (даже если оно больше порога), но в бюджете
        more, last, early_done = _forward(token, last + 1, early_end, float("inf"), until)
        trs += more
    early = [t for t in trs if t["block"] <= early_end]
    t_win = time.time()
    lo = max(last, early_end) + 1
    step = max(1, (head - lo) // (SAMPLE_PAGES + 1))
    starts = [lo + step * k for k in range(1, SAMPLE_PAGES + 1)]
    jobs = [lambda s0=s0: _sample(token, s0, min(head, s0 + step - 1), until) for s0 in starts]
    jobs.append(lambda: _recent(token, lo, head, RECENT_PAGES, until))
    parts = _parallel(jobs)
    sampled, recent = [t for p in parts[:-1] for t in p], parts[-1]
    seen = trs + sampled + recent
    t_bal = time.time()
    cand = {a for t in seen for a in (t["frm"], t["to"])} - set(excluded)
    bal = balances(token, cand | (set(excluded) - {ZERO}))
    held = {a: v for a, v in bal.items() if a in cand and v > 0}
    circulating = supply - sum(v for a, v in bal.items() if a in excluded)
    top = sorted(held, key=lambda a: -held[a])[:HOLDERS_N]
    t_hold = time.time()
    mine, mine_done, expanded = [], True, 0
    if top:
        mine, mine_done = _holder_logs(token, launch_block, head, top, until)
        # расширение: контрагенты топа (кому отдавали, от кого получали) — связанные кошельки одного оператора
        extra = {a for t in mine for a in (t["frm"], t["to"])} - cand - set(excluded)
        if extra and time.time() < until:
            more = {a: v for a, v in balances(token, extra).items() if v > 0}
            expanded = len(more)
            held.update(more)
            cand |= extra
            top2 = sorted(held, key=lambda a: -held[a])[:HOLDERS_N]
            new = [a for a in top2 if a not in top]
            if new:
                add, done2 = _holder_logs(token, launch_block, head, new, until)
                mine, mine_done = mine + add, mine_done and done2
            top = top2
    # движку — окно запуска и все переводы топа (выборки и свежие страницы — только для поиска кандидатов)
    uniq = {(t["tx"], t["log_index"]): t for t in early + mine}
    transfers = sorted(uniq.values(), key=lambda t: (t["block"], t["log_index"]))
    covered = sum(held.values())
    unseen = max(0, circulating - covered)
    base = {"supply": supply, "circulating": circulating, "holders_total": len(held), "balances": held}
    info = {"mode": "windowed", "logs_read": len(seen), "early_blocks": EARLY_BLOCKS, "early_until_block": early_end,
            "early_complete": early_done, "sample_pages": SAMPLE_PAGES, "sampled_logs": len(sampled),
            "recent_logs": len(recent), "holder_logs": len(mine), "holder_logs_complete": mine_done,
            "expanded": expanded, "seconds": {"full_probe": round(t_win - t_start, 1), "samples": round(t_bal - t_win, 1),
                                              "balances": round(t_hold - t_bal, 1), "holders": round(time.time() - t_hold, 1)},
            "candidates": len(cand), "coverage": covered / circulating if circulating else 0.0,
            "top_exact": unseen < max(held[top[-1]] if len(top) == HOLDERS_N else 0, DUST_SHARE * circulating),
            "holders_total_partial": True}
    return transfers, base, info, {a: v for a, v in bal.items() if a in excluded}


def token_facts(token, launch, deadline=None):
    """Факты для движка (общий контракт сетей) + "q_factor" и "flap" (данные лаунчпада для результата).
    Резерв: на кривой — виртуальный h + 1e9 − circulating из состояния Portal; после выпуска — getReserves пары.
    q_factor = 1 − sellTax после выпуска (налог токенами: в пару уходит q × (1 − sellTax)), на кривой 1.0
    (налог берётся в ETH, в кривую уходит всё q). Инфраструктура и рынок — см. INFRA / ROUTERS."""
    token = token.lower()
    st = _STATE.get(token) or detect(token)
    supply = ch.token_supply(token)
    dex = st["status"] == STATUS_DEX
    pool = st["main_pool"]
    own = {token} | {x for x in (st["tax_processor"], pool, launch.get("shadow_pair")) if x}
    if st["tax_recipient"] and st["tax_recipient_vault"]:
        own.add(st["tax_recipient"])          # Flap Vault — контракт получателя налога, не холдер
    market = {PORTAL} | PORTAL_HELPERS | ROUTERS | {ch.V4_POOL_MGR} | own - {launch.get("shadow_pair")}
    excluded = ch.INFRA | INFRA | market | own
    transfers, base, hist, infra_bal = history(token, launch["block"], supply, excluded, deadline)
    if dex:   # пара не прочиталась — резерв 0: reserve_ok False («liquidity not measured»), а не формула кривой
        reserve, is0 = _pair_reserve(token, pool) if pool else (0, None)
        st["token_is0"] = is0
        source = "pair_reserves"
    else:
        reserve = st["h"] + SUPPLY - st["circulating"]
        source = "curve_state"
    q_factor = 1.0 - st["sell_tax"] / 10_000 if dex else 1.0
    locked = {}   # сколько токенов сейчас в локерах (исключены из оборота, но это не «ничьи» токены)
    if infra_bal is not None:          # история окнами: балансы инфраструктуры уже прочитаны
        locked = {a: infra_bal.get(a, 0) for a in LOCKERS}
    else:
        for t in transfers:
            for a, sign in ((t["to"], 1), (t["frm"], -1)):
                if a in LOCKERS:
                    locked[a] = locked.get(a, 0) + sign * t["amount"]
    locked = {a: v for a, v in locked.items() if v > 0}
    rcp = st["tax_recipient"]
    is_dev = bool(rcp) and rcp in {launch.get("deployer"), launch.get("creator")}
    info = {"phase": "dex" if dex else "bonding_curve",
            "phase_text": "dex" if dex else f"bonding curve {st['progress'] / 1e16:.0f}%",
            "progress": st["progress"] / 1e18, "version": st["version"],
            "tax": {"buy": st["buy_tax"] / 10_000, "sell": st["sell_tax"] / 10_000},
            "tax_recipient": rcp if (st["buy_tax"] or st["sell_tax"]) else None,
            "tax_recipient_is_dev": is_dev and bool(st["buy_tax"] or st["sell_tax"]),
            "tax_processor": st["tax_processor"], "pool": pool, "shadow_pair": launch.get("shadow_pair"),
            "reserve_source": source, "q_factor": q_factor, "dev_buy": launch.get("dev_buy", 0),
            "locked": locked, "locked_share_supply": sum(locked.values()) / supply if supply else 0.0,
            "infra": sorted(own | {PORTAL}), "history": hist}
    return {"supply": supply, "transfers": transfers, "excluded": excluded, "market": market,
            "reserve": max(0, reserve), "reserve_ok": reserve > 0, "base": base, "q_factor": q_factor,
            "flap": info}


def classify_entries(token, transfers, wallets, chunk=50):
    """Первое получение токена каждым кошельком (контракт как у ch.classify_entries).
    buy — в чеке транзакции есть покупка этого токена: TokenBought на Portal (кривая; покупка через агрегатор —
    trader = исполнитель, кошелёк получает токен вторым переводом) или Swap пары V2 с выходом токена (после
    выпуска; налог на покупку — перевод пары на контракт токена). Иначе transfer.
    eth_in — ETH покупки, если количество сходится с полученным (допуск ENTRY_TOL + налог на покупку)."""
    token = token.lower()
    st = _STATE.get(token) or {}
    pool, buy_tax = st.get("main_pool"), st.get("buy_tax", 0) / 10_000
    want = {w.lower() for w in wallets}
    first = {}
    for t in transfers:
        if t["to"] in want and t["to"] not in first:
            first[t["to"]] = t
    receipts = ch.receipt_logs(sorted({t["tx"] for t in first.values()}), chunk)
    out = {}
    for w, t in first.items():
        buys = []   # (токенов из сделки, ETH)
        for l in receipts.get(t["tx"], []):
            tp, addr = l.get("topics") or [], l["address"].lower()
            t0 = tp[0].lower() if tp else ""
            if addr == PORTAL and t0 == TOKEN_BOUGHT:
                d = _words(l["data"])
                if len(d) >= 5 and _addr(d[1]) == token:
                    buys.append((d[3], d[4]))
            elif pool and addr == pool and t0 == V2_SWAP:
                a0in, a1in, a0out, a1out = (_words(l["data"]) + [0] * 4)[:4]
                is0 = st.get("token_is0")
                sides = [(a0out, a1in), (a1out, a0in)] if is0 is None else [(a0out, a1in) if is0 else (a1out, a0in)]
                buys += [(o, q) for o, q in sides if o > 0]
        eth_in = None
        if buys and t["amount"]:
            got, quote = min(buys, key=lambda b: abs(b[0] - t["amount"]))
            if abs(got - t["amount"]) <= (ENTRY_TOL + buy_tax) * max(got, t["amount"]):
                eth_in = quote
        out[w] = {"kind": "buy" if buys else "transfer", "tx": t["tx"], "block": t["block"], "via": t["frm"],
                  "eth_in": eth_in}
    return out


# общее с Robinhood: вызываются в момент вызова (тесты подменяют функции ch)
def block_timestamps(blocks, *a, **k):
    return ch.block_timestamps(blocks, *a, **k)


def wallet_distinct_tokens(*a, **k):
    return ch.wallet_distinct_tokens(*a, **k)


def is_contract(addresses, *a, **k):
    return ch.is_contract(addresses, *a, **k)


def token_meta(token):
    return ch.token_meta(token)
