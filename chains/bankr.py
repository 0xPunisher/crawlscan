"""Адаптер лаунчпада Bankr на Robinhood Chain (только при BANKR_ENABLED=true; по умолчанию выключен).

Bankr (bankr.bot, AI-агент) запускает токены через Doppler: Airlock выпускает токен и сразу кладёт его в пул
Uniswap V4 мультикривой (без бондинг-кривой); пул — в общем V4 PoolManager (тот же, что у Pons), хук пула —
инициализатор мультикривой Doppler (держит LP навсегда), модуль Bankr — антиснайп-комиссия (80% → ~1% за ~14 с)
и часть каждой сделки обратно в LP. Сеть та же, что у Pons (chains/robinhood.py): RPC, лимитер, счётчик
запросов, кэши чеков и времени блоков — общие.

Токен Bankr: адрес оканчивается на ba3 (vanity Bankr; на 3M блоков все 293 токена Doppler с ba3 — от Bankr,
другие интеграторы Doppler — другие суффиксы) — без RPC; затем один eth_call Airlock.getAssetData(token):
интегратор — Bankr. Запуск, пул (PoolKey), дев, получатели комиссий и вестинг — из чека транзакции выпуска.
Dump impact и probably rug — по котировкам V4Quoter (eth_call, без сделок): у свежего токена в PoolManager лежат
непроданные токены верхних диапазонов мультикривой, и формула по балансу пула занижает падение в десятки раз.
Вестинг дева (15% сапплая на контракте токена у большинства запусков): контракт — не холдер, остаток приписан
бенефициару (деву); ещё не разблокированное в q для impact не входит (продать сейчас нельзя).
Исследование и замеры — scripts/probe_bankr.py (локально), раздел «Bankr» в DEV_NOTES."""
import os

from chains import flap
from chains import robinhood as ch
import detect as d

CHAIN = ch.CHAIN
REQUESTS = ch.REQUESTS            # один счётчик HTTP с адаптером Robinhood
PACK_WINDOW = ch.PACK_WINDOW
BUNDLE_WINDOW = ch.BUNDLE_WINDOW
RUG_SNIPERS = ch.RUG_SNIPERS
RPS = ch.RPS
HISTORY_PARALLEL = ch.HISTORY_PARALLEL

SUFFIX = "ba3"
AIRLOCK = "0xeb7c034704ef8dcd2d32324c1545f62fb4ad0862"      # Doppler Airlock: выпуск токена и пул
INTEGRATOR = "0xf60633d02690e2a15a54ab919925f3d038df163e"   # интегратор Bankr в getAssetData
INITIALIZER = "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"  # инициализатор мультикривой V4 = хук пула, держит LP
HOOK = "0x9982538f41f2ae29ddb9d3d9307010052984fdbb"         # модуль Bankr: антиснайп-комиссия, часть комиссии в LP
MIGRATOR = "0xba2f330edb16cd8056f5988d8ce19bbc63475a0e"     # мигратор (пустой: пул не мигрирует)
PROTOCOL = "0x21e2ce70511e4fe542a97708e89520471daa7a66"     # получатель 5% комиссий LP (протокол)
FEE_RECIPIENTS = {PROTOCOL,                                 # получатели комиссии модуля Bankr (1/3 и 2/3)
                  "0x042455f9990098e11592be1fbd72e6dc68419b13", "0x5f8da8f88ec81e27f2e22fcb9ca5d926c595e508"}
V4_QUOTER = "0x8dc178efb8111bb0973dd9d722ebeff267c98f94"    # Uniswap V4Quoter (deployments/4663.md)
PM = ch.V4_POOL_MGR
ROUTERS = flap.ROUTERS | {  # проходные контракты (вход и выход токена в одной транзакции, баланс ~0), CHOP 2026-10-08
    "0x0da5899dbb36bd741885d4228f04cd9c6304fc56",
    "0x6aa80dbbed9ae5ab45fbf61f9644fada3b29326e",
    "0x0005ea38eb0a69d1253508ebdbdb9ea8cb26b5ef",
    "0x36dc95f1f088e11c0066dc19173f745655002bc2",
    "0xec9aa0a6c2372131a0502aba6188269ab52cacb5",
    "0x48a097df16c7844a33b1c3d11ab353457846e13f",
    "0x5399d94d2cab7c252a6034042e1917a0e5e17a18",
}
MARKET = ROUTERS | {PM, INITIALIZER, HOOK}   # отдал сюда — продал (detect.sellers)
INFRA = {AIRLOCK, INTEGRATOR, MIGRATOR} | FEE_RECIPIENTS
ZERO = "0x" + "0" * 40

