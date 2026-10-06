"""Ручной запуск Rewards & Burns на реальном токене Robinhood Chain.

  python3 scripts/rewards_once.py --dry-run --day YYYY-MM-DD [--token 0x...]
      розыгрыш за сутки [22:00 UTC day-1, 22:00 UTC day) из блокчейна, без записи в базу: участники, топ-5 весов с шансом,
      list_hash, seed-блок, победитель, verify, время расчёта и число RPC-запросов;
  python3 scripts/rewards_once.py --day YYYY-MM-DD
      то же с записью в базу (DRAW_DB_PATH; идемпотентно), затем проверка сжиганий, выплат и сапплая.
Токен — --token или REWARDS_TOKEN (по умолчанию токен проекта); DEV_WALLETS — из env. Нужен CRAWLER_RPC в .env.
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
        sys.exit("рано: seed-блока ещё нет (seed — 22:01 UTC дня розыгрыша)")
    if not a.dry_run:
        store = RewardsStore()
        row = rs.run_draw(store, cfg, a.day)
        print({k: v for k, v in row.items()})
        print("сжиганий новых:", rs.check_burns(store, cfg), "| выплат отмечено:", rs.check_payouts(store, cfg))
        print("сапплай:", rs.refresh_supply(store, cfg))
        return

    t0, r0 = time.time(), ch.REQUESTS[0]
    launch = rs.launch_info(None, token)
    dec = rs.decimals(None, token)
    rec, parts, info = rs.compute_day(token, a.day, cfg["dev_wallets"], launch, dec)
    dt, nreq = time.time() - t0, ch.REQUESTS[0] - r0
    print(f"токен {token}, decimals {dec}, запуск в блоке {launch['block']}, кривая {launch['curve']}")
    print(f"сутки {a.day}: блоки [{rec['start_block']}, {rec['end_block']}), DEV_WALLETS: {len(cfg['dev_wallets'])}")
    print(f"переводов с запуска до конца суток: {info['transfers']}, за сутки: {info['day_transfers']}")
    print(f"адресов с ненулевым средним балансом: {info['holders']}, исключено инфраструктуры/dev/burn: "
          f"{info['excluded']}, контрактов: {info['contracts']}")
    print(f"участников: {rec['participants']}, сумма весов {rec['total_weight']} ({human(rec['total_weight'], dec)} токенов)")
    for addr, w in sorted(parts.items(), key=lambda kv: -kv[1])[:5]:
        print(f"  {addr}  вес {human(w, dec)} токенов, шанс {w / rec['total_weight'] * 100:.2f}%")
    print(f"list_hash: {rec['list_hash']}")
    seed = datetime.fromtimestamp(rec["seed_time"], timezone.utc).isoformat()
    print(f"seed: первый блок с timestamp >= {seed} -> блок {rec['seed_block']}, hash {rec['blockhash']}")
    if rec["winner"]:
        print(f"r = {rec['r']}, победитель {rec['winner']} (вес {human(rec['winner_weight'], dec)} токенов, "
              f"шанс {rec['winner_weight'] / rec['total_weight'] * 100:.2f}%)")
    else:
        print("участников нет: no eligible holders")
    v = rw.verify(rec, parts)
    print(f"verify: {'ok' if v['ok'] else 'FAIL'} {v['checks']}")
    print(f"время расчёта {dt:.1f} с, RPC-запросов {nreq}")


if __name__ == "__main__":
    main()
