"""Draw storage: SQLite (standard sqlite3). Path is DRAW_DB_PATH, default ./data/draw.db.

Tables:
  snapshots          — hourly snapshots (key: UTC hour 'YYYY-MM-DDTHH'): when taken, slot, holder count;
  snapshot_balances  — balances of regular wallets in a snapshot (raw units);
  owners             — owner check cache: account owner program and whether it is a wallet;
  draws              — draws (key: UTC day of weights 'YYYY-MM-DD'): list_hash, seed, winner, prize;
  draw_participants  — draw participants with weights (for verification).
Everything is idempotent: writing the same hour or the same day again duplicates nothing.
"""
import os, time

import db

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(ROOT, "data", "draw.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  hour TEXT PRIMARY KEY, day TEXT NOT NULL, mint TEXT NOT NULL, taken_at INTEGER NOT NULL,
  slot INTEGER, holders INTEGER NOT NULL, wallets INTEGER NOT NULL, total INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS snapshots_day ON snapshots(day);
CREATE TABLE IF NOT EXISTS snapshot_balances (
  hour TEXT NOT NULL, owner TEXT NOT NULL, amount INTEGER NOT NULL, PRIMARY KEY (hour, owner));
CREATE TABLE IF NOT EXISTS owners (
  address TEXT PRIMARY KEY, program TEXT, wallet INTEGER NOT NULL, checked_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS draws (
  day TEXT PRIMARY KEY, mint TEXT NOT NULL, status TEXT NOT NULL, snapshots INTEGER NOT NULL,
  list_hash TEXT NOT NULL, seed_time INTEGER NOT NULL, seed_slot INTEGER, blockhash TEXT,
  r TEXT, winner TEXT, winner_weight TEXT, total_weight TEXT NOT NULL, participants INTEGER NOT NULL,
  decimals INTEGER, price_usd REAL, threshold_mode TEXT, threshold_raw TEXT,
  prize_amount TEXT, prize_currency TEXT, payout_tx TEXT, created_at INTEGER NOT NULL, paid_at INTEGER);
CREATE TABLE IF NOT EXISTS draw_participants (
  day TEXT NOT NULL, address TEXT NOT NULL, weight TEXT NOT NULL, PRIMARY KEY (day, address));
"""
# weights and sums are stored as strings: the day's weight total may not fit in INTEGER (int64)


class Store:
    def __init__(self, path=None):
        self.path = path or os.environ.get("DRAW_DB_PATH") or DEFAULT_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = db.lock_for(self.path)   # one lock per file for all stores
        self.db = db.connect(self.path)
        with self._lock, self.db:
            self.db.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self.db.close()

    # ---------- snapshots ----------
    def has_snapshot(self, hour):
        with self._lock:
            return self.db.execute("SELECT 1 FROM snapshots WHERE hour=?", (hour,)).fetchone() is not None

    def save_snapshot(self, hour, day, mint, slot, holders, balances, taken_at=None):
        """Writes the hour's snapshot once. -> True if written now; False if it already existed."""
        with self._lock, self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO snapshots(hour, day, mint, taken_at, slot, holders, wallets, total) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (hour, day, mint, int(taken_at or time.time()), slot, holders, len(balances), sum(balances.values())))
            if cur.rowcount == 0:
                return False
            self.db.executemany("INSERT OR IGNORE INTO snapshot_balances(hour, owner, amount) VALUES (?,?,?)",
                                [(hour, o, int(v)) for o, v in balances.items()])
            return True

    def snapshots_of_day(self, day, mint):
        """[{owner: balance}] over the taken snapshots of the day for token mint, in hour order."""
        with self._lock:
            hours = [r["hour"] for r in self.db.execute(
                "SELECT hour FROM snapshots WHERE day=? AND mint=? ORDER BY hour", (day, mint))]
            return [{r["owner"]: int(r["amount"]) for r in self.db.execute(
                "SELECT owner, amount FROM snapshot_balances WHERE hour=?", (h,))} for h in hours]

    def snapshot_hours(self, day, mint):
        with self._lock:
            return [dict(r) for r in self.db.execute(
                "SELECT hour, taken_at, slot, holders, wallets FROM snapshots WHERE day=? AND mint=? ORDER BY hour",
                (day, mint))]

    def days_with_snapshots(self, mint):
        with self._lock:
            return [r["day"] for r in self.db.execute(
                "SELECT DISTINCT day FROM snapshots WHERE mint=? ORDER BY day", (mint,))]

    # ---------- owner cache ----------
    def owners_cached(self, addresses):
        """{address: is_wallet} for those already checked."""
        out = {}
        addrs = list(addresses)
        with self._lock:
            for i in range(0, len(addrs), 500):
                part = addrs[i:i + 500]
                q = "SELECT address, wallet FROM owners WHERE address IN (%s)" % ",".join("?" * len(part))
                out.update({r["address"]: bool(r["wallet"]) for r in self.db.execute(q, part)})
        return out

    def save_owners(self, rows):
        """rows: [(address, program | None, is_wallet)]."""
        now = int(time.time())
        with self._lock, self.db:
            self.db.executemany("INSERT OR REPLACE INTO owners(address, program, wallet, checked_at) VALUES (?,?,?,?)",
                                [(a, p, int(bool(w)), now) for a, p, w in rows])

    # ---------- draws ----------
    def get_draw(self, day):
        with self._lock:
            r = self.db.execute("SELECT * FROM draws WHERE day=?", (day,)).fetchone()
        return dict(r) if r else None

    def save_draw(self, rec, participants):
        """Writes the day's draw and participants in one transaction, once. -> True if written now."""
        cols = ["day", "mint", "status", "snapshots", "list_hash", "seed_time", "seed_slot", "blockhash", "r", "winner",
                "winner_weight", "total_weight", "participants", "decimals", "price_usd", "threshold_mode",
                "threshold_raw", "created_at"]
        vals = [rec.get(c) for c in cols]
        vals = [str(v) if c in ("r", "winner_weight", "total_weight", "threshold_raw") and v is not None else v
                for c, v in zip(cols, vals)]
        with self._lock, self.db:
            cur = self.db.execute(f"INSERT OR IGNORE INTO draws({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            if cur.rowcount == 0:
                return False
            self.db.executemany("INSERT OR IGNORE INTO draw_participants(day, address, weight) VALUES (?,?,?)",
                                [(rec["day"], a, str(int(w))) for a, w in participants.items()])
            return True

    def participants(self, day):
        """{address: weight} of the day's draw participants."""
        with self._lock:
            return {r["address"]: int(r["weight"]) for r in self.db.execute(
                "SELECT address, weight FROM draw_participants WHERE day=?", (day,))}

    def latest_draw(self):
        with self._lock:
            r = self.db.execute("SELECT * FROM draws ORDER BY day DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def history(self, limit=30):
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM draws ORDER BY day DESC LIMIT ?", (int(limit),))]

    def set_payout(self, day, amount, currency, tx):
        """Records the prize. -> 'ok' | 'not_found' | 'no_winner' (no participants, prize carries over)
        | 'conflict' (payout already recorded with a different transaction)."""
        with self._lock, self.db:
            r = self.db.execute("SELECT payout_tx, winner FROM draws WHERE day=?", (day,)).fetchone()
            if r is None:
                return "not_found"
            if not r["winner"]:
                return "no_winner"
            if r["payout_tx"] and r["payout_tx"] != tx:
                return "conflict"
            self.db.execute("UPDATE draws SET prize_amount=?, prize_currency=?, payout_tx=?, paid_at=? WHERE day=?",
                            (str(amount), currency, tx, int(time.time()), day))
            return "ok"
