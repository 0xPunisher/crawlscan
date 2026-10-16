"""rh-crawler detectors: pure functions with no network access, chain-agnostic.
Input: token transfers and per-wallet data collected beforehand by the adapter;
output: holders, signals, links, packs, operators, score and the "probably rug" projection. Rules: README.

All shares (share) are numbers 0..1 of the CIRCULATING supply (circulating = supply minus
everything held by infrastructure: curve, pool manager, locker, burn, ...).
Share of total supply: share_supply = share * circulating / supply. ETH amounts are wei (int).
"""
from collections import defaultdict
from statistics import median

# --- Holders (README "Holders") ---
TOP_N = 20                 # look at the top-20 holders
DUST_SHARE = 0.001         # dust: < 0.1% of circulating is ignored

# --- Signals (README "Per-wallet signals") ---
SHORT_HISTORY_MAX = 3      # short history: ≤ 3 distinct coins in the whole history before entry
SNIPER_SECONDS = 30        # sniper: entry within the first 30 s after launch
# launch_bundle: entry no later than bundle_window blocks after launch (chain parameter: Robinhood 0, Solana 2)

# --- Links and packs (README "Links") ---
HUB_MIN_OUT = 100          # hub: ≥ 100 outgoing ETH transfers, never link through it
PACK_MIN = 3               # pack: ≥ 3 wallets in one block
PACK_SPREAD = 0.30         # pack: each wallet's eth_in within ±30% of the group median
PACK_WEIGHT = 0.6          # pack weight in operator share (proven ×1.0)

# --- Score (README "Score") ---
MIN_HOLDERS = 10           # fewer than 10 holders (all non-zero, excluding infrastructure) → "too early or too late"
WEIGHTS = {                # weights of score parts, sum 100
    "operator": 35,        # price drop if the biggest operator (weighted) sells everything into liquidity
    "virgin": 25,          # share of virgin wallets in the top (excluding the deployer)
    "transfer": 20,        # share of supply received by transfer rather than bought
    "sniper": 10,          # sniper supply (first 30 s) not yet sold
    "concentration": 10,   # top-20 concentration
}
# Part scale: x ≤ good → full points, x ≥ bad → 0, linear in between.
# Upper bounds of three parts = README gates; other bounds are initial
# (not set in README), calibrated on real tokens.
SCALE = {
    "operator":      (0.10, 0.60),  # dump_impact: 35 points at ≤ 10% price drop, 0 at ≥ 60%
    "operator_share": (0.05, 0.25), # reserve not measured (reserve_ok=False): by weighted share of circulating, as in v1
    "virgin":        (0.10, 0.80),
    "transfer":      (0.01, 0.10),
    "sniper":        (0.01, 0.15),
    "concentration": (0.25, 0.70),
}
GATE_IMPACT = 0.50         # gate: selling the biggest operator would drop price by ≥ 50% — if it is 2+ linked
                           # wallets, or one suspicious wallet (deployer, virgin, history not read, entry
                           # by transfer or not found); an independent buyer with history (thin pool) — soft
                           # rule, band no better than RISKY
LIQ_MIN_USD = 1_000        # soft rule: liquidity from the header < $1,000 → band no better than RISKY
GATE_VIRGIN = 0.80         # gate: virgin ≥ 80% of the top ...
GATE_VIRGIN_MIN = 5        # ... at least 5 read holders (excluding the deployer)
UNREAD_MAX = 0.50          # more than half of the top (excluding the deployer) unread — virgin share unknown:
                           # virgin part gets a neutral half weight, virgin rule not applied, a note is added
GATE_TRANSFER = 0.10       # gate: received by transfer ≥ 10% of circulating
SOFT_GATE_SCORE = 59       # soft gate: a pack or a proven operator of ≥ 3 wallets
SOFT_GATE_WALLETS = 3      #   → band no better than RISKY (score = min(score, 59))
BANDS = ((80, "CLEAN"), (60, "OK"), (40, "RISKY"))  # otherwise DANGER
TOO_EARLY = "TOO_EARLY_OR_LATE"

