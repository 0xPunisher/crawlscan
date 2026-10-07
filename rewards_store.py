"""Хранилище Rewards & Burns: SQLite, тот же файл, что у розыгрыша (DRAW_DB_PATH, по умолчанию ./data/draw.db),
свои таблицы с префиксом rw_:

  rw_draws         — розыгрыши (ключ — UTC-сутки весов 'YYYY-MM-DD'): блоки суток, seed, list_hash, победитель, выплата;
  rw_participants  — участники розыгрыша с весами (для проверки);
  rw_burns         — сжигания разработчика (ключ — транзакция + номер лога);
  rw_meta          — служебное: курсор сжиганий, набор DEV_WALLETS, сожжённый сапплай, запуск токена, decimals.
Всё идемпотентно: повторная запись тех же суток или той же транзакции ничего не дублирует.
Базы нет — розыгрыши пересчитываются из блокчейна детерминированно (rewards_service.compute_day).
"""
import json, os, sqlite3, threading, time

from draw_store import DEFAULT_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS rw_draws (
  day TEXT PRIMARY KEY, token TEXT NOT NULL, status TEXT NOT NULL,
  start_block INTEGER, end_block INTEGER, list_hash TEXT NOT NULL, seed_time INTEGER NOT NULL,
  seed_block INTEGER, blockhash TEXT, r TEXT, winner TEXT, winner_weight TEXT, total_weight TEXT NOT NULL,
  participants INTEGER NOT NULL, decimals INTEGER, dev_wallets TEXT NOT NULL, created_at INTEGER NOT NULL,
  payout_amount TEXT, payout_tx TEXT, payout_log_index INTEGER, payout_block INTEGER, payout_from TEXT, paid_at INTEGER,
  payout_currency TEXT);
CREATE TABLE IF NOT EXISTS rw_participants (
  day TEXT NOT NULL, address TEXT NOT NULL, weight TEXT NOT NULL, PRIMARY KEY (day, address));
CREATE TABLE IF NOT EXISTS rw_burns (
  tx TEXT NOT NULL, log_index INTEGER NOT NULL, token TEXT NOT NULL, wallet TEXT NOT NULL, to_addr TEXT NOT NULL,
  amount TEXT NOT NULL, block INTEGER NOT NULL, ts INTEGER NOT NULL, PRIMARY KEY (tx, log_index));
CREATE TABLE IF NOT EXISTS rw_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
# веса, суммы и количества — строками: 18 знаков, не влезают в INTEGER (int64)
BIG = ("r", "winner_weight", "total_weight", "payout_amount", "amount")


def _row(r):
    return {k: (int(r[k]) if k in BIG and r[k] is not None else r[k]) for k in r.keys()} if r else None


