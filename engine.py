"""Дирижёр скана: идёт по шагам, вызывает адаптер и детекторы, на каждом шаге — emit(event).
Жёсткий бюджет 25 секунд: кошельки, чью историю не успели прочитать, помечаются unread,
скан их не ждёт: забираем только тех, кто успел до дедлайна, зависших не ждём.
Сеть — по адресу: 0x + 40 hex → Robinhood (chains/robinhood.py), base58 → Solana
(chains/solana.py, только при SOLANA_ENABLED=true). Оба адаптера отдают одинаковые факты.
Запуск из кода: engine.scan("0x...", emit=print) -> result (dict)."""
import math, os, re, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _CFTimeout

from chains import bankr
from chains import flap
from chains import priority
from chains import robinhood as ch
from chains import solana as sol
import detect as d
import market

BUDGET = 25.0          # секунд на весь скан (README)
DETECT_RESERVE = 1.0   # секунд оставляем на detect и события после чтения кошельков
MARKET_WAIT = 3.0      # шапку GeckoTerminal ждём до первого RPC не дольше стольких секунд от старта скана
MARKET_LATE = 20.0     # не дождались — запрос GT продолжается в фоне (до стольких секунд на все попытки),
                       # поздний ответ проверяется перед вердиктом
RESERVE_MIN_SEEN = 0.25  # резерв в $ < 25% токенной стороны ликвидности GT (liquidity / 2) — пулы не найдены
ESTABLISHED_ENV = {"min_age_days": "ESTABLISHED_MIN_AGE_DAYS", "min_liquidity_usd": "ESTABLISHED_MIN_LIQUIDITY_USD",
                   "min_mcap_usd": "ESTABLISHED_MIN_MCAP_USD"}
WORKERS = 12           # потоков чтения истории кошельков; лимитер RPS в адаптере общий
# сколько кошельков читать одновременно (по убыванию доли): адаптер с тяжёлыми запросами под общим лимитером
# (Robinhood: батч из 10 getLogs = 10 слотов RPS) задаёт HISTORY_PARALLEL — при нехватке бюджета крупнейшие
# успевают первыми; без атрибута — все WORKERS сразу, как раньше
SPIDERS = 6            # пауков; кошельки раздаются по кругу
HIST_CAP = d.SHORT_HISTORY_MAX + 1  # монеты до входа считаем до 4: важно 0 / 1..3 / больше
USE_FUNDING = False    # фандинг выключен: eth_inflows/outgoing_count не вызываются,
                       # связь "общий фандер" и сигнал one_shot_funded выключены

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CHAINS = {"robinhood": ch, "solana": sol}
NATIVE = {"robinhood": ("ETH", 10 ** 18), "solana": ("SOL", 10 ** 9)}   # единица eth_in
NOT_LAUNCHPAD = {"robinhood": "not a Pons V2 token", "solana": "not a pump.fun token"}
NOT_LAUNCHPAD_FLAP = "not a Pons V2 or Flap token"   # Robinhood при FLAP_ENABLED
TOO_LARGE = "token history too large"                  # Pons: больше ch.SCAN_MAX_LOGS переводов
GT_NETWORK = {"robinhood": "robinhood", "solana": "solana"}


class ScanError(Exception):
    """Понятная пользователю ошибка скана (плохой адрес, не Pons V2 / pump.fun, Solana выключена)."""


PARTIAL_BUILDING = "Partial scan: building the full holder history, check again in {eta}"
PARTIAL_TOO_LARGE = "Partial scan: the holder history is too large to read in full, top holders are approximate"
PARTIAL_QUEUED = "Partial scan: full holder history queued: {pos} in line, check again in {eta}"
PARTIAL_QUEUE_FULL = "Partial scan: full history is queued, check again later"
PARTIAL_LATER = "Partial scan: the full holder history is not available right now, check again later"