# --- Too established: old and large token — no full scan (README "Too established") ---
TOO_ESTABLISHED = "TOO_ESTABLISHED"
ESTABLISHED = {"min_age_days": 30.0, "min_liquidity_usd": 750_000.0, "min_mcap_usd": 10_000_000.0}
ESTABLISHED_HEADLINE = "This token is too established for CrawlScan."
LIMITED_NOTE = " — market data unavailable, older token: holder signals are limited"
PARTIAL_SCORE = 59   # partial Bankr scan (score partial=True): score capped, band no better than RISKY
PARTIAL_NOTE = " — long history, top holders partially read: holder signals are limited"   # Flap: history read in windows
ESTABLISHED_TEXT = ("CrawlScan is built for fresh memecoins. On large, older tokens the top holders are mostly "
                    "exchanges and big liquidity pools: tokens reach exchange wallets by transfer, not by buying, "
                    "and liquidity is spread across many pools, so holder patterns don't mean what they mean "
                    "on a fresh launch.")

# --- Too active: Bankr token history does not fit in a regular scan and the background index is off — no verdict ---
TOO_ACTIVE = "TOO_ACTIVE"
ACTIVE_HEADLINE = "This token has too many trades for a full scan right now."
ACTIVE_TEXT = ("CrawlScan reads the whole transfer history to find who really holds a token. This one has more "
               "trades than a scan can read, so there is no verdict or score. The dev vesting, price, liquidity and "
               "market cap shown here are exact.")

# --- Probably rug (README «Probably rug») ---
RUG_MIN_DROP = 0.40        # the projection is shown if selling the suspicious stock would drop price by ≥ 40%
RUG_ORDER = ("linked", "transfer", "virgin", "bundle")  # reason priority: a wallet goes to the first matching one


def supply_base(transfers, supply, excluded):
    """{"supply", "circulating", "holders_total", "balances"}: circulating = sum of
    positive balances outside excluded (= supply minus infrastructure);
    holders_total — all holders with a non-zero balance excluding infrastructure (no dust cutoff)."""
    bal = defaultdict(int)
    for t in transfers:
        bal[t["frm"]] -= t["amount"]
        bal[t["to"]] += t["amount"]
    pos = {a: v for a, v in bal.items() if v > 0 and a not in excluded}
    return {"supply": supply, "circulating": sum(pos.values()), "holders_total": len(pos), "balances": pos}


def top_holders(base, n=TOP_N):
    """[(addr, balance, share)] by descending balance, share = share of circulating;
    without dust < DUST_SHARE of circulating. base — supply_base(...)."""
    circ = base["circulating"]
    rows = [(a, v, v / circ) for a, v in base["balances"].items() if circ and v / circ >= DUST_SHARE]
    rows.sort(key=lambda r: -r[1])
    return rows[:n]


def sellers(transfers, market):
    """Wallets that have sent the token to the market at least once (sold)."""
    return {t["frm"] for t in transfers if t["to"] in market and t["frm"] not in market}


def wallet_signals(data, launch_ts, deployer, launch_block=None, bundle_window=0):
    """Per-wallet signals. data — {wallet: {
        "kind": "buy" | "transfer" | None,  # type of first entry (adapter.classify_entries); None — not found
        "tx": str, "block": int,      # transaction and block of first entry
        "via": str,                   # who the token came from
        "eth_in": int | None,         # ETH spent on the buy, wei
        "entry_ts": int | None,       # unix time of the entry block; None — entry not found (Solana)
        "distinct_tokens": int | None,  # distinct coins in the whole history before entry (cap > 3); None — not read in time
        "inflows": [{"from", "block", "amount"}],  # incoming ETH before entry
        "sold": bool,                 # whether the token was sold (detect.sellers)
    }}
    Returns {wallet: {"kind", "virgin", "short_history", "one_shot_funded",
    "sniper", "launch_bundle", "eth_in", "is_deployer", "sold", "unread", "block", "tx", "via"}}.
    virgin — 0 coins before entry; short_history — 1..3 coins (virgin is not included).
    unread — history not read within budget: virgin and short_history = False.
    launch_bundle — entry in the launch block or no later than bundle_window blocks (slots) after;
    launch_block None or entry not found — False."""
    out = {}
    for w, d in data.items():
        n = d["distinct_tokens"]
        b = d["block"]
        out[w] = {
            "kind": d["kind"],
            "virgin": n == 0,
            "short_history": n is not None and 0 < n <= SHORT_HISTORY_MAX,
            "unread": n is None,
            "one_shot_funded": len(d["inflows"]) == 1,
            "sniper": d["entry_ts"] is not None and d["entry_ts"] - launch_ts <= SNIPER_SECONDS,
            "launch_bundle": launch_block is not None and b is not None and 0 <= b - launch_block <= bundle_window,
            "eth_in": d["eth_in"],
            "is_deployer": w == deployer,
            "sold": d.get("sold", False),
            "block": d["block"], "tx": d["tx"], "via": d["via"],
        }
    return out


