"""Manual draw run (snapshot and/or draw), for testing on any pump.fun token.

  python3 scripts/draw_once.py --dry-run [--mint MINT]
      one snapshot now (not written to the DB), weights from that snapshot, threshold at the current price,
      seed is the first slot with blockTime >= 00:01 UTC today, winner and verify;
  python3 scripts/draw_once.py [--mint MINT]
      snapshot of the current hour into the DB (DRAW_DB_PATH; skipped if it already exists);
  python3 scripts/draw_once.py --draw-day YYYY-MM-DD [--mint MINT]
      draw for the day from the DB snapshots (idempotent; not before 00:05 UTC of the next day).
Token: --mint or DRAW_MINT. Requires HELIUS_RPC and SOLANA_RPC in .env.
"""
import argparse, os, sys, time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import env
env.load_dotenv()
import draw as dr
import draw_service as ds
from draw_store import Store


def human(raw, decimals):
    return f"{raw / 10 ** decimals:,.2f}"


def dry_run(mint, cfg):
    t0 = time.time()
    snap = ds.take_snapshot(None, mint, dry_run=True)
    t_snap = time.time() - t0
    decimals, price = ds.token_decimals(mint), ds.token_price(mint)
    today = dr.day_key(time.time())
    yesterday = datetime.fromtimestamp(dr.seed_time(today) - 2 * 86400, timezone.utc).strftime("%Y-%m-%d")
    t1 = time.time()
    rec, parts = ds.decide(yesterday, mint, [snap["balances"]], cfg, decimals, price)   # seed = today 00:01 UTC
    t_draw = time.time() - t1
    print(f"token {mint}, decimals {decimals}, GeckoTerminal price {price}")
    print(f"snapshots: 1 (index slot {snap['slot']}, {t_snap:.1f} s): holders {snap['holders']}, regular wallets {snap['wallets']}")
    print(f"threshold: {rec['threshold_mode']}, {human(int(rec['threshold_raw']), decimals)} tokens "
          f"(min_usd {cfg['min_usd']}, min_tokens {cfg['min_tokens']})")
    print(f"participants after threshold: {rec['participants']}, total weight {rec['total_weight']}")
    top = sorted(parts.items(), key=lambda kv: -kv[1])[:5]
    for a, w in top:
        print(f"  {a}  weight {w} ({human(w, decimals)} tokens, {w / rec['total_weight'] * 100:.2f}%)")
    print(f"list_hash: {rec['list_hash']}")
    seed = datetime.fromtimestamp(rec["seed_time"], timezone.utc).isoformat()
    print(f"seed: first slot with blockTime >= {seed} -> slot {rec['seed_slot']}, blockhash {rec['blockhash']} ({t_draw:.1f} s)")
    print(f"r = {rec['r']}, winner {rec['winner']} (weight {rec['winner_weight']}), status {rec['status']}")
    v = dr.verify(rec, parts)
    print(f"verify: {'ok' if v['ok'] else 'FAIL'} {v['checks']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mint")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--draw-day")
    a = ap.parse_args()
    cfg = ds.config()
    mint = a.mint or cfg["mint"]
    if not mint:
        sys.exit("--mint or DRAW_MINT is required")
    cfg["mint"] = mint
    if a.dry_run:
        return dry_run(mint, cfg)
    store = Store()
    if a.draw_day:
        if not dr.is_day(a.draw_day):
            sys.exit("--draw-day: YYYY-MM-DD")
        try:
            row = ds.run_draw(store, mint, a.draw_day, cfg)
        except ds.NotReady as e:
            sys.exit(f"too early: {e}")
        print(ds.draw_json(row) if row else "no snapshots for this day")
        return
    s = ds.take_snapshot(store, mint)
    print({k: v for k, v in s.items() if k != "balances"})


if __name__ == "__main__":
    main()