def bankr_partial(token, hist):
    """Частичный скан Bankr -> {"state", "eta_s", "message"[, "position"]} (state — bankr.index_status: building |
    queued | queue_full | too_large | unavailable | none | ready). ready — индекс достроился, пока шёл скан: следующий
    скан будет полным."""
    st = bankr.index_status(token)
    state, eta = st["state"], st["eta_s"]
    if state in ("building", "ready"):
        msg = PARTIAL_BUILDING.format(eta="about a minute" if state == "ready" else _about(eta))
    elif state == "queued":
        msg = PARTIAL_QUEUED.format(pos=_ordinal(st["position"]), eta=_about(eta))
        return {"state": state, "eta_s": eta, "position": st["position"], "message": msg}
    elif state == "queue_full":
        msg = PARTIAL_QUEUE_FULL
    elif state == "too_large":
        msg = PARTIAL_TOO_LARGE
    else:
        msg = PARTIAL_LATER
    return {"state": state, "eta_s": eta, "message": msg}


def _about(eta_s):
    mins = math.ceil((eta_s or 60) / 60)
    return "about a minute" if mins <= 1 else f"about {mins} minutes"


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def not_launchpad(chain):
    """Текст ошибки «не с лаунчпада» по включённым лаунчпадам сети (выключатели FLAP_ENABLED, BANKR_ENABLED)."""
    if chain != "robinhood":
        return NOT_LAUNCHPAD[chain]
    pads = ["Pons V2"] + (["Flap"] if flap.enabled() else []) + (["Bankr"] if bankr.enabled() else [])
    return f"not a {pads[0]} token" if len(pads) == 1 else \
        f"not a {', '.join(pads[:-1])} or {pads[-1]} token"