def _star(group, kind, via):
    """Group links as a star from the first wallet: enough for connected components."""
    group = sorted(group)
    return [{"a": group[0], "b": b, "kind": kind, "level": "proven", "via": via} for b in group[1:]]


def find_links(holders, transfers, signals, inflows, outgoing, contracts, excluded, packs=()):
    """Links between holders: [{"a", "b", "kind", "level", "via"}].
    holders — holder addresses; signals — wallet_signals (tx needed);
    inflows — {wallet: [{"from", ...}]}; outgoing — {addr: outgoing ETH transfers}
    (for funders and distributors); contracts — addresses with code; excluded — infrastructure and market.
    Proven (level "proven"):
      same_tx     — first entry in the same transaction;
      distributor — received the token from the same regular wallet (not a contract, not a hub);
      direct      — direct token transfer between holders;
      funder      — common first-level funder: not a contract and < HUB_MIN_OUT outgoing.
    Unknown outgoing count of a distributor/funder is treated as a hub: no data, no linking.
    Packs (level "pack", kind "pack") — from packs (find_packs)."""
    hs = set(holders)
    bad = set(excluded) | set(contracts)
    ok = lambda a: a not in bad and outgoing.get(a, HUB_MIN_OUT) < HUB_MIN_OUT
    links, seen = [], set()

    def add(ls):
        for l in ls:
            key = (min(l["a"], l["b"]), max(l["a"], l["b"]), l["kind"])
            if l["a"] != l["b"] and key not in seen:
                seen.add(key); links.append(l)

    by_tx = defaultdict(set)
    for w in hs:
        by_tx[signals[w]["tx"]].add(w)
    for tx, g in by_tx.items():
        if tx and len(g) > 1:  # tx None — entry not found, not a shared transaction
            add(_star(g, "same_tx", tx))

    by_src = defaultdict(set)
    for t in transfers:
        if t["to"] in hs and t["frm"] not in hs and ok(t["frm"]):
            by_src[t["frm"]].add(t["to"])
        if t["to"] in hs and t["frm"] in hs:
            add([{"a": t["frm"], "b": t["to"], "kind": "direct", "level": "proven", "via": t["tx"]}])
    for src, g in by_src.items():
        if len(g) > 1:
            add(_star(g, "distributor", src))

    by_funder = defaultdict(set)
    for w in hs:
        for f in inflows.get(w, []):
            if ok(f["from"]):
                by_funder[f["from"]].add(w)
    for f, g in by_funder.items():
        if len(g) > 1:
            add(_star(g, "funder", f))

    for p in packs:
        add([dict(l, level="pack") for l in _star(p["wallets"], "pack", f"block {p['block']}")])
    return links


def find_packs(signals, window=0):
    """Behavioral packs: [{"block", "wallets", "median_eth"}].
    ≥ PACK_MIN wallets bought in one block window, each virgin, each wallet's eth_in
    within ±PACK_SPREAD of the group median. The deployer is never in a pack.
    window — chain parameter: 0 = same block (Robinhood), 2 = up to 2 slots after the first
    buyer of the group (Solana); pack block = block of the first buyer."""
    cand = sorted((s["block"], w) for w, s in signals.items()
                  if s["kind"] == "buy" and s["virgin"] and s["eth_in"] and not s["is_deployer"])
    by_block = {}
    start = None
    for b, w in cand:
        if start is None or b - start > window:
            start = b
        by_block.setdefault(start, []).append(w)
    packs = []
    for b, ws in sorted(by_block.items()):
        if len(ws) < PACK_MIN:
            continue
        med = median(signals[w]["eth_in"] for w in ws)
        g = sorted(w for w in ws if abs(signals[w]["eth_in"] - med) <= PACK_SPREAD * med)
        if len(g) >= PACK_MIN:
            packs.append({"block": b, "wallets": g, "median_eth": med})
    return packs


