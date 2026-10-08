"""Дирижёр скана: идёт по шагам, вызывает адаптер и детекторы, на каждом шаге — emit(event).
Жёсткий бюджет 25 секунд: кошельки, чью историю не успели прочитать, помечаются unread,
скан их не ждёт: забираем только тех, кто успел до дедлайна, зависших не ждём.
Сеть — по адресу: 0x + 40 hex → Robinhood (chains/robinhood.py), base58 → Solana
(chains/solana.py, только при SOLANA_ENABLED=true). Оба адаптера отдают одинаковые факты.
Запуск из кода: engine.scan("0x...", emit=print) -> result (dict)."""
import os, re, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _CFTimeout

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
GT_NETWORK = {"robinhood": "robinhood", "solana": "solana"}


class ScanError(Exception):
    """Понятная пользователю ошибка скана (плохой адрес, не Pons V2 / pump.fun, Solana выключена)."""


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
            "price_usd": gt.get("price_usd"), "mcap_usd": gt.get("mcap_usd"),
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
    launch = a.get_launch(token)
    if launch is None:
        raise ScanError(NOT_LAUNCHPAD[chain])
    if est := late_established():
        return est

    ev("stage", "transfers")
    facts = a.token_facts(token, launch)
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
    # резерв надёжен: адаптер нашёл пул (на кривой — всегда) и он согласуется с ликвидностью GT
    reserve_ok = facts.get("reserve_ok", True) and reserve_seen(facts["reserve"], supply, gt)
    sc = d.score(holders, sig, ops, base, facts["reserve"], gt.get("liquidity_usd"), reserve_ok=reserve_ok,
                 limited=limited)
    reason = _reason(sc, base)
    # probably rug: только вычисления на уже собранных данных, без запросов в сеть
    # и только если DANGER вызван поведенческим жёстким правилом (не одиночным китом в тонком пуле)
    behavioral = any(not g.startswith("soft:") for g in sc["gates"])
    rug = None if limited else d.rug_projection(holders, sig, ops, base, facts["reserve"], sc["band"],
                                                snipers=a.RUG_SNIPERS, reserve_ok=reserve_ok, behavioral=behavioral)
    if rug:
        rug["level_usd"] = gt["price_usd"] * rug["level_factor"] if gt.get("price_usd") else None
    meta = {}
    if not gt.get("name"):
        meta = a.token_meta(token)
    header = _header(gt, meta, round((time.time() - ts[launch["block"]]) / 3600, 1))
    elapsed = round(time.time() - t0, 1)
    ev("done", reason, score=sc["score"], band=sc["band"], headline=sc["headline"], rug=rug)

    return {
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
        "rug": rug,
        "unread": unread, "use_funding": USE_FUNDING,
        "elapsed_s": elapsed, "rpc_requests": a.REQUESTS[0] - r0,
    }
