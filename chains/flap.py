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
Замеры и события — scripts/probe_flap.py (локально), раздел «Flap» в DEV_NOTES."""
import os

from chains import robinhood as ch

CHAIN = ch.CHAIN
REQUESTS = ch.REQUESTS            # один счётчик HTTP с адаптером Robinhood
PACK_WINDOW = ch.PACK_WINDOW
BUNDLE_WINDOW = ch.BUNDLE_WINDOW
RUG_SNIPERS = ch.RUG_SNIPERS

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


def token_facts(token, launch):
    """Факты для движка (общий контракт сетей) + "q_factor" и "flap" (данные лаунчпада для результата).
    Резерв: на кривой — виртуальный h + 1e9 − circulating из состояния Portal; после выпуска — getReserves пары.
    q_factor = 1 − sellTax после выпуска (налог токенами: в пару уходит q × (1 − sellTax)), на кривой 1.0
    (налог берётся в ETH, в кривую уходит всё q). Инфраструктура и рынок — см. INFRA / ROUTERS."""
    token = token.lower()
    st = _STATE.get(token) or detect(token)
    transfers = ch.get_token_transfers(token, launch["block"])
    supply = ch.token_supply(token)
    dex = st["status"] == STATUS_DEX
    pool = st["main_pool"]
    own = {token} | {x for x in (st["tax_processor"], pool, launch.get("shadow_pair")) if x}
    if st["tax_recipient"] and st["tax_recipient_vault"]:
        own.add(st["tax_recipient"])          # Flap Vault — контракт получателя налога, не холдер
    market = {PORTAL} | PORTAL_HELPERS | ROUTERS | {ch.V4_POOL_MGR} | own - {launch.get("shadow_pair")}
    excluded = ch.INFRA | INFRA | market | own
    if dex:   # пара не прочиталась — резерв 0: reserve_ok False («liquidity not measured»), а не формула кривой
        reserve, is0 = _pair_reserve(token, pool) if pool else (0, None)
        st["token_is0"] = is0
        source = "pair_reserves"
    else:
        reserve = st["h"] + SUPPLY - st["circulating"]
        source = "curve_state"
    q_factor = 1.0 - st["sell_tax"] / 10_000 if dex else 1.0
    locked = {}
    for t in transfers:   # сколько токенов сейчас в локерах (исключены из оборота, но это не «ничьи» токены)
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
            "infra": sorted(own | {PORTAL})}
    return {"supply": supply, "transfers": transfers, "excluded": excluded, "market": market,
            "reserve": max(0, reserve), "reserve_ok": reserve > 0, "base": None, "q_factor": q_factor,
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