def operators(holders, links, packs=(), supply_ratio=1.0):
    """Operators = connected components (union-find) over links and packs.
    holders — [(addr, balance, share)]; supply_ratio = circulating / supply.
    Returns, by descending weighted share:
    [{"wallets", "share", "share_supply", "level", "weighted"}] (shares of circulating); level "proven" if there is
    a proven link inside, "pack" — pack only, None — single wallet (weight 1.0)."""
    share = {a: s for a, _, s in holders}
    parent = {a: a for a in share}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    def union(a, b):
        if a in parent and b in parent:
            parent[find(a)] = find(b)

    for l in links:
        union(l["a"], l["b"])
    for p in packs:
        for w in p["wallets"][1:]:
            union(p["wallets"][0], w)

    groups = defaultdict(list)
    for a in share:
        groups[find(a)].append(a)
    proven = {find(l["a"]) for l in links if l["level"] == "proven" and l["a"] in parent and l["b"] in parent}

    out = []
    for r, ws in groups.items():
        lvl = None if len(ws) == 1 else "proven" if r in proven else "pack"
        s = sum(share[w] for w in ws)
        out.append({"wallets": sorted(ws, key=lambda w: -share[w]), "share": s,
                    "share_supply": s * supply_ratio, "level": lvl,
                    "weighted": s * (PACK_WEIGHT if lvl == "pack" else 1.0)})
    out.sort(key=lambda o: -o["weighted"])
    return out


def dump_impact(q, reserve):
    """How much (0..1) the price drops if q tokens are sold into x*y=k liquidity with a token
    reserve (pool after migration or bonding curve before): 1 - (R / (R + q))^2.
    No reserve — nowhere to sell: 1.0 (if there is anything to sell)."""
    if q <= 0:
        return 0.0
    if not reserve or reserve <= 0:
        return 1.0
    return 1.0 - (reserve / (reserve + q)) ** 2


def curve_impact(q, curve):
    """Price drop after selling q along a quote curve [(q_i, drop_i)] (ascending q; Bankr — V4Quoter):
    linear between points; below the first — proportional from zero; above the last — same as the last."""
    if q <= 0:
        return 0.0
    if q <= curve[0][0]:
        return curve[0][1] * q / curve[0][0]
    for (q0, i0), (q1, i1) in zip(curve, curve[1:]):
        if q <= q1:
            return i0 + (i1 - i0) * (q - q0) / (q1 - q0)
    return curve[-1][1]


def _impact(q, reserve, curve=None):
    """dump_impact from the x*y=k reserve or, if there is a quote curve (curve not empty), from it."""
    return curve_impact(q, curve) if curve else dump_impact(q, reserve)


def _unlocked(q, wallets, locked, k=1.0):
    """q minus what is not yet unlocked for these wallets (dev vesting: counted in share, cannot be sold now), × k."""
    return max(0, q - k * sum(locked.get(w, 0) for w in wallets)) if locked else q


def _part(name, x, scale=None):
    good, bad = SCALE[scale or name]
    k = 1.0 if x <= good else 0.0 if x >= bad else (bad - x) / (bad - good)
    return round(WEIGHTS[name] * k, 1)


def too_established(market, limits=ESTABLISHED):
    """Old and large token per GeckoTerminal: age ≥ min_age_days AND liquidity of all pools
    ≥ min_liquidity_usd AND market cap ≥ min_mcap_usd. Any number missing — False (regular scan)."""
    age, liq, mcap = (market or {}).get("age_days"), (market or {}).get("liquidity_usd"), (market or {}).get("mcap_usd")
    if age is None or liq is None or mcap is None:
        return False
    return age >= limits["min_age_days"] and liq >= limits["min_liquidity_usd"] and mcap >= limits["min_mcap_usd"]


