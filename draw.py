"""Holder draw: pure functions with no network, like detect.py.

Network and DB live in draw_service.py and draw_store.py. Rules: DEV_NOTES.md, section "Draw".
Verifiable selection (canonical, list_hash, pick, verify) and draw timing do not depend on the chain:
the Robinhood token draw (rewards.py) uses them too.

Units: balances and weights are integer base units of the token (raw, decimals applied).
Draw day `day` (YYYY-MM-DD) is the UTC day over which weights are computed; the draw itself runs
the next day at 00:05 UTC, seed is the first slot with blockTime >= 00:01 UTC of the next day.
"""
import hashlib, json, math
from datetime import datetime, timedelta, timezone

SYSTEM_PROGRAM = "11111111111111111111111111111111"
DRAW_AT = (0, 5)      # draw at 00:05 UTC of the next day
SEED_AT = (0, 1)      # seed slot: first slot with blockTime >= 00:01 UTC of the next day


# ---------- time ----------
def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def hour_key(ts):
    """Hourly snapshot key: 'YYYY-MM-DDTHH' (UTC)."""
    return utc(ts).strftime("%Y-%m-%dT%H")


def day_key(ts):
    return utc(ts).strftime("%Y-%m-%d")


def _next_day(day, hm):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
    return int(d.replace(hour=hm[0], minute=hm[1]).timestamp())


def draw_time(day):
    """Unix time of the draw for day: 00:05 UTC of the next day."""
    return _next_day(day, DRAW_AT)


def seed_time(day):
    """Unix time of the seed: 00:01 UTC of the next day (blockhash of the first slot not earlier)."""
    return _next_day(day, SEED_AT)


def is_day(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d") == s
    except (TypeError, ValueError):
        return False


# ---------- snapshot ----------
def aggregate(token_accounts):
    """Token accounts [{owner, amount}] -> {owner: total balance}, non-zero only."""
    out = {}
    for a in token_accounts:
        amt = int(a["amount"])
        if amt > 0:
            out[a["owner"]] = out.get(a["owner"], 0) + amt
    return out


def is_wallet(owner_program, on_curve):
    """Regular wallet: a point on the ed25519 curve (not a PDA) and the owner's account belongs to
    System Program. No account (wallet without SOL, owner_program None) is also a wallet if on the curve.
    Pools, curves, PDAs and contracts (programs and their accounts) are excluded."""
    return bool(on_curve) and owner_program in (SYSTEM_PROGRAM, None)


def eligible_balances(balances, wallets):
    """Balances of regular wallets only. wallets is the set of owners recognized as wallets."""
    return {o: v for o, v in balances.items() if o in wallets}


# ---------- weights and threshold ----------
def day_weights(snapshots):
    """snapshots: wallet balances for the day's taken snapshots [{owner: balance}, ...].
    Weight = average balance over taken snapshots: wallet missing from a snapshot counts 0 for it;
    snapshots missed due to downtime are not in the denominator. Integer (floor), zero weights dropped."""
    n = len(snapshots)
    if not n:
        return {}
    total = {}
    for snap in snapshots:
        for o, v in snap.items():
            total[o] = total.get(o, 0) + int(v)
    return {o: s // n for o, s in total.items() if s // n > 0}


def threshold(decimals, price_usd=None, min_usd=10.0, min_tokens=None):
    """Average-balance threshold in base units: {"mode", "min_raw", "price_usd", "min_usd", "min_tokens"}.
    Price known: min_usd at that price (rounded up to a whole unit); no price: min_tokens; neither: no threshold."""
    scale = 10 ** int(decimals)
    if price_usd and price_usd > 0 and min_usd is not None:
        raw = math.ceil(min_usd / price_usd * scale)
        return {"mode": "usd", "min_raw": raw, "price_usd": price_usd, "min_usd": min_usd, "min_tokens": None}
    if min_tokens is not None:
        return {"mode": "tokens", "min_raw": math.ceil(float(min_tokens) * scale), "price_usd": None,
                "min_usd": None, "min_tokens": min_tokens}
    return {"mode": "none", "min_raw": 0, "price_usd": None, "min_usd": None, "min_tokens": None}


def apply_threshold(weights, min_raw):
    return {o: w for o, w in weights.items() if w >= min_raw and w > 0}


# ---------- verifiable selection ----------
def canonical(participants):
    """Canonical JSON of the participant list: [[address, weight], ...], sorted by address, no spaces."""
    rows = sorted([str(a), int(w)] for a, w in dict(participants).items())
    return json.dumps(rows, separators=(",", ":"))


def list_hash(participants):
    return hashlib.sha256(canonical(participants).encode()).hexdigest()


def pick(participants, blockhash):
    """Winner: r = int(sha256(blockhash + list_hash), 16) mod total_weight (strings, UTF-8);
    walk participants in address sort order; the winner is the first whose cumulative
    weight > r. No participants: winner None.
    -> {"list_hash", "total", "r", "winner", "winner_weight", "participants"}"""
    lh = list_hash(participants)
    rows = sorted((str(a), int(w)) for a, w in dict(participants).items())
    total = sum(w for _, w in rows)
    if not rows or total <= 0:
        return {"list_hash": lh, "total": 0, "r": None, "winner": None, "winner_weight": None, "participants": 0}
    r = int(hashlib.sha256((blockhash + lh).encode()).hexdigest(), 16) % total
    acc = 0
    for a, w in rows:
        acc += w
        if acc > r:
            return {"list_hash": lh, "total": total, "r": r, "winner": a, "winner_weight": w, "participants": len(rows)}
    raise AssertionError("unreachable: r < total")


def verify(record, participants):
    """Recompute the draw from stored data. record: {"list_hash", "blockhash", "winner",
    "total_weight", "r"}; participants: {address: weight}. -> {"ok", "checks", "recomputed"}."""
    re = pick(participants, record.get("blockhash") or "")
    checks = {
        "list_hash": re["list_hash"] == record.get("list_hash"),
        "total_weight": re["total"] == int(record.get("total_weight") or 0),
        "r": (re["r"] is None and record.get("r") is None) or (record.get("r") is not None and re["r"] == int(record["r"])),
        "winner": re["winner"] == record.get("winner"),
    }
    return {"ok": all(checks.values()), "checks": checks, "recomputed": re}
