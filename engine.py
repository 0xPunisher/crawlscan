"""Scan conductor: walks the steps, calls the adapter and detectors, emit(event) at every step.
Hard budget of 25 seconds: wallets whose history was not read in time are marked unread,
the scan does not wait for them: we take only those who made it before the deadline, stragglers are not awaited.
Chain by address: 0x + 40 hex -> Robinhood (chains/robinhood.py), base58 -> Solana
(chains/solana.py, only with SOLANA_ENABLED=true). Both adapters return the same facts.
Running from code: engine.scan("0x...", emit=print) -> result (dict)."""
import math, os, re, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as _CFTimeout

from chains import bankr
from chains import flap
from chains import priority
from chains import robinhood as ch
from chains import solana as sol
import detect as d
import market

BUDGET = 25.0          # seconds for the whole scan (README)
DETECT_RESERVE = 1.0   # seconds kept for detect and events after reading wallets
MARKET_WAIT = 3.0      # wait for the GeckoTerminal header before the first RPC at most this many seconds from scan start
MARKET_LATE = 20.0     # not received in time -- the GT request continues in the background (up to this many seconds for all attempts),
                       # a late response is checked before the verdict
RESERVE_MIN_SEEN = 0.25  # reserve in $ < 25% of the token side of GT liquidity (liquidity / 2) -- pools not found
ESTABLISHED_ENV = {"min_age_days": "ESTABLISHED_MIN_AGE_DAYS", "min_liquidity_usd": "ESTABLISHED_MIN_LIQUIDITY_USD",
                   "min_mcap_usd": "ESTABLISHED_MIN_MCAP_USD"}
WORKERS = 12           # threads reading wallet history; the RPS limiter in the adapter is shared
# how many wallets to read at once (by descending share): an adapter with heavy requests under the shared limiter
# (Robinhood: a batch of 10 getLogs = 10 RPS slots) sets HISTORY_PARALLEL -- when the budget is short the largest
# go first; without the attribute -- all WORKERS at once, as before
SPIDERS = 6            # spiders; wallets are dealt round-robin
HIST_CAP = d.SHORT_HISTORY_MAX + 1  # count coins before entry up to 4: what matters is 0 / 1..3 / more
USE_FUNDING = False    # funding disabled: eth_inflows/outgoing_count are not called,
                       # the "common funder" link and the one_shot_funded signal are disabled

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CHAINS = {"robinhood": ch, "solana": sol}
NATIVE = {"robinhood": ("ETH", 10 ** 18), "solana": ("SOL", 10 ** 9)}   # unit of eth_in
NOT_LAUNCHPAD = {"robinhood": "not a Pons V2 token", "solana": "not a pump.fun token"}
NOT_LAUNCHPAD_FLAP = "not a Pons V2 or Flap token"   # Robinhood with FLAP_ENABLED
TOO_LARGE = "token history too large"                  # Pons: more than ch.SCAN_MAX_LOGS transfers
GT_NETWORK = {"robinhood": "robinhood", "solana": "solana"}


class ScanError(Exception):
    """User-facing scan error (bad address, not Pons V2 / pump.fun, Solana disabled)."""


PARTIAL_BUILDING = "Partial scan: building the full holder history, check again in {eta}"
PARTIAL_TOO_LARGE = "Partial scan: the holder history is too large to read in full, top holders are approximate"
PARTIAL_QUEUED = "Partial scan: full holder history queued: {pos} in line, check again in {eta}"
PARTIAL_QUEUE_FULL = "Partial scan: full history is queued, check again later"
PARTIAL_LATER = "Partial scan: the full holder history is not available right now, check again later"


