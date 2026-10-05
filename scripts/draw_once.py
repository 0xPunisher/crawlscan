"""Ручной запуск розыгрыша (снимок и/или розыгрыш), для теста на любом pump.fun-токене.

  python3 scripts/draw_once.py --dry-run [--mint MINT]
      один снимок сейчас (в базу не пишется), веса по этому снимку, порог по текущей цене,
      seed — первый слот с blockTime >= 00:01 UTC сегодня, победитель и verify;
  python3 scripts/draw_once.py [--mint MINT]
      снимок текущего часа в базу (DRAW_DB_PATH; уже есть — не повторяется);
  python3 scripts/draw_once.py --draw-day YYYY-MM-DD [--mint MINT]
      розыгрыш за сутки по снимкам в базе (идемпотентно; не раньше 00:05 UTC следующих суток).
Токен — --mint или DRAW_MINT. Нужны HELIUS_RPC и SOLANA_RPC в .env.
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
    rec, parts = ds.decide(yesterday, mint, [snap["balances"]], cfg, decimals, price)   # seed = сегодня 00:01 UTC
    t_draw = time.time() - t1
    print(f"токен {mint}, decimals {decimals}, цена GeckoTerminal {price}")
    print(f"снимков: 1 (слот индекса {snap['slot']}, {t_snap:.1f} с): холдеров {snap['holders']}, обычных кошельков {snap['wallets']}")
    print(f"порог: {rec['threshold_mode']}, {human(int(rec['threshold_raw']), decimals)} токенов "
          f"(min_usd {cfg['min_usd']}, min_tokens {cfg['min_tokens']})")
    print(f"участников после порога: {rec['participants']}, сумма весов {rec['total_weight']}")
    top = sorted(parts.items(), key=lambda kv: -kv[1])[:5]
    for a, w in top:
        print(f"  {a}  вес {w} ({human(w, decimals)} токенов, {w / rec['total_weight'] * 100:.2f}%)")
    print(f"list_hash: {rec['list_hash']}")
    seed = datetime.fromtimestamp(rec["seed_time"], timezone.utc).isoformat()
    print(f"seed: первый слот с blockTime >= {seed} -> слот {rec['seed_slot']}, blockhash {rec['blockhash']} ({t_draw:.1f} с)")
    print(f"r = {rec['r']}, победитель {rec['winner']} (вес {rec['winner_weight']}), статус {rec['status']}")
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
        sys.exit("нужен --mint или DRAW_MINT")
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
            sys.exit(f"рано: {e}")
        print(ds.draw_json(row) if row else "за эти сутки снимков нет")
        return
    s = ds.take_snapshot(store, mint)
    print({k: v for k, v in s.items() if k != "balances"})


if __name__ == "__main__":
    main()
