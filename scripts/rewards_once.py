"""Manual Rewards & Burns run on a real Robinhood Chain token.

  python3 scripts/rewards_once.py --dry-run --day YYYY-MM-DD [--token 0x...]
      draw for the day [22:00 UTC day-1, 22:00 UTC day) from the chain, without writing to the DB: participants, top 5 weights with odds,
      list_hash, seed block, winner, verify, compute time and number of RPC requests;
  python3 scripts/rewards_once.py --day YYYY-MM-DD
      same, writing to the DB (DRAW_DB_PATH; idempotent), then checks burns, payouts and supply.
Token: --token or REWARDS_TOKEN (default: the project token); DEV_WALLETS from env. Requires CRAWLER_RPC in .env.
"""
import argparse, os, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import env
env.load_dotenv()
import rewards as rw
import rewards_service as rs
from chains import robinhood as ch
from rewards_store import RewardsStore


def human(raw, dec):
    return f"{raw / 10 ** dec:,.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--token")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not rw.is_day(a.day):
        sys.exit("--day: YYYY-MM-DD")
    cfg = rs.config()
    if a.token:
        cfg["token"] = a.token.lower()
    token = cfg["token"]
    if time.time() < rw.seed_time(a.day):
        sys.exit("too early: no seed block yet (seed is 22:01 UTC on the draw day)")
    if not a.dry_run:
        store = RewardsStore()
        row = rs.run_draw(store, cfg, a.day)
        print({k: v for k, v in row.items()})
        print("new burns:", rs.check_burns(store, cfg), "| payouts marked:", rs.check_payouts(store, cfg))
        print("supply:", rs.refresh_supply(store, cfg))
        return

    t0, r0 = time.time(), ch.REQUESTS[0]
    launch = rs.launch_info(None, token)
    dec = rs.decimals(None, token)
    rec, parts, info = rs.compute_day(token, a.day, cfg["dev_wallets"], launch, dec)
    dt, nreq = time.time() - t0, ch.REQUESTS[0] - r0
    print(f"token {token}, decimals {dec}, launched in block {launch['block']}, curve {launch['curve']}")
    print(f"day {a.day}: blocks [{rec['start_block']}, {rec['end_block']}), DEV_WALLETS: {len(cfg['dev_wallets'])}")
    print(f"transfers from launch to end of day: {info['transfers']}, during the day: {info['day_transfers']}")
    print(f"addresses with non-zero average balance: {info['holders']}, excluded infrastructure/dev/burn: "
          f"{info['excluded']}, contracts: {info['contracts']}")
    print(f"participants: {rec['participants']}, total weight {rec['total_weight']} ({human(rec['total_weight'], dec)} tokens)")
    for addr, w in sorted(parts.items(), key=lambda kv: -kv[1])[:5]:
        print(f"  {addr}  weight {human(w, dec)} tokens, odds {w / rec['total_weight'] * 100:.2f}%")
    print(f"list_hash: {rec['list_hash']}")
    seed = datetime.fromtimestamp(rec["seed_time"], timezone.utc).isoformat()
    print(f"seed: first block with timestamp >= {seed} -> block {rec['seed_block']}, hash {rec['blockhash']}")
    if rec["winner"]:
        print(f"r = {rec['r']}, winner {rec['winner']} (weight {human(rec['winner_weight'], dec)} tokens, "
              f"odds {rec['winner_weight'] / rec['total_weight'] * 100:.2f}%)")
    else:
        print("no participants: no eligible holders")
    v = rw.verify(rec, parts)
    print(f"verify: {'ok' if v['ok'] else 'FAIL'} {v['checks']}")
    print(f"compute time {dt:.1f} s, RPC requests {nreq}")


if __name__ == "__main__":
    main()