def bankr_partial(token, hist):
    """Partial Bankr scan -> {"state", "eta_s", "message"[, "position"]} (state -- bankr.index_status: building |
    queued | queue_full | too_large | unavailable | none | ready). ready -- the index finished building during the scan: the next
    scan will be full."""
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
    """Error text "not from a launchpad" based on the chain's enabled launchpads (switches FLAP_ENABLED, BANKR_ENABLED)."""
    if chain != "robinhood":
        return NOT_LAUNCHPAD[chain]
    pads = ["Pons V2"] + (["Flap"] if flap.enabled() else []) + (["Bankr"] if bankr.enabled() else [])
    return f"not a {pads[0]} token" if len(pads) == 1 else \
        f"not a {', '.join(pads[:-1])} or {pads[-1]} token"


def solana_enabled():
    return os.environ.get("SOLANA_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def chain_of(token):
    """CA -> (chain, normalized address) or ScanError.
    0x + 40 hex -> robinhood (lowercase); base58 32 bytes -> solana (case preserved);
    Solana with SOLANA_ENABLED off -> "Solana support is coming soon"."""
    token = (token or "").strip() if isinstance(token, str) or token is None else ""
    if ADDR_RE.match(token):
        return "robinhood", token.lower()
    if sol.is_address(token):
        if not solana_enabled():
            raise ScanError("Solana support is coming soon")
        return "solana", token
    raise ScanError("not a token address")


def established_limits():
    """Thresholds for "too established" from env (ESTABLISHED_*); invalid or empty value -> default detect.ESTABLISHED."""
    out = {}
    for k, name in ESTABLISHED_ENV.items():
        try:
            v = float(os.environ.get(name) or "nan")
            out[k] = v if v == v and v >= 0 else d.ESTABLISHED[k]
        except ValueError:
            out[k] = d.ESTABLISHED[k]
    return out


def reserve_seen(reserve, supply, gt):
    """The reserve must agree with GT liquidity: R in $ (R / supply x FDV) is at least RESERVE_MIN_SEEN of the token
    side (liquidity / 2). Less -- not all pools were found (another DEX, outside top-20): the reserve is unreliable.
    No GT numbers -- nothing to check against, True."""
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
    """Result without a full scan: the token is old and large (detect.too_established). No score.
    rpc_requests > 0 -- verdict from a late GT response, the scan was already running. market_source -- where market data came from
    ("gt" | "dexscreener", market.fetch_market), market_pool -- pool for the chart widget."""
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
    """Result without a verdict: the Bankr token history does not fit in a regular scan and the background index is disabled
    (bankr.TooActive). Only what is certain without the full history: dev vesting, price, liquidity, market cap (GT), chart."""
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
    """CA -> normalized address or ScanError (see chain_of)."""
    return chain_of(token)[1]


class _RankGate:
    """Wallet reading window: the task of rank i starts when i < released + width (at most width at once,
    in rank order -- largest first). A slot is released when the task finishes or uses up its share
    of time slot_s (then it keeps reading in the background but does not hold the queue: a slow wallet does not block the next ones).
    close() -- budget exhausted: waiting tasks exit immediately without reading."""

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


_HIST, _HIST_LOCK = {}, threading.Lock()  # (wallet, before_block) -> coins before entry

def _history(a, wallet, before_block, token):
    key = (wallet, before_block)
    with _HIST_LOCK:
        if key in _HIST:
            return _HIST[key]
    n = a.wallet_distinct_tokens(wallet, before_block, token, window=None, cap=HIST_CAP)
    if n is not None:  # None -- the adapter did not fit in the read limit: read again next time
        with _HIST_LOCK:
            _HIST[key] = n
    return n


def _flags(s):
    return [f for f, on in (("unread", s["unread"]), ("virgin", s["virgin"]),
                            ("short_history", s["short_history"]), ("one_shot_funded", s["one_shot_funded"]),
                            ("sniper", s["sniper"]), ("deployer", s["is_deployer"]), ("sold", s["sold"])) if on]


def _reason(sc, base):
    """Main reason for the verdict in one line."""
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
    """Full token scan. emit(event) -- events in README format. Returns result (dict).
    ScanError -- user-facing error (address, not Pons V2 / pump.fun, Solana disabled); other exceptions -- failure.
    The chain adapter returns token facts in a common shape (token_facts): transfers, supply,
    excluded addresses, market, liquidity token reserve (reserve) and, if the chain knows
    balances directly (Solana), base."""
    t0 = time.time()
    chain, token = chain_of(token)
    a = CHAINS[chain]
    # background scan (alerts recheck) runs under its own BACKGROUND_RPS limit: the budget grows by the same factor
    # to read as many wallets as a live scan
    deadline = t0 + BUDGET * (priority.background_scale(a.RPS) if priority.is_background() else 1.0)
    r0 = a.REQUESTS[0]
    seq = [0]

    def ev(type_, detail="", wallet=None, spider=None, **extra):
        e = {"i": seq[0], "t": int((time.time() - t0) * 1000), "type": type_,
             "spider": spider, "wallet": wallet, "detail": detail, "chain": chain} | extra
        seq[0] += 1
        emit(e)

    # Market (GeckoTerminal; no answer -> DexScreener: market.fetch_market) -- before the first RPC: header and the
    # "too established" check. A cached too established verdict (market.ESTABLISHED_TTL) is returned right away, no requests.
    # No answer within MARKET_WAIT -- the scan proceeds, the request continues in the background; a late answer is checked between stages
    # and before the verdict. Market unavailable -- regular scan (old token -> limited).
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
        """A late GT answer has arrived and the token is too established -> result (scan stops here), otherwise None."""
        if not late or mkt_thread.is_alive() or not d.too_established(mkt_box, limits):
            return None
        print(f"gt: late answer for {token} at {time.time() - t0:.1f}s: too established", flush=True)
        market.established_put(token, network, mkt_box)
        return established_result(token, chain, dict(mkt_box), limits, ev, t0, a.REQUESTS[0] - r0)

    ev("stage", "launch")
    # Robinhood launchpad: Flap -- only with FLAP_ENABLED, suffix 8888/7777 (no RPC) and Portal confirmation
    # (one HTTP); Bankr -- only with BANKR_ENABLED, suffix ba3 (no RPC) and one Airlock eth_call;
    # the rest -- Pons the old way, no extra requests
    flap_state = flap.detect(token) if chain == "robinhood" else None
    bankr_state = bankr.detect(token) if chain == "robinhood" and flap_state is None else None
    eth_box, eth_thread = {}, None
    if bankr_state is not None:
        a = bankr
        launch = bankr.get_launch(token, bankr_state)
    elif flap_state is not None:
        a = flap
        if flap_state["status"] != flap.STATUS_DEX:   # curve: GT does not know it, price = Portal x ETH/USD (in parallel)
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
    # Flap: a large token history is read in windows within the scan budget (deadline counted from scan start)
    try:
        facts = a.token_facts(token, launch, deadline) if a in (flap, bankr) else a.token_facts(token, launch)
    except ch.HistoryTooLarge as e:
        print(f"scan: {token} history too large ({e} logs, cap {ch.SCAN_MAX_LOGS})", flush=True)
        raise ScanError(TOO_LARGE) from None
    except bankr.TooActive as e:   # Bankr, index disabled: history larger than a regular scan -- no verdict
        if est := late_established():
            return est
        bankr.mark_active(token)
        info = bankr.active_info(token, launch)
        if late:   # market needed for the header: wait for the late GT answer within the budget
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
    # entry not found (Solana: kind None) -- no entry time and no history read: unread, not virgin
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
    # alerts recheck pool -- also background (chains.priority: yields to live scans in the RPS limiter)
    ex = ThreadPoolExecutor(max_workers=WORKERS,
                            thread_name_prefix=f"{priority.BG}-pool" if priority.is_background() else "")
    order = [w for w in hs if entries[w]["kind"] is not None]   # hs -- by descending share: largest first
    width = getattr(a, "HISTORY_PARALLEL", WORKERS)
    left = max(0.05, deadline - DETECT_RESERVE - time.time())
    # time share per wallet: if each fits into it, all make it; whoever takes longer yields the slot to the next
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
    # token distributors shared by >= 2 holders: only check whether it is a contract
    hset, by_src = set(hs), {}
    for t in transfers:
        if t["to"] in hset and t["frm"] not in hset and t["frm"] not in excluded:
            by_src.setdefault(t["frm"], set()).add(t["to"])
    cand = {a for a, ws in by_src.items() if len(ws) > 1}
    contracts = {x for x, c in a.is_contract(cand).items() if c} if cand else set()
    # without funding we do not know outgoing ETH: a distributor wallet (not a contract) is not considered a hub
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

    # late GT answer (not received within MARKET_WAIT): if it arrived -- use it; too established -> that is the verdict
    if est := late_established():
        return est
    if late:
        if not mkt_thread.is_alive():
            gt = dict(mkt_box)
        print(f"gt: late answer for {token} at {time.time() - t0:.1f}s: "
              + ("ok" if gt else "still none" if mkt_thread.is_alive() else "failed"), flush=True)
    # fallback without GT: market unknown, but the token is older on-chain than the too established threshold --
    # holder signals are limited: no hard rules on impact and transfer, and no probably rug
    limited = not gt and time.time() - ts[launch["block"]] > limits["min_age_days"] * 86400
    # Flap: a large history was read in windows and the top is not guaranteed accurate -- holder signals are limited too
    hist = (facts.get("flap") or facts.get("bankr") or {}).get("history") or {}
    partial = hist.get("mode") == "windowed" and not hist.get("top_exact")
    limited_note = d.PARTIAL_NOTE if partial and not limited else d.LIMITED_NOTE
    limited = limited or partial
    # Bankr: large history, the full holder index is still building (or unavailable) -- partial scan:
    # verdict no better than RISKY, on the site and in the bot -- "Partial scan: ..." (Pons and Flap -- old path)
    partial_scan = bankr_partial(token, hist) if "bankr" in facts and partial else None
    # reserve is reliable: the adapter found a pool (on the curve -- always) and it agrees with GT liquidity
    reserve_ok = facts.get("reserve_ok", True) and reserve_seen(facts["reserve"], supply, gt)
    q_factor = facts.get("q_factor", 1)   # Flap after graduation: sell tax in tokens (1 - sellTax)
    # Bankr: impact from V4Quoter quotes (impact_curve), not from the reserve; non-redeemable dev vesting (locked) is
    # in the dev share but not in q (cannot be sold now); dev -- dev share from contracts (rule applies even when limited).
    # Pons and Flap: None -- old path
    curve, locked = facts.get("impact_curve"), facts.get("locked")
    sc = d.score(holders, sig, ops, base, facts["reserve"], gt.get("liquidity_usd"), reserve_ok=reserve_ok,
                 limited=limited, q_factor=q_factor, limited_note=limited_note, impact_curve=curve, locked=locked,
                 dev=facts.get("dev"), partial=partial_scan is not None)
    reason = _reason(sc, base)
    # probably rug: only computation on already collected data, no network requests
    # and only if DANGER was triggered by a behavioral hard rule (not a single whale in a thin pool)
    behavioral = any(not g.startswith("soft:") for g in sc["gates"])
    rug = None if limited else d.rug_projection(holders, sig, ops, base, facts["reserve"], sc["band"],
                                                snipers=a.RUG_SNIPERS, reserve_ok=reserve_ok, behavioral=behavioral,
                                                q_factor=q_factor, impact_curve=curve, locked=locked)
    # Flap on the curve: GeckoTerminal does not know it -- name and ticker from the contract, price = Portal x ETH/USD,
    # market cap = price x full supply, liquidity = ETH in the curve x ETH/USD
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