class RewardsStore:
    def __init__(self, path=None):
        self.path = path or os.environ.get("DRAW_DB_PATH") or DEFAULT_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock, self.db:
            self.db.executescript(SCHEMA)
            # база до выплат в ETH: колонки валюты нет; старые выплаты (NULL) — токеном
            if "payout_currency" not in {r["name"] for r in self.db.execute("PRAGMA table_info(rw_draws)")}:
                self.db.execute("ALTER TABLE rw_draws ADD COLUMN payout_currency TEXT")

    def close(self):
        with self._lock:
            self.db.close()

    # ---------- служебное ----------
    def meta(self, key, default=None):
        with self._lock:
            r = self.db.execute("SELECT value FROM rw_meta WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_meta(self, key, value):
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO rw_meta(key, value) VALUES (?,?)", (key, json.dumps(value)))

    # ---------- розыгрыши ----------
    def get_draw(self, day):
        with self._lock:
            return _row(self.db.execute("SELECT * FROM rw_draws WHERE day=?", (day,)).fetchone())

    def save_draw(self, rec, participants):
        """Розыгрыш суток и участники одной транзакцией, один раз. -> True, если записан сейчас."""
        cols = ["day", "token", "status", "start_block", "end_block", "list_hash", "seed_time", "seed_block", "blockhash",
                "r", "winner", "winner_weight", "total_weight", "participants", "decimals", "dev_wallets", "created_at"]
        vals = [str(rec.get(c)) if c in BIG and rec.get(c) is not None else rec.get(c) for c in cols]
        with self._lock, self.db:
            cur = self.db.execute(f"INSERT OR IGNORE INTO rw_draws({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            if cur.rowcount == 0:
                return False
            self.db.executemany("INSERT OR IGNORE INTO rw_participants(day, address, weight) VALUES (?,?,?)",
                                [(rec["day"], a, str(int(w))) for a, w in participants.items()])
            return True

    def participants(self, day):
        with self._lock:
            return {r["address"]: int(r["weight"]) for r in self.db.execute(
                "SELECT address, weight FROM rw_participants WHERE day=?", (day,))}

    def latest_draw(self):
        with self._lock:
            return _row(self.db.execute("SELECT * FROM rw_draws ORDER BY day DESC LIMIT 1").fetchone())

    def draws(self, limit=30):
        with self._lock:
            return [_row(r) for r in self.db.execute("SELECT * FROM rw_draws ORDER BY day DESC LIMIT ?", (int(limit),))]

    def draws_before(self, max_day, limit):
        """Страница истории розыгрышей: дни <= max_day (None — без границы), новые первыми, limit + 1 строка
        (лишняя — признак следующей страницы)."""
        with self._lock:
            return [_row(r) for r in self.db.execute(
                "SELECT * FROM rw_draws WHERE (? IS NULL OR day <= ?) ORDER BY day DESC LIMIT ?",
                (max_day, max_day, int(limit) + 1))]

    def draws_count(self):
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM rw_draws").fetchone()[0]

    def unpaid_draws(self):
        with self._lock:
            return [_row(r) for r in self.db.execute(
                "SELECT * FROM rw_draws WHERE winner IS NOT NULL AND payout_tx IS NULL ORDER BY day")]

    def payout_keys(self):
        """{(tx, log_index)} уже засчитанных выплат."""
        with self._lock:
            return {(r["payout_tx"], r["payout_log_index"]) for r in self.db.execute(
                "SELECT payout_tx, payout_log_index FROM rw_draws WHERE payout_tx IS NOT NULL")}

    def set_payout(self, day, t, ts):
        """Выплата розыгрыша — перевод t (с блокчейна): токен или ETH (t["currency"], сумма — в wei / базовых
        единицах токена). Уже записана — не меняется. -> True, если записана сейчас."""
        with self._lock, self.db:
            cur = self.db.execute(
                "UPDATE rw_draws SET payout_amount=?, payout_tx=?, payout_log_index=?, payout_block=?, payout_from=?, "
                "paid_at=?, payout_currency=? WHERE day=? AND winner IS NOT NULL AND payout_tx IS NULL",
                (str(t["amount"]), t["tx"], t["log_index"], t["block"], t["frm"], int(ts),
                 t.get("currency") or "CRAWLSCAN", day))
            return cur.rowcount > 0

    # ---------- сжигания ----------
    def save_burns(self, token, burns, ts_of):
        """-> сколько новых записано (повтор той же транзакции и лога не дублируется)."""
        with self._lock, self.db:
            before = self.db.total_changes
            self.db.executemany(
                "INSERT OR IGNORE INTO rw_burns(tx, log_index, token, wallet, to_addr, amount, block, ts) VALUES (?,?,?,?,?,?,?,?)",
                [(b["tx"], b["log_index"], token, b["frm"], b["to"], str(b["amount"]), b["block"], int(ts_of[b["block"]]))
                 for b in burns])
            return self.db.total_changes - before

    def burns(self, token, limit=None):
        q = "SELECT * FROM rw_burns WHERE token=? ORDER BY block DESC, log_index DESC" + (" LIMIT ?" if limit else "")
        with self._lock:
            return [_row(r) for r in self.db.execute(q, (token,) + ((int(limit),) if limit else ()))]

    def burns_before(self, token, before, limit):
        """Страница истории сжиганий: время < before (None — без границы), новые первыми. Сжигания с одним
        временем страницей не разрываются (курсор — время): страница добирает все строки с временем последней,
        поэтому может быть длиннее limit. -> (строки, есть ли ещё)."""
        q = ("SELECT * FROM rw_burns WHERE token=? AND (? IS NULL OR ts < ?) {} "
             "ORDER BY ts DESC, block DESC, log_index DESC")
        with self._lock:
            rows = [_row(r) for r in self.db.execute(q.format("") + " LIMIT ?", (token, before, before, int(limit)))]
            if len(rows) < limit:
                return rows, False
            last = rows[-1]["ts"]
            tied = [_row(r) for r in self.db.execute(q.format("AND ts=?"), (token, before, before, last))]
            more = self.db.execute("SELECT 1 FROM rw_burns WHERE token=? AND ts<? LIMIT 1", (token, last)).fetchone()
        return [r for r in rows if r["ts"] != last] + tied, bool(more)

    def burned_total(self, token):
        """(сумма сожжённого разработчиком, число сжиганий)."""
        with self._lock:
            rows = self.db.execute("SELECT amount FROM rw_burns WHERE token=?", (token,)).fetchall()
        return sum(int(r["amount"]) for r in rows), len(rows)

    def clear_burns(self, token):
        with self._lock, self.db:
            self.db.execute("DELETE FROM rw_burns WHERE token=?", (token,))