def _impact_phrase(impact, ops):
    """Price phrase for the headline: < 1% → "<1%"; no operators of 2+ wallets and < 1% — not shown;
    reserve not measured (None) — "liquidity not measured"."""
    if impact is None:
        return ", liquidity not measured"
    if impact < 0.01:
        return ", could move price <1% if sold" if any(len(o["wallets"]) > 1 for o in ops) else ""
    return f", could move price −{impact * 100:.0f}% if sold"


def _sold_into_pool(q, q_factor):
    """How much of q tokens reaches liquidity: q × q_factor (sell tax taken in tokens, Flap after graduation).
    q_factor = 1 — q unchanged (same int: Pons result bit-for-bit as before)."""
    return q if q_factor == 1 else q * q_factor


def score(holders, signals, ops, base, reserve, liquidity_usd=None, reserve_ok=True, limited=False, q_factor=1,
          limited_note=LIMITED_NOTE, impact_curve=None, locked=None, dev=None, partial=False):
    """Score 0–100 (100 = clean): {"score", "band", "parts", "gates", "metrics", "headline"}.
    reserve — liquidity token reserve (raw units): pool balance after migration or curve before.
    The "operator" part and its gate come from dump_impact of the biggest operator (q = weighted share of
    circulating × circulating), metrics["impact"]; metrics["operator"] — its weighted share of circulating (informational).
    Other metrics are shares of circulating. parts — points for the five README parts.
    liquidity_usd — liquidity from the token header; < LIQ_MIN_USD → soft rule (band no better than RISKY),
    None (unknown) — rule not applied.
    reserve_ok=False — reserve could not be determined reliably: impact = None, the impact gate is not
    applied, the "operator" part uses weighted share of circulating (SCALE["operator_share"]).
    gates — triggered gates: hard (→ DANGER) and soft with the "soft:" prefix
    (a pack or a proven operator of ≥ 3 wallets → score at most 59, band no better than RISKY).
    limited=True — no market data (GT did not respond) and the token is old (holder signals are limited):
    hard rules on impact and transfer are not applied, band not below RISKY, LIMITED_NOTE is appended to headline.
    Fewer than MIN_HOLDERS holders (base["holders_total"]) → score None, band TOO_EARLY_OR_LATE.
    q_factor — fraction of a sale that reaches liquidity (1 − sell tax, if the tax is taken in tokens).
    limited_note — explanation of limited in headline (default LIMITED_NOTE; Flap with an incomplete top — PARTIAL_NOTE).
    impact_curve — quote curve [(q, drop)] (Bankr: V4Quoter): impact from it rather than the reserve; None — from reserve.
    locked — {wallet: tokens} that are in the wallet's share but cannot be sold now (Bankr dev vesting): subtracted from q
    for impact (with the operator's weight); None — q unchanged.
    dev — dev share known exactly from contracts (Bankr: deployer wallet via balanceOf + vesting):
    {"wallet", "sellable" (what can be sold now: wallet + available in vesting), "share_supply" (wallet + all
    vesting)}. If selling it would drop price by ≥ GATE_IMPACT — hard rule, including under limited (the numbers do not
    depend on an incomplete top); metrics["dev_impact"]. None — no rule (Pons, Flap: previous path).
    partial=True — partial scan (Bankr: long history, full holder index still building): verdict no better than
    RISKY and score at most PARTIAL_SCORE (no CLEAN/OK on an incomplete top). False — previous path."""
    n = len(holders)
    big = ops[0] if ops else {"share": 0.0, "share_supply": 0.0, "weighted": 0.0, "wallets": []}
    q = _unlocked(big["weighted"] * base["circulating"], big["wallets"], locked,
                  big["weighted"] / big["share"] if locked and big["share"] else 1.0)
    impact = _impact(_sold_into_pool(q, q_factor), reserve, impact_curve) if reserve_ok else None
    headline = (f"{n} wallets → {len(ops)} operators, biggest holds "
                f"{big['share'] * 100:.1f}% of float ({big['share_supply'] * 100:.1f}% of supply)"
                + _impact_phrase(impact, ops) + (limited_note if limited else ""))
    if base["holders_total"] < MIN_HOLDERS:
        return {"score": None, "band": TOO_EARLY, "parts": {}, "gates": [], "metrics": {}, "headline": headline,
                "notes": []}

    share = {a: s for a, _, s in holders}
    non_dev = [a for a in share if not signals[a]["is_deployer"]]
    # virgin — only among read wallets: an unread wallet is neither evidence nor "clean"
    read = [a for a in non_dev if not signals[a]["unread"]]
    unread_share = 1 - len(read) / len(non_dev) if non_dev else 0.0
    virgin_known = unread_share <= UNREAD_MAX
    virgins = [a for a in read if signals[a]["virgin"]]
    notes = [] if virgin_known else [f"virgin: {len(non_dev) - len(read)} of {len(non_dev)} top wallets unread — "
                                     f"scored neutral"]
    m = {
        "operator": big["weighted"],
        "impact": impact,
        "virgin": (len(virgins) / len(read) if read else 0.0) if virgin_known else None,
        "unread": unread_share,
        "transfer": sum(s for a, s in share.items() if signals[a]["kind"] == "transfer"),
        "sniper": sum(share[a] for a in non_dev if signals[a]["sniper"]),
        "concentration": sum(share.values()),
    }
    parts = {k: _part(k, m[k]) for k in WEIGHTS if k not in ("operator", "virgin")}
    parts["virgin"] = _part("virgin", m["virgin"]) if virgin_known else WEIGHTS["virgin"] / 2   # unknown — neutral
    parts["operator"] = (_part("operator", impact) if impact is not None
                         else _part("operator", m["operator"], "operator_share"))
    parts = {k: parts[k] for k in WEIGHTS}
    gates = []
    thin = impact is not None and impact >= GATE_IMPACT and not limited
    # single wallet: hard only if there is evidence (deployer, entry by transfer, read with no history — virgin);
    # an unread wallet or entry not found is not evidence: soft rule, same as an independent buyer
    lone = big["wallets"][0] if len(big["wallets"]) == 1 else None
    s0 = signals.get(lone) if lone else None
    suspect = bool(s0) and (s0["is_deployer"] or s0["kind"] == "transfer" or s0["virgin"])
    unknown = bool(s0) and not suspect and (s0["unread"] or s0["kind"] is None)
    independent = bool(s0) and not suspect and not unknown
    if thin and not (independent or unknown):
        gates.append(f"biggest operator could move price −{m['impact'] * 100:.0f}% if sold "
                     f"(≥ {GATE_IMPACT * 100:.0f}%; {m['operator'] * 100:.1f}% of float)")
    if virgin_known and len(read) >= GATE_VIRGIN_MIN and m["virgin"] >= GATE_VIRGIN:
        gates.append(f"{m['virgin'] * 100:.0f}% virgin wallets in top (≥ {GATE_VIRGIN * 100:.0f}%)")
    if m["transfer"] >= GATE_TRANSFER and not limited:
        gates.append(f"{m['transfer'] * 100:.1f}% of float received by transfer (≥ {GATE_TRANSFER * 100:.0f}%)")
    dev_gate = False
    if dev is not None and reserve_ok:
        m["dev_impact"] = _impact(_sold_into_pool(dev["sellable"], q_factor), reserve, impact_curve)
        same = gates and gates[0].startswith("biggest operator") and dev["wallet"] in big["wallets"]
        if m["dev_impact"] >= GATE_IMPACT and not same:
            gates.append(f"deployer could move price −{m['dev_impact'] * 100:.0f}% if sold "
                         f"(≥ {GATE_IMPACT * 100:.0f}%; {dev['share_supply'] * 100:.1f}% of supply with vesting)")
            dev_gate = True
    hard = bool(gates)
    total = round(sum(parts.values()))
    if thin and independent:   # one independent buyer with history in a thin pool: not DANGER on its own
        gates.append(f"soft: one holder could move price −{m['impact'] * 100:.0f}% (thin liquidity)")
    if thin and unknown:       # history not read (budget) or entry not found — not evidence
        gates.append(f"soft: biggest holder couldn't be fully read; could move price −{m['impact'] * 100:.0f}% "
                     f"(thin liquidity)")
    groups = [o for o in ops if o["level"] == "pack"
              or (o["level"] == "proven" and len(o["wallets"]) >= SOFT_GATE_WALLETS)]
    for o in groups:
        gates.append(f"soft: {'pack of' if o['level'] == 'pack' else 'proven operator of'} "
                     f"{len(o['wallets'])} wallets, {o['share'] * 100:.1f}% of float")
    if liquidity_usd is not None and liquidity_usd < LIQ_MIN_USD:
        gates.append(f"soft: liquidity too thin (${liquidity_usd:,.0f})")
    if any(g.startswith("soft:") for g in gates):
        total = min(total, SOFT_GATE_SCORE)
    band = "DANGER" if hard else next((b for lim, b in BANDS if total >= lim), "DANGER")
    if limited and band == "DANGER" and not dev_gate:
        band = "RISKY"   # old token without market data: holder signals are limited, verdict not below RISKY
                         # (except the dev rule: its share is known exactly from contracts)
    if partial and band in ("CLEAN", "OK"):
        band, total = "RISKY", min(total, PARTIAL_SCORE)   # incomplete top: we do not claim "clean"
    return {"score": total, "band": band, "parts": parts, "gates": gates, "metrics": m, "headline": headline,
            "notes": notes}


