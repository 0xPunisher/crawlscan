"""Дирижёр скана: идёт по шагам, вызывает адаптер и детекторы, на каждом шаге — emit(event).
Жёсткий бюджет 25 секунд: кошельки, чью историю не успели прочитать, помечаются unread,
скан их не ждёт: забираем только тех, кто успел до дедлайна, зависших не ждём.
Сеть — по адресу: 0x + 40 hex → Robinhood (chains/robinhood.py), base58 → Solana
(chains/solana.py, только при SOLANA_ENABLED=true). Оба адаптера отдают одинаковые факты.
Запуск из кода: engine.scan("0x...", emit=print) -> result (dict)."""
import os, re, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _CFTimeout

from chains import robinhood as ch
from chains import solana as sol
import detect as d
import market

BUDGET = 25.0          # секунд на весь скан (README)
DETECT_RESERVE = 1.0   # секунд оставляем на detect и события после чтения кошельков
MARKET_WAIT = 3.0      # шапку GeckoTerminal ждём не дольше стольких секунд от старта скана
WORKERS = 12           # потоков чтения истории кошельков; лимитер RPS в адаптере общий
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


def validate(token):
    """CA → нормализованный адрес или ScanError (см. chain_of)."""
    return chain_of(token)[1]


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
    return {"operator": f"biggest operator could move price −{m['impact'] * 100:.0f}% if sold "
                        f"({m['operator'] * 100:.1f}% of float)",
            "virgin": f"{m['virgin'] * 100:.0f}% virgin wallets in top",
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
    deadline = t0 + BUDGET
    chain, token = chain_of(token)
    a = CHAINS[chain]
    r0 = a.REQUESTS[0]
    seq = [0]

    def ev(type_, detail="", wallet=None, spider=None, **extra):
        e = {"i": seq[0], "t": int((time.time() - t0) * 1000), "type": type_,
             "spider": spider, "wallet": wallet, "detail": detail, "chain": chain} | extra
        seq[0] += 1
        emit(e)

    mkt_box = {}
    mkt_thread = threading.Thread(target=lambda: mkt_box.update(market.fetch_market(token, GT_NETWORK[chain])),
                                  daemon=True)
    mkt_thread.start()

    ev("stage", "launch")
    launch = a.get_launch(token)
    if launch is None:
        raise ScanError(NOT_LAUNCHPAD[chain])

    ev("stage", "transfers")
    facts = a.token_facts(token, launch)
    transfers, supply, excluded, mkt = facts["transfers"], facts["supply"], facts["excluded"], facts["market"]
    base = facts["base"] or d.supply_base(transfers, supply, excluded)
    holders = d.top_holders(base)
    hs = [w for w, _, _ in holders]
    ratio = base["circulating"] / supply if supply else 0.0

    ev("stage", "entries")
    entries = a.classify_entries(token, transfers, hs)
    ts = a.block_timestamps([launch["block"]] + [e["block"] for e in entries.values() if e["block"] is not None])
    sold = d.sellers(transfers, mkt)
    # вход не найден (Solana: kind None) — без времени входа и без чтения истории: unread, не девственный
    data = {w: entries[w] | {"entry_ts": ts.get(entries[w]["block"]), "inflows": [], "sold": w in sold,
                             "distinct_tokens": None} for w in hs}
    unit, scale = NATIVE[chain]

    ev("stage", "wallets")
    spider = {w: i % SPIDERS for i, w in enumerate(hs)}
    share = {a: s for a, _, s in holders}
    for w in hs:
        ev("spider_move", "to wallet", wallet=w, spider=spider[w])
    ex = ThreadPoolExecutor(max_workers=WORKERS)
    futs = {ex.submit(_history, a, w, entries[w]["block"], token): w for w in hs if entries[w]["kind"] is not None}

    def flag(w):
        s = d.wallet_signals({w: data[w]}, ts[launch["block"]], launch["deployer"])[w]
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
    ex.shutdown(wait=False, cancel_futures=True)
    unread = [w for w in hs if w not in done]
    for w in unread:
        flag(w)

    ev("stage", "links")
    sig = d.wallet_signals(data, ts[launch["block"]], launch["deployer"])
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

    # ликвидность из шапки нужна скору; не пришла за MARKET_WAIT — скор без правила ликвидности
    mkt_thread.join(timeout=max(0.0, t0 + MARKET_WAIT - time.time()))
    gt = dict(mkt_box)   # снимок: поздний ответ GT не меняет уже посчитанный результат
    sc = d.score(holders, sig, ops, base, facts["reserve"], gt.get("liquidity_usd"))
    reason = _reason(sc, base)
    meta = {}
    if not gt.get("name"):
        meta = a.token_meta(token)
    header = {"name": gt.get("name") or meta.get("name"), "ticker": gt.get("ticker") or meta.get("symbol"),
              "price_usd": gt.get("price_usd"), "mcap_usd": gt.get("mcap_usd"),
              "liquidity_usd": gt.get("liquidity_usd"), "vol24h_usd": gt.get("vol24h_usd"),
              "age_h": round((time.time() - ts[launch["block"]]) / 3600, 1)}
    elapsed = round(time.time() - t0, 1)
    ev("done", reason, score=sc["score"], band=sc["band"], headline=sc["headline"])

    return {
        "token": token, "chain": chain, "header": header,
        "launch": launch | {"ts": ts[launch["block"]]},
        "supply": supply, "circulating": base["circulating"], "holders_total": base["holders_total"],
        "holders": [{"wallet": a, "share": s, "share_supply": s * ratio, "spider": spider[a],
                     "signals": sig[a]} for a, _, s in holders],
        "links": links, "packs": packs, "operators": ops,
        "score": sc["score"], "band": sc["band"], "parts": sc["parts"], "gates": sc["gates"],
        "metrics": sc["metrics"], "headline": sc["headline"], "reason": reason, "reserve": facts["reserve"],
        "unread": unread, "use_funding": USE_FUNDING,
        "elapsed_s": elapsed, "rpc_requests": a.REQUESTS[0] - r0,
    }