SEL_ASSET_DATA = "0x1652e7b7"      # getAssetData(address) -> (numeraire, timelock, governance, migrator, initializer,
                                   #   poolOrHook, migrationPool, numTokensToSell, totalSupply, integrator)
SEL_AVAILABLE = "0x4c869795"       # computeAvailableVestedAmount(address): можно забрать из вестинга сейчас
SEL_QUOTE = "0xaa9d21cb"           # quoteExactInputSingle(((c0,c1,fee,spacing,hooks),zeroForOne,amount,hookData))
SEL_TRY_AGGREGATE = "0xbce38bd7"   # Multicall3.tryAggregate(bool,(address,bytes)[])
INITIALIZE = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"   # PoolManager: [id, c0, c1]
V4_SWAP = ch.V4_SWAP_TOPIC                                                           # [id, sender]: a0, a1, ...
BENEFICIARIES = "0x5be4f748347693e0500df872d81f7d96bce1b98e6f5adff0cfddfe3e9e415f20"  # инициализатор: [(адрес, доля)]
HOOK_BENEFICIARIES = "0x0c90f8fcadd900399eb6c30bc91ec4531380b92bc2c4c364675528b1d30601e2"
VESTING = "0x6b467f0a76daac5283d2251b6e7660fed01ea99dbd70aa7092f3318731690c9d"      # токен: [бенефициар, id], amount
WAD = 10 ** 18

# котировки для dump impact: q — доли оборота; у каждой точки пара (q, q × 1.01) — маржинальная цена после продажи q
QUOTE_GRID = (0.0005, 0.001, 0.002, 0.0035, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03, 0.05, 0.075, 0.1, 0.15, 0.2,
              0.3, 0.45, 0.65, 1.0)
QUOTE_REF = 1e-5                   # опорная точка: цена до продажи
QUOTE_STEP = 101                   # q × 101 / 100
QUOTE_GAS = 150_000_000            # 40 котировок с хуком ~25–30M газа (замер CHOP/AUREON: 24M на 32)
ENTRY_TOL = 0.06                   # покупатель получил не больше выхода свопа (+6%: округления, налог роутера) ...
ENTRY_MIN = 0.15                   # ... и не меньше 15% (антиснайп до 80% + комиссия роутера до ~5%)

_STATE = {}    # token -> getAssetData (Bankr навсегда: интегратор не меняется)
_LAUNCH = {}   # token -> запуск: блок, дев, PoolKey, получатели комиссий, вестинг (не меняется — навсегда)