def rug_projection(holders, signals, ops, base, reserve, band, snipers=True, reserve_ok=True, behavioral=True,
                   q_factor=1, impact_curve=None, locked=None):
    """The "probably rug" projection: where the price drops if the whole suspicious stock is sold into liquidity.
    Only for band DANGER (does not change score or verdict); otherwise, with no stock or with a drop
    < RUG_MIN_DROP — None.
    Stock (each wallet once) — top wallets:
      linked   — in an operator of 2+ wallets (proven links and packs);
      transfer — first entry by transfer;
      virgin   — 0 coins before entry (unread is excluded: we do not guess history);
      bundle   — never sold and launch_bundle, and with snipers=True also a sniper (≤ SNIPER_SECONDS).
    A wallet goes to the first matching reason in RUG_ORDER, so the parts shares
    sum to share. drop = dump_impact(q, reserve), q — all stock tokens, unweighted.
    {"drop", "level_factor" (= 1 − drop), "share", "share_supply", "parts": [{"kind", "wallets",
    "share", "share_supply"}], "wallets"}; shares are of circulating, share_supply of supply.
    reserve_ok=False (reserve not measured reliably) — None: no drop computed without liquidity.
    behavioral — DANGER caused by a behavioral hard rule (linked operator, virgin, transfer); if not — None:
    a lone whale in a thin pool or a low score without signals is not a rug. Stock parts are behavioral only
    (linked, transfer, virgin, bundle/snipers): a whale without signals is not part of the stock.
    q_factor — as in score: q × q_factor reaches liquidity. impact_curve and locked — as in score."""
    if band != "DANGER" or not reserve_ok or not behavioral:
        return None
    linked = {w for o in ops if len(o["wallets"]) > 1 for w in o["wallets"]}
    test = {
        "linked": lambda w, s: w in linked,
        "transfer": lambda w, s: s["kind"] == "transfer",
        "virgin": lambda w, s: s["virgin"],
        "bundle": lambda w, s: not s["sold"] and (s["launch_bundle"] or (snipers and s["sniper"])),
    }
    ratio = base["circulating"] / base["supply"] if base["supply"] else 0.0
    parts, taken, q = [], set(), 0
    for kind in RUG_ORDER:
        rows = [(w, v, sh) for w, v, sh in holders if w not in taken and test[kind](w, signals[w])]
        if not rows:
            continue
        sh = sum(r[2] for r in rows)
        parts.append({"kind": kind, "wallets": [r[0] for r in rows], "share": sh, "share_supply": sh * ratio})
        taken.update(r[0] for r in rows)
        q += sum(r[1] for r in rows)
    if not taken:
        return None
    drop = _impact(_sold_into_pool(_unlocked(q, taken, locked), q_factor), reserve, impact_curve)
    if drop < RUG_MIN_DROP:
        return None
    share = sum(p["share"] for p in parts)
    return {"drop": drop, "level_factor": 1.0 - drop, "share": share, "share_supply": share * ratio,
            "parts": parts, "wallets": [w for w, _, _ in holders if w in taken]}


