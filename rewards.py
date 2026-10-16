"""Token Burn & Holder Rewards: pure functions without network access, like detect.py.

Network -- rewards_service.py (via chains/robinhood.py), storage -- rewards_store.py. Rules -- DEV_NOTES.md,
section "Rewards & Burns".

Verifiable winner selection (canonical list, list_hash, r, cumulative sum) and verify are
shared with the Solana draw: draw.pick / draw.verify, they do not depend on the chain.

Schedule -- fixed UTC times:
  draw D (D -- draw date) -- at 22:00 UTC on day D, computed at 22:05 UTC; the day for the average balance is
  [22:00 UTC D-1, 22:00 UTC D); seed -- the first block with timestamp >= 22:01 UTC D;
  scheduled burns -- 10:00 and 22:00 UTC every day (timer only).

Units: balances and weights -- integer base units of the token (raw, decimals applied).
Addresses -- lowercase 0x.
"""
from datetime import datetime, timedelta, timezone

import draw as dr

ZERO = "0x" + "0" * 40
DEAD = "0x000000000000000000000000000000000000dead"
BURN_ADDRESS = "0x000000000000000000000000000000000000dEaD"   # burn address for display (checksummed)
BURN_SINKS = {ZERO, DEAD}                                       # the zero address is a burn too
DAY_SECONDS = 86400
DRAW_HOUR = 22                 # draw and weight-day boundary: 22:00 UTC
SEED_DELAY = 60                # seed -- the first block with timestamp >= 22:01 UTC
RUN_DELAY = 300                # computation -- at 22:05 UTC
BURN_HOURS = (10, 22)          # scheduled burns: 10:00 and 22:00 UTC

# shared selection rules (do not depend on the chain)
pick, verify, canonical, list_hash, is_day = dr.pick, dr.verify, dr.canonical, dr.list_hash, dr.is_day


def _date(day):
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def day_bounds(day):
    """Weight day of draw `day` in unix time: [22:00 UTC day-1, 22:00 UTC day)."""
    end = int(_date(day).replace(hour=DRAW_HOUR).timestamp())
    return end - DAY_SECONDS, end


def draw_at(day):
    """Time of draw `day` (what the timer shows): 22:00 UTC day = end of the weight day."""
    return day_bounds(day)[1]


def seed_time(day):
    """Seed of draw `day`: the first block with timestamp >= 22:01 UTC day."""
    return draw_at(day) + SEED_DELAY


def draw_time(day):
    """When draw `day` can be computed (and from when the payout is searched for): 22:05 UTC day."""
    return draw_at(day) + RUN_DELAY


def day_key(ts):
    """The draw whose weight day contains moment ts: from 22:00 UTC it is already tomorrow's."""
    return (datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=24 - DRAW_HOUR)).strftime("%Y-%m-%d")


def prev_day(ts):
    """The last draw whose weight day has already ended by moment ts."""
    return (_date(day_key(ts)) - timedelta(days=1)).strftime("%Y-%m-%d")


def next_draw(now):
    """Next draw > now: (day, time 22:00 UTC)."""
    day = day_key(now)
    return day, draw_at(day)


# ---------- weights ----------
def balances_before(transfers, block):
    """{address: balance} from transfers in blocks < block (transfers in chain order)."""
    bal = {}
    for t in transfers:
        if t["block"] >= block:
            break
        bal[t["frm"]] = bal.get(t["frm"], 0) - t["amount"]
        bal[t["to"]] = bal.get(t["to"], 0) + t["amount"]
    return bal