def solana_enabled():
    return os.environ.get("SOLANA_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def chain_of(token):
    """CA → (сеть, нормализованный адрес) или ScanError.
    0x + 40 hex → robinhood (нижний регистр); base58 32 байта → solana (регистр сохраняется);
    Solana при выключенном SOLANA_ENABLED → "Solana support is coming soon"."""
    token = (token or "").strip() if isinstance(token, str) or token is None else ""
    if ADDR_RE.match(token):
        return "robinhood", token.lower()
    if sol.is_address(token):
        if not solana_enabled():
            raise ScanError("Solana support is coming soon")
        return "solana", token
    raise ScanError("not a token address")


def established_limits():
    """Пороги «too established» из env (ESTABLISHED_*), неверное или пустое значение — дефолт detect.ESTABLISHED."""
    out = {}
    for k, name in ESTABLISHED_ENV.items():
        try:
            v = float(os.environ.get(name) or "nan")
            out[k] = v if v == v and v >= 0 else d.ESTABLISHED[k]
        except ValueError:
            out[k] = d.ESTABLISHED[k]
    return out


def reserve_seen(reserve, supply, gt):
    """Резерв согласуется с ликвидностью GT: R в $ (R / supply × FDV) не меньше RESERVE_MIN_SEEN токенной
    стороны (liquidity / 2). Меньше — пулы найдены не все (другой DEX, вне топ-20): резерв ненадёжен.
    Нет чисел GT — проверить нечем, True."""
    fdv, liq = gt.get("fdv_usd"), gt.get("liquidity_usd")
    if not reserve or not supply or not fdv or not liq:
        return True
    return reserve / supply * fdv >= liq / 2 * RESERVE_MIN_SEEN


def _header(gt, meta=None, age_h=None):
    meta = meta or {}
    return {"name": gt.get("name") or meta.get("name"), "ticker": gt.get("ticker") or meta.get("symbol"),
            "price_usd": gt.get("price_usd"), "mcap_usd": gt.get("mcap_usd"), "fdv_usd": gt.get("fdv_usd"),
            "liquidity_usd": gt.get("liquidity_usd"), "vol24h_usd": gt.get("vol24h_usd"), "age_h": age_h}


def established_result(token, chain, gt, limits, ev, t0, rpc_requests=0):
    """Результат без полного скана: токен старый и большой (detect.too_established). Без скора.
    rpc_requests > 0 — вердикт по позднему ответу GT, скан уже шёл. market_source — откуда данные рынка
    ("gt" | "dexscreener", market.fetch_market), market_pool — пул для виджета чарта."""
    ev("done", d.ESTABLISHED_TEXT, score=None, band=d.TOO_ESTABLISHED, headline=d.ESTABLISHED_HEADLINE, rug=None)
    age = gt.get("age_days")
    return {"token": token, "chain": chain, "header": _header(gt, age_h=None if age is None else round(age * 24, 1)),
            "score": None, "band": d.TOO_ESTABLISHED, "headline": d.ESTABLISHED_HEADLINE,
            "reason": d.ESTABLISHED_TEXT, "parts": {}, "gates": [], "metrics": {}, "rug": None,
            "holders": [], "holders_total": None, "operators": [], "links": [], "packs": [], "unread": [],
            "established": {"liquidity_usd": gt.get("liquidity_usd"), "mcap_usd": gt.get("mcap_usd"),
                            "age_days": age, "limits": limits},
            "market_source": gt.get("source"), "market_pool": gt.get("pool"),
            "elapsed_s": round(time.time() - t0, 1), "rpc_requests": rpc_requests}


def active_result(token, chain, gt, launch, info, est_logs, ev, t0, rpc_requests):
    """Результат без вердикта: история токена Bankr не помещается в обычный скан, а фоновый индекс выключен
    (bankr.TooActive). Только то, что точно без полной истории: вестинг дева, цена, ликвидность, капа (GT), чарт."""
    ev("done", d.ACTIVE_TEXT, score=None, band=d.TOO_ACTIVE, headline=d.ACTIVE_HEADLINE, rug=None)
    meta = {} if gt.get("name") else ch.token_meta(token)
    age_h = round((time.time() - launch["ts"]) / 3600, 1) if launch.get("ts") else None
    return {"token": token, "chain": chain, "launchpad": "bankr", "bankr": info, "header": _header(gt, meta, age_h),
            "launch": launch, "score": None, "band": d.TOO_ACTIVE, "headline": d.ACTIVE_HEADLINE,
            "reason": d.ACTIVE_TEXT, "parts": {}, "gates": [], "metrics": {}, "rug": None,
            "holders": [], "holders_total": None, "operators": [], "links": [], "packs": [], "unread": [],
            "too_active": {"est_logs": est_logs}, "partial_scan": None, "limited": False,
            "market_source": gt.get("source"), "market_pool": gt.get("pool"),
            "elapsed_s": round(time.time() - t0, 1), "rpc_requests": rpc_requests}


def validate(token):
    """CA → нормализованный адрес или ScanError (см. chain_of)."""
    return chain_of(token)[1]


class _RankGate:
    """Окно чтения кошельков: задача ранга i стартует, когда i < освобождено + width (не больше width одновременно,
    в порядке рангов — крупнейшие первыми). Слот освобождается, когда задача закончила или выбрала свою долю
    времени slot_s (тогда дочитывает в фоне, но очередь не держит: медленный кошелёк не блокирует следующих).
    close() — бюджет вышел: ждущие задачи сразу выходят без чтения."""

    def __init__(self, width, slot_s=None):
        self.width, self.slot_s, self.released, self.closed = width, slot_s, 0, False
        self.cv = threading.Condition()

    def _release(self, done):
        with self.cv:
            if not done[0]:
                done[0] = True
                self.released += 1
                self.cv.notify_all()

    def run(self, rank, fn, *args):
        with self.cv:
            self.cv.wait_for(lambda: self.closed or rank < self.released + self.width)
            if self.closed:
                return None
        done = [False]
        timer = None
        if self.slot_s is not None:
            timer = threading.Timer(max(0.0, self.slot_s), self._release, (done,))
            timer.daemon = True
            timer.start()
        try:
            return fn(*args)
        finally:
            if timer:
                timer.cancel()
            self._release(done)

    def close(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()


_HIST, _HIST_LOCK = {}, threading.Lock()  # (wallet, before_block) -> монет до входа

def _history(a, wallet, before_block, token):
    key = (wallet, before_block)
    with _HIST_LOCK:
        if key in _HIST:
            return _HIST[key]
    n = a.wallet_distinct_tokens(wallet, before_block, token, window=None, cap=HIST_CAP)
    if n is not None:  # None — адаптер не уложился в лимит чтения: в следующий раз читаем заново
        with _HIST_LOCK:
            _HIST[key] = n
    return n


def _flags(s):
    return [f for f, on in (("unread", s["unread"]), ("virgin", s["virgin"]),
                            ("short_history", s["short_history"]), ("one_shot_funded", s["one_shot_funded"]),
                            ("sniper", s["sniper"]), ("deployer", s["is_deployer"]), ("sold", s["sold"])) if on]


def _reason(sc, base):
    """Главная причина вердикта одной строкой."""
    if sc["band"] == d.TOO_EARLY:
        return f"fewer than {d.MIN_HOLDERS} holders ({base['holders_total']}) — too early or too late"
    hard = [g for g in sc["gates"] if not g.startswith("soft:")]
    if hard:
        return hard[0]
    soft = [g for g in sc["gates"] if g.startswith("soft:")]
    if soft:
        return soft[0][len("soft: "):]
    m = sc["metrics"]
    loss = {k: d.WEIGHTS[k] - v for k, v in sc["parts"].items()}
    k = max(loss, key=loss.get)
    if loss[k] < 5:
        return "no clear signs of a single operator"
    return {"operator": (f"biggest operator could move price −{m['impact'] * 100:.0f}% if sold "
                         f"({m['operator'] * 100:.1f}% of float)" if m["impact"] is not None else
                         f"biggest operator holds {m['operator'] * 100:.1f}% of float (liquidity not measured)"),
            "virgin": (f"{m['virgin'] * 100:.0f}% virgin wallets in top" if m["virgin"] is not None
                       else "virgin share unknown: most of the top couldn't be read"),
            "transfer": f"{m['transfer'] * 100:.1f}% of float received by transfer",
            "sniper": f"snipers hold {m['sniper'] * 100:.1f}% of float",
            "concentration": f"top 20 hold {m['concentration'] * 100:.1f}% of float"}[k]


def scan(token, emit=lambda e: None):
    """Полный скан токена. emit(event) — события в формате README. Возвращает result (dict).
    ScanError — понятная ошибка (адрес, не Pons V2 / pump.fun, Solana выключена); прочие исключения — сбой.
    Факты о токене адаптер сети отдаёт в общей форме (token_facts): переводы, сапплай,
    исключённые адреса, рынок, резерв токенов ликвидности (reserve) и, если сеть знает
    балансы напрямую (Solana), base."""
    t0 = time.time()
    chain, token = chain_of(token)
    a = CHAINS[chain]
    # фоновый скан (перепроверка alerts) идёт под своим лимитом BACKGROUND_RPS: бюджет растёт во столько же раз,
    # чтобы прочитать столько же кошельков, сколько живой скан
    deadline = t0 + BUDGET * (priority.background_scale(a.RPS) if priority.is_background() else 1.0)
    r0 = a.REQUESTS[0]
    seq = [0]

    def ev(type_, detail="", wallet=None, spider=None, **extra):
        e = {"i": seq[0], "t": int((time.time() - t0) * 1000), "type": type_,
             "spider": spider, "wallet": wallet, "detail": detail, "chain": chain} | extra
        seq[0] += 1
        emit(e)

    # Рынок (GeckoTerminal, не ответил — DexScreener: market.fetch_market) — до первого RPC: шапка и проверка
    # «too established». Вердикт too established в кэше (market.ESTABLISHED_TTL) — сразу он, без запросов.
    # Не ответил за MARKET_WAIT — скан идёт, запрос продолжается в фоне; поздний ответ проверяем между этапами
    # и перед вердиктом. Рынок недоступен — обычный скан (старый токен — limited).
    network = GT_NETWORK[chain]
    limits = established_limits()
    cached = market.established_get(token, network)
    if cached is not None and d.too_established(cached, limits):
        return established_result(token, chain, cached, limits, ev, t0)
    mkt_box = {}
    mkt_thread = threading.Thread(target=lambda: mkt_box.update(market.fetch_market(token, network,
                                                                                    budget=MARKET_LATE)),
                                  daemon=True)
    mkt_thread.start()
    mkt_thread.join(timeout=max(0.0, t0 + MARKET_WAIT - time.time()))
    gt = dict(mkt_box)
    late = mkt_thread.is_alive()
    if late:
        print(f"gt: no answer in {MARKET_WAIT:g}s for {token}, scanning; late check before verdict", flush=True)
    if d.too_established(gt, limits):
        market.established_put(token, network, gt)
        return established_result(token, chain, gt, limits, ev, t0)

    def late_established():
        """Поздний ответ GT уже пришёл и токен too established — результат (скан дальше не идёт), иначе None."""
        if not late or mkt_thread.is_alive() or not d.too_established(mkt_box, limits):
            return None
        print(f"gt: late answer for {token} at {time.time() - t0:.1f}s: too established", flush=True)
        market.established_put(token, network, mkt_box)
        return established_result(token, chain, dict(mkt_box), limits, ev, t0, a.REQUESTS[0] - r0)

    ev("stage", "launch")
    # лаунчпад Robinhood: Flap — только при FLAP_ENABLED, суффикс 8888/7777 (без RPC) и подтверждение Portal
    # (один HTTP); Bankr — только при BANKR_ENABLED, суффикс ba3 (без RPC) и один eth_call Airlock;
    # остальные — Pons прежним путём, без дополнительных запросов
    flap_state = flap.detect(token) if chain == "robinhood" else None
    bankr_state = bankr.detect(token) if chain == "robinhood" and flap_state is None else None
    eth_box, eth_thread = {}, None
    if bankr_state is not None:
        a = bankr
        launch = bankr.get_launch(token, bankr_state)
    elif flap_state is not None:
        a = flap
        if flap_state["status"] != flap.STATUS_DEX:   # кривая: GT её не знает, цена = Portal × ETH/USD (параллельно)
            eth_thread = threading.Thread(target=lambda: eth_box.update(usd=market.native_usd(network)), daemon=True)
            eth_thread.start()
        launch = flap.get_launch(token, flap_state)
    else:
        launch = a.get_launch(token)
    if launch is None:
        raise ScanError(not_launchpad(chain))
    if est := late_established():
        return est

    ev("stage", "transfers")
    # Flap: история большого токена читается окнами в бюджете скана (deadline считается от начала скана)
    try:
        facts = a.token_facts(token, launch, deadline) if a in (flap, bankr) else a.token_facts(token, launch)
    except ch.HistoryTooLarge as e:
        print(f"scan: {token} history too large ({e} logs, cap {ch.SCAN_MAX_LOGS})", flush=True)
        raise ScanError(TOO_LARGE) from None
    except bankr.TooActive as e:   # Bankr, индекс выключен: история больше обычного скана — без вердикта
        if est := late_established():
            return est
        bankr.mark_active(token)
        info = bankr.active_info(token, launch)
        if late:   # рынок нужен для шапки: ждём поздний ответ GT в пределах бюджета
            mkt_thread.join(timeout=max(0.0, deadline - time.time()))
            gt = dict(mkt_box)
        ts0 = a.block_timestamps([launch["block"]]).get(launch["block"])
        return active_result(token, chain, gt, launch | {"ts": ts0}, info, e.est_logs, ev, t0, a.REQUESTS[0] - r0)
    transfers, supply, excluded, mkt = facts["transfers"], facts["supply"], facts["excluded"], facts["market"]
    base = facts["base"] or d.supply_base(transfers, supply, excluded)
    holders = d.top_holders(base)
    hs = [w for w, _, _ in holders]
    ratio = base["circulating"] / supply if supply else 0.0

    if est := late_established():
        return est

    ev("stage", "entries")
    entries = a.classify_entries(token, transfers, hs)
    ts = a.block_timestamps([launch["block"]] + [e["block"] for e in entries.values() if e["block"] is not None])
    sold = d.sellers(transfers, mkt)
    # вход не найден (Solana: kind None) — без времени входа и без чтения истории: unread, не девственный
    data = {w: entries[w] | {"entry_ts": ts.get(entries[w]["block"]), "inflows": [], "sold": w in sold,
                             "distinct_tokens": None} for w in hs}
    unit, scale = NATIVE[chain]

    if est := late_established():
        return est

    ev("stage", "wallets")
    spider = {w: i % SPIDERS for i, w in enumerate(hs)}
    share = {a: s for a, _, s in holders}
    for w in hs:
        ev("spider_move", "to wallet", wallet=w, spider=spider[w])
    # пул перепроверки alerts — тоже фоновый (chains.priority: уступает живым сканам в лимитере RPS)
    ex = ThreadPoolExecutor(max_workers=WORKERS,
                            thread_name_prefix=f"{priority.BG}-pool" if priority.is_background() else "")
    order = [w for w in hs if entries[w]["kind"] is not None]   # hs — по убыванию доли: крупнейшие первыми
    width = getattr(a, "HISTORY_PARALLEL", WORKERS)
    left = max(0.05, deadline - DETECT_RESERVE - time.time())
    # доля времени на кошелёк: если каждый уложится в неё, успеют все; кто дольше — уступает слот следующему
    gate = _RankGate(width, left * width / len(order) if width < len(order) else None)
    futs = {ex.submit(gate.run, i, _history, a, w, entries[w]["block"], token): w for i, w in enumerate(order)}

    def flag(w):
        s = d.wallet_signals({w: data[w]}, ts[launch["block"]], launch["deployer"],
                             launch["block"], a.BUNDLE_WINDOW)[w]
        eth = "" if s["eth_in"] is None else f" {s['eth_in'] / scale:.4f} {unit}"
        fl = _flags(s)
        when = "entry not found" if data[w]["entry_ts"] is None else f"+{data[w]['entry_ts'] - ts[launch['block']]}s"
        ev("wallet_flag", f"{s['kind'] or 'unknown'}{eth}, {when}"
           + (", " + ", ".join(fl) if fl else ""), wallet=w, spider=spider[w],
           flags=fl, kind=s["kind"], share=share[w], share_supply=share[w] * ratio)

    done = set()
    try:
        for f in as_completed(futs, timeout=max(0.05, deadline - DETECT_RESERVE - time.time())):
            w = futs[f]
            try:
                data[w]["distinct_tokens"] = f.result()
            except Exception:
                pass
            done.add(w)
            flag(w)
    except _CFTimeout:
        pass
    gate.close()
    ex.shutdown(wait=False, cancel_futures=True)
    unread = [w for w in hs if w not in done]
    for w in unread:
        flag(w)

    ev("stage", "links")
    sig = d.wallet_signals(data, ts[launch["block"]], launch["deployer"], launch["block"], a.BUNDLE_WINDOW)
    packs = d.find_packs(sig, window=a.PACK_WINDOW)
    # раздатчики токена, общие для ≥ 2 холдеров: проверяем только на контракт
    hset, by_src = set(hs), {}
    for t in transfers:
        if t["to"] in hset and t["frm"] not in hset and t["frm"] not in excluded:
            by_src.setdefault(t["frm"], set()).add(t["to"])
    cand = {a for a, ws in by_src.items() if len(ws) > 1}
    contracts = {x for x, c in a.is_contract(cand).items() if c} if cand else set()
    # без фандинга исходящих ETH не знаем: раздатчик-кошелёк (не контракт) считаем не хабом
    outgoing = {a: 0 for a in cand - contracts}
    links = d.find_links(hs, transfers, sig, {}, outgoing, contracts, excluded, packs)
    ops = d.operators(holders, links, packs, ratio)
    for l in links:
        ev("link", l["kind"], wallet=l["a"], b=l["b"], level=l["level"], kind=l["kind"], via=l["via"])
    for o in ops:
        if len(o["wallets"]) > 1:
            ev("cluster", f"{len(o['wallets'])} wallets, {o['share'] * 100:.1f}% of float",
               wallet=o["wallets"][0], wallets=o["wallets"], level=o["level"],
               share=o["share"], share_supply=o["share_supply"])

    # поздний ответ GT (не дождались за MARKET_WAIT): если пришёл — используем, too established — вердикт он
    if est := late_established():
        return est
    if late:
        if not mkt_thread.is_alive():
            gt = dict(mkt_box)
        print(f"gt: late answer for {token} at {time.time() - t0:.1f}s: "
              + ("ok" if gt else "still none" if mkt_thread.is_alive() else "failed"), flush=True)
    # подстраховка без GT: рынка не знаем, а токен по блокчейну старше порога too established —
    # сигналы холдеров ограничены: без жёстких правил по impact и transfer и без probably rug
    limited = not gt and time.time() - ts[launch["block"]] > limits["min_age_days"] * 86400
    # Flap: большая история прочитана окнами и топ не гарантированно точный — сигналы холдеров тоже ограничены
    hist = (facts.get("flap") or facts.get("bankr") or {}).get("history") or {}
    partial = hist.get("mode") == "windowed" and not hist.get("top_exact")
    limited_note = d.PARTIAL_NOTE if partial and not limited else d.LIMITED_NOTE
    limited = limited or partial
    # Bankr: большая история, полный индекс холдеров ещё строится (или недоступен) — частичный скан:
    # вердикт не лучше RISKY, на сайте и в боте — «Partial scan: …» (Pons и Flap — прежний путь)
    partial_scan = bankr_partial(token, hist) if "bankr" in facts and partial else None
    # резерв надёжен: адаптер нашёл пул (на кривой — всегда) и он согласуется с ликвидностью GT
    reserve_ok = facts.get("reserve_ok", True) and reserve_seen(facts["reserve"], supply, gt)
    q_factor = facts.get("q_factor", 1)   # Flap после выпуска: налог на продажу токенами (1 − sellTax)
    # Bankr: impact по котировкам V4Quoter (impact_curve), не по резерву; невыкупаемый вестинг дева (locked) —
    # в его доле, но не в q (продать сейчас нельзя); dev — доля дева из контрактов (правило и при limited).
    # Pons и Flap: None — прежний путь
    curve, locked = facts.get("impact_curve"), facts.get("locked")
    sc = d.score(holders, sig, ops, base, facts["reserve"], gt.get("liquidity_usd"), reserve_ok=reserve_ok,
                 limited=limited, q_factor=q_factor, limited_note=limited_note, impact_curve=curve, locked=locked,
                 dev=facts.get("dev"), partial=partial_scan is not None)
    reason = _reason(sc, base)
    # probably rug: только вычисления на уже собранных данных, без запросов в сеть
    # и только если DANGER вызван поведенческим жёстким правилом (не одиночным китом в тонком пуле)
    behavioral = any(not g.startswith("soft:") for g in sc["gates"])
    rug = None if limited else d.rug_projection(holders, sig, ops, base, facts["reserve"], sc["band"],
                                                snipers=a.RUG_SNIPERS, reserve_ok=reserve_ok, behavioral=behavioral,
                                                q_factor=q_factor, impact_curve=curve, locked=locked)
    # Flap на кривой: GeckoTerminal её не знает — имя и тикер из контракта, цена = Portal × ETH/USD,
    # капа = цена × весь сапплай, ликвидность = ETH в кривой × ETH/USD
    fl = facts.get("flap") or {}
    curve = fl.get("phase") == "bonding_curve"
    if curve:
        gt = {k: v for k, v in gt.items()
              if k not in ("name", "ticker", "price_usd", "mcap_usd", "liquidity_usd", "vol24h_usd")}
        if eth_thread is not None:
            eth_thread.join(timeout=max(0.0, min(2.0, deadline - time.time())))
        usd = eth_box.get("usd")
        if usd and fl.get("price_eth"):
            gt.update(price_usd=fl["price_eth"] * usd, mcap_usd=fl["price_eth"] * usd * supply / 1e18)
        if usd and fl.get("reserve_eth") is not None:
            gt["liquidity_usd"] = fl["reserve_eth"] * usd
    if rug:
        rug["level_usd"] = gt["price_usd"] * rug["level_factor"] if gt.get("price_usd") else None
    meta = {}
    if not gt.get("name"):
        meta = a.token_meta(token)
    header = _header(gt, meta, round((time.time() - ts[launch["block"]]) / 3600, 1))
    elapsed = round(time.time() - t0, 1)
    ev("done", reason, score=sc["score"], band=sc["band"], headline=sc["headline"], rug=rug)

    extra = ({"launchpad": "flap", "flap": facts["flap"]} if "flap" in facts else
             {"launchpad": "bankr", "bankr": facts["bankr"]} if "bankr" in facts else {})
    return extra | {
        "token": token, "chain": chain, "header": header,
        "launch": launch | {"ts": ts[launch["block"]]},
        "supply": supply, "circulating": base["circulating"], "holders_total": base["holders_total"],
        "holders": [{"wallet": a, "share": s, "share_supply": s * ratio, "spider": spider[a],
                     "signals": sig[a]} for a, _, s in holders],
        "links": links, "packs": packs, "operators": ops,
        "score": sc["score"], "band": sc["band"], "parts": sc["parts"], "gates": sc["gates"],
        "metrics": sc["metrics"], "headline": sc["headline"], "reason": reason, "notes": sc.get("notes", []),
        "reserve": facts["reserve"],
        "reserve_ok": reserve_ok, "limited": limited, "market_source": gt.get("source"),
        "market_pool": gt.get("pool"),
        "rug": rug, "partial_scan": partial_scan,
        "unread": unread, "use_funding": USE_FUNDING,
        "elapsed_s": elapsed, "rpc_requests": a.REQUESTS[0] - r0,
    }