# ---------------------------------------------------------------------------
# early buyers: status of the first buyers and summary (data from the adapter: early_buyers / early_status)
# ---------------------------------------------------------------------------
EARLY_EXIT_DUST = 0.01  # less than 1% of the bought amount left — fully exited
EARLY_MOVED_MIN = 0.20  # status moved only if at least this much of the bought amount went to other wallets


def early_status(bought, now, sold=0, moved=0, burned=0, locked=0):
    """holding_all | added | sold_part | sold_all | moved | burned | locked.
    locked — tokens in a known locker (Sablier etc.): not an exit. Locked at least EARLY_MOVED_MIN of the bought amount
    and at least the sold, transferred and burned amounts — locked; otherwise locked tokens count as in the wallet.
    A transfer below EARLY_MOVED_MIN of the bought amount is minor: status by the rest (bought minus transferred),
    the transfer itself shows only as a share (moved_share_supply). Exited (≤ EARLY_EXIT_DUST left) or partially exited —
    by whichever is larger: sold, transferred, burned (tie — sale)."""
    if locked and locked >= bought * EARLY_MOVED_MIN and locked >= max(sold, moved, burned):
        return "locked"
    now += locked
    big_move = moved >= bought * EARLY_MOVED_MIN
    base = bought if big_move else bought - moved
    if now > base:
        return "added"
    if now == base:
        return "holding_all"
    if big_move and moved > sold and moved >= burned:
        return "moved"
    if burned > sold:
        return "burned"
    return "sold_all" if now <= base * EARLY_EXIT_DUST else "sold_part"