def time_weights(transfers, start_block, end_block, ts_of, day_start, day_end):
    """Time-weighted average balance of each address over the day.

    transfers -- all token transfers up to the end of the day, in chain order (block, log_index);
    start_block / end_block -- the first block with timestamp >= start / end of the day: transfers in blocks
    < start_block give the balance at the start of the day, in [start_block, end_block) -- movements during the day;
    ts_of -- {block: timestamp} for the day's blocks.
    Weight = integral of balance dt / day length, rounded down; zero weights are dropped.
    Example: bought X at 12:00 and held until the end of the day -- weight X / 2."""
    span = day_end - day_start
    bal = balances_before(transfers, start_block)
    area, last = {}, {}

    def move(addr, delta, ts):
        area[addr] = area.get(addr, 0) + bal.get(addr, 0) * (ts - last.get(addr, day_start))
        last[addr] = ts
        bal[addr] = bal.get(addr, 0) + delta

    for t in transfers:
        if t["block"] < start_block:
            continue
        if t["block"] >= end_block:
            break
        ts = min(max(ts_of[t["block"]], day_start), day_end)
        move(t["frm"], -t["amount"], ts)
        move(t["to"], t["amount"], ts)
    out = {}
    for a, b in bal.items():
        total = area.get(a, 0) + b * (day_end - last.get(a, day_start))
        w = total // span
        if w > 0:
            out[a] = w
    return out


def eligible(weights, excluded, contracts=()):
    """Participants: nonzero weight, not in excluded (DEV_WALLETS, infrastructure, zero and dead) and not a contract."""
    skip = {a.lower() for a in excluded} | BURN_SINKS | {a.lower() for a in contracts}
    return {a: w for a, w in weights.items() if w > 0 and a not in skip}


# ---------- burns and payouts ----------
def find_burns(transfers, dev_wallets):
    """Developer burns: transfers from any of dev_wallets to the zero or dead address."""
    devs = {a.lower() for a in dev_wallets}
    return [t for t in transfers if t["frm"] in devs and t["to"] in BURN_SINKS and t["amount"] > 0]


CURRENCY_TOKEN, CURRENCY_ETH = "CRAWLSCAN", "ETH"


def match_payouts(draws, transfers, dev_wallets, ts_of, used_txs=()):
    """Payouts: for each draw with a winner and no payout -- the first transfer from any of dev_wallets
    to the winner's wallet no earlier than the draw computation (draw_time, 22:05 UTC): token or native ETH
    (transfer with a currency field; ETH -- log_index -1). "First" -- by block, within a block -- by log index (ETH first).
    Draws -- by day in order; one transfer is the payout of only one draw (used_txs -- already counted).
    -> {day: transfer}."""
    devs = {a.lower() for a in dev_wallets}
    used = {(tx, li) for tx, li in used_txs}
    transfers = sorted(transfers, key=lambda t: (t["block"], t["log_index"]))
    out = {}
    for d in sorted(draws, key=lambda d: d["day"]):
        if not d.get("winner") or d.get("payout_tx"):
            continue
        after = draw_time(d["day"])
        for t in transfers:
            key = (t["tx"], t["log_index"])
            if (t["frm"] in devs and t["to"] == d["winner"] and t["amount"] > 0 and key not in used
                    and ts_of.get(t["block"], 0) >= after):
                out[d["day"]] = t
                used.add(key)
                break
    return out


def total_burned(minted, supply, dead_balance):
    """Burned supply of a Pons V2 token (OpenZeppelin ERC20 + Burnable): burn() emits Transfer to 0x0 and
    reduces totalSupply; transfer to 0x0 is forbidden (revert); transfer to dead leaves tokens in the supply.
    Hence burned = minted (transfers from 0x0) - totalSupply + dead balance. balanceOf(0x0) is always 0."""
    return max(0, int(minted) - int(supply)) + int(dead_balance)


def next_burns(now, n=2):
    """Next n scheduled burns > now (timer only): 10:00 and 22:00 UTC every day."""
    day0 = int(now) - int(now) % DAY_SECONDS
    out, d = [], day0
    while len(out) < n:
        out += [d + h * 3600 for h in BURN_HOURS if d + h * 3600 > now][:n - len(out)]
        d += DAY_SECONDS
    return out


def next_burn(now):
    return next_burns(now, 1)[0]