def enabled():
    return os.environ.get("BANKR_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def candidate(token):
    """Похож на Bankr по адресу (без RPC): флаг включён и суффикс ba3."""
    return enabled() and isinstance(token, str) and token.lower().endswith(SUFFIX)


_words, _addr, _call, _arg = flap._words, flap._addr, flap._call, flap._arg


def _signed(x):
    return x - (1 << 256) if x >= 1 << 255 else x


def detect(token):
    """Токен Bankr -> состояние (dict) или None. Без суффикса ba3 (или флаг выключен) — None без запросов.
    Один eth_call Airlock.getAssetData: интегратор Bankr. Чужой адрес Airlock отдаёт нулями (не revert)."""
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
    """Событие бенефициаров: (offset, n, адрес, доля, ...) -> [(адрес, доля 0..1)]."""
    w = _words(data)
    n = w[1] if len(w) >= 2 else 0
    return [(_addr(w[2 + 2 * i]), w[3 + 2 * i] / WAD) for i in range(n) if 3 + 2 * i < len(w)]


def get_launch(token, st=None):
    """{"block", "tx", "curve", "deployer", "creator", "pool_id", "pool_key", "token_is0", "beneficiaries",
    "hook_beneficiaries", "vesting"} или None. Выпуск с 0x0 — один getLogs по адресу токена; чек и транзакция
    выпуска — один HTTP-батч. PoolKey — из Initialize PoolManager. Дев — получатель наибольшей доли комиссий LP
    (кроме протокола и получателей модуля Bankr); такого нет (всё протоколу) — tx.from. vesting — {бенефициар:
    токенов} из событий вестинга токена. Кэш навсегда (запуск и пул не меняются)."""
    token = token.lower()
    if token in _LAUNCH:
        return dict(_LAUNCH[token])
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
    h = mint["transactionHash"]
    rc, tx = ch.rpc_batch([("eth_getTransactionReceipt", [h]), ("eth_getTransactionByHash", [h])])
    key, ben, hook_ben, vest = None, [], [], {}
    for l in (rc or {}).get("logs", []):
        tp, addr = [t.lower() for t in l.get("topics") or []], l["address"].lower()
        if not tp:
            continue
        if addr == PM and tp[0] == INITIALIZE and len(tp) >= 4 and token in (ch.addr_from_topic(tp[2]),
                                                                                 ch.addr_from_topic(tp[3])):
            w = _words(l["data"])   # fee, tickSpacing (int24, знак расширен до 256 бит), hooks, sqrtPriceX96, tick
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
    """(market, excluded): рынок (PoolManager, инициализатор, модуль Bankr, роутеры) и все не-холдеры: рынок,
    контракт токена (вестинг), Airlock, интегратор, мигратор, получатели комиссий (кроме дева), инфраструктура сети."""
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
    agg = flap._encode_aggregate(calls)          # aggregate((address,bytes)[]): тот же массив после селектора
    return SEL_TRY_AGGREGATE + f"{0:064x}" + f"{64:064x}" + agg[10 + 64:]


def _decode_try_aggregate(res):
    """Ответ tryAggregate: (bool success, bytes returnData)[] -> [returnData или None]."""
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
    """(eth_call котировок одним Multicall3, [q]): опорная точка и сетка QUOTE_GRID, у каждой — q и q × 1.01."""
    qs = []
    for f in (QUOTE_REF,) + QUOTE_GRID:
        q = max(1, int(circulating * f))
        qs += [q, q * QUOTE_STEP // 100]
    zf1 = launch["token_is0"]          # продаём токен: token -> пара
    calls = [(V4_QUOTER, _quote_data(launch["pool_key"], zf1, q)) for q in qs]
    return ("eth_call", [{"to": flap.MULTICALL, "data": _encode_try_aggregate(calls), "gas": hex(QUOTE_GAS)},
                         "latest"]), qs


def impact_curve(qs, raw):
    """Котировки -> [(q, падение цены после продажи q)] по возрастанию q; [] — котировок нет.
    Падение — маржинальная цена после продажи q против цены до продажи: 1 − p(q) / p0, p(q) = Δout / Δq."""
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
        worst = max(worst, min(1.0, max(0.0, 1 - p / p0)))   # монотонно: продать больше — цена не выше
        out.append((q, worst))
    return out


def token_facts(token, launch, deadline=None):
    """Факты для движка (общий контракт сетей) + "impact_curve", "locked" и "bankr" (данные лаунчпада).
    История — как у Flap (flap.history: до 20 000 переводов целиком, больше — окнами в бюджете скана).
    Вестинг: остаток на контракте токена — деву (бенефициарам пропорционально выделенному), в обороте и в доле
    дева; "locked" — ещё не разблокированное (нельзя продать сейчас), движок вычитает его из q для impact.
    Котировки V4Quoter (impact) и доступное в вестинге — один HTTP-батч. reserve — баланс PoolManager (только для
    сведений и сверки с GT): impact — по котировкам, не по нему."""
    token = token.lower()
    st = _STATE.get(token) or detect(token) or {}
    supply = ch.token_supply(token)
    market, excluded = _sets(token, launch)
    transfers, base, hist, infra_bal = flap.history(token, launch["block"], supply, excluded, deadline)
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
    if hist.get("mode") == "windowed":
        missing = [b for b in bens if b not in base["balances"]]
        if missing:   # окнами: бенефициар мог не попасть в кандидаты — его баланс на кошельке читаем отдельно
            for b, v in flap.balances(token, missing).items():   # (оборот окнами = сапплай − инфраструктура:
                if v > 0:                                         #  этот баланс в нём уже есть)
                    base["balances"][b] = v
                    base["holders_total"] += 1
    left = min(in_contract, total)   # на контракте токена бывает и присланное сверх вестинга — это не дева
    remaining = {b: left * vest[b] // total for b in bens} if total else {}
    for b, v in remaining.items():   # остаток вестинга — деву: в обороте и в его балансе
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
    info = {"pool_id": launch["pool_id"], "pool_key": launch["pool_key"], "pair": pair_info,
            "dev": launch.get("deployer"), "creator": launch.get("creator"),
            "fee_recipients": sorted(excluded & ({a for a, _ in launch.get("beneficiaries") or []}
                                                 | {a for a, _ in launch.get("hook_beneficiaries") or []}
                                                 | FEE_RECIPIENTS)),
            "vesting": {"total": total, "unlocked": unlocked, "released": released, "in_contract": left,
                        "locked": sum(locked.values()), "beneficiaries": {b: vest[b] for b in bens},
                        "total_share_supply": total / supply if supply else 0.0,
                        "unlocked_share_supply": unlocked / supply if supply else 0.0},
            "impact_source": "v4_quoter" if curve else None,
            "impact_curve": [[q / circ if circ else 0.0, i] for q, i in curve],
            "pm_reserve": reserve, "infra": sorted({token, PM, INITIALIZER, HOOK, AIRLOCK}), "history": hist}
    return {"supply": supply, "transfers": transfers, "excluded": excluded, "market": market,
            "reserve": reserve, "reserve_ok": bool(curve), "base": base, "impact_curve": curve,
            "locked": locked, "bankr": info}


def _user_swaps(logs, launch):
    """V4 Swap пула токена не от хука/инициализатора (их собственные свопы комиссии) с выходом токена:
    [(токенов из свопа, котировочная сторона)]."""
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
    """Первое получение токена каждым кошельком (контракт как у ch.classify_entries).
    buy — в чеке транзакции есть своп пула токена с выходом токена (не собственный своп хука комиссии): покупки
    первых секунд с антиснайп-комиссией хука — тоже покупки. eth_in — котировочная сторона свопа (пара ETH), если
    кошелёк получил от 15% до 106% выхода свопа (антиснайп до 80%, комиссия роутера).
    vesting — бенефициар вестинга (дев): его доля выделена при запуске (вход — транзакция запуска, даже если токены
    ещё на контракте). fees — первый вход деву из инициализатора/модуля (комиссии LP). Иначе transfer."""
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
        if t is None:   # вход не найден (история окнами не дочитана) — без угадывания
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
    """Первые n покупателей после запуска (контракт как у ch.early_buyers) или None (не Bankr).
    Общее ядро Robinhood: покупка без чека — токен прямо из PoolManager или роутера; иначе по чеку
    (classify_entries). Инфраструктура Bankr (вестинг, получатели комиссий) — не покупатели."""
    token = token.lower()
    st = _STATE.get(token) or detect(token)
    launch = get_launch(token, st) if st else None
    if launch is None:
        return None
    market, excluded = _sets(token, launch)
    return ch.early_buyers_from(token, n, deadline, launch, excluded, ROUTERS | {PM}, classify_entries)


def early_status(token, buyers, deadline=None, launch=None, out_cap=ch.EARLY_OUT_CAP):
    """Что ранние покупатели сделали с токеном (контракт как у ch.early_status): выход на рынок — продажа."""
    return ch.early_status_from(token.lower(), buyers, deadline, MARKET, flap._sell(MARKET), out_cap=out_cap)


# общее с Robinhood: вызываются в момент вызова (тесты подменяют функции ch)
def block_timestamps(blocks, *a, **k):
    return ch.block_timestamps(blocks, *a, **k)


def wallet_distinct_tokens(*a, **k):
    return ch.wallet_distinct_tokens(*a, **k)


def is_contract(addresses, *a, **k):
    return ch.is_contract(addresses, *a, **k)


def token_meta(token):
    return ch.token_meta(token)
