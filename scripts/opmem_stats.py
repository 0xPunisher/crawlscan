"""Сводка памяти операторов (opmem): сколько токенов, операторов, кошельков, размер файла и топ-10 кошельков,
встреченных в наибольшем числе токенов. Только чтение.

  python3 scripts/opmem_stats.py [путь к memory.db]   (по умолчанию OPMEM_DB_PATH / рядом с DRAW_DB_PATH)
"""
import datetime as dt, os, sqlite3, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from env import load_dotenv   # noqa: E402

load_dotenv()
import opmem                  # noqa: E402


def size(path):
    return sum(os.path.getsize(p) for p in (path, path + "-wal", path + "-shm") if os.path.exists(p))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else opmem.db_path()
    if not os.path.exists(path):
        sys.exit(f"no database at {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    one = lambda q: con.execute(q).fetchone()[0]
    print(f"file      {path}  {size(path) / 1024:.1f} KB (with WAL)")
    print(f"scans     {one('SELECT COUNT(*) FROM scans')}  (limited {one('SELECT COUNT(*) FROM scans WHERE limited')}, "
          f"partial {one('SELECT COUNT(*) FROM scans WHERE partial')})")
    print(f"tokens    {one('SELECT COUNT(DISTINCT token) FROM scans')}")
    print(f"operators {one('SELECT COUNT(DISTINCT operator_id) FROM operators')} distinct "
          f"({one('SELECT COUNT(*) FROM operators')} rows)")
    print(f"wallets   {one('SELECT COUNT(DISTINCT wallet) FROM wallets')} distinct "
          f"({one('SELECT COUNT(*) FROM wallets')} rows)")
    first, last = con.execute("SELECT MIN(ts), MAX(ts) FROM scans").fetchone()
    if first:
        f = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"period    {f(first)} — {f(last)}")
    rows = con.execute("""
        SELECT wallet, COUNT(DISTINCT token) AS tokens, GROUP_CONCAT(DISTINCT roles) AS roles,
               COUNT(DISTINCT operator_id) AS ops
        FROM wallets GROUP BY wallet ORDER BY tokens DESC, wallet LIMIT 10""").fetchall()
    print("\ntop wallets by tokens:")
    for w, n, roles, ops in rows:
        uniq = sorted({r for r in (roles or "").split(",") if r})
        print(f"  {w}  {n:>3} tokens  {ops} operators  {','.join(uniq)}")
    con.close()


if __name__ == "__main__":
    main()