def early_report(data, status, flags=None):
    """{"buyers": [...], "summary": {...}} from the adapter's early_buyers (data) and early_status (status).
    flags — {wallet: [scan flags]} for wallets the scan checked (top-20); others get scan_flags = None.
    Shares are of supply. dt_s — seconds since launch, block_offset — blocks (Solana: slots) since launch.
    summary.same_destination — addresses where 2+ buyers sent tokens (sign of a single owner).
    Locked in a locker (locked, locked_in — Robinhood adapters) is not an exit: such a buyer is holding,
    summary.held_share_supply = in wallets + in lockers (now_share_supply — in wallets only)."""
    launch, supply = data["launch"], status["supply"] or 1
    rows, dest = [], {}
    for i, b in enumerate(data["buyers"], 1):
        w = b["wallet"]
        st = status["wallets"].get(w) or {"now": 0, "sold": 0, "moved": {}, "burned": 0, "partial": True}
        moved = sum(st["moved"].values())
        for to in st["moved"]:
            dest.setdefault(to, []).append(w)
        rows.append({
            "rank": i, "wallet": w, "dev": w == launch.get("deployer"),
            "dt_s": b["ts"] - launch["ts"] if b.get("ts") is not None and launch.get("ts") is not None else None,
            "block_offset": b["block"] - launch["block"],
            "bought_share_supply": b["bought"] / supply, "now_share_supply": st["now"] / supply,
            "status": early_status(b["bought"], st["now"], st["sold"], moved, st["burned"], st.get("locked", 0)),
            "sold_share_supply": st["sold"] / supply, "moved_share_supply": moved / supply,
            "burned_share_supply": st["burned"] / supply,
            "locked_share_supply": st.get("locked", 0) / supply, "locked_in": list(st.get("locked_in") or []),
            "moved_to": sorted(st["moved"], key=lambda a: -st["moved"][a]),
            "scan_flags": flags.get(w) if flags is not None and w in flags else None,
            "partial": st["partial"]})
    keep = lambda w: (status["wallets"].get(w) or {}).get("now", 0) + (status["wallets"].get(w) or {}).get("locked", 0)
    held = [r for r, b in zip(rows, data["buyers"]) if keep(r["wallet"]) > b["bought"] * EARLY_EXIT_DUST]
    return {"buyers": rows, "summary": {
        "buyers": len(rows), "exited": len(rows) - len(held), "holding": len(held),
        "now_share_supply": sum(r["now_share_supply"] for r in rows),
        "held_share_supply": sum(r["now_share_supply"] + r["locked_share_supply"] for r in rows),
        "bought_share_supply": sum(r["bought_share_supply"] for r in rows),
        "same_destination": [{"to": to, "wallets": ws} for to, ws in dest.items() if len(ws) >= 2],
        "partial": any(r["partial"] for r in rows)}}
