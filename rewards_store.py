"""Rewards & Burns storage: SQLite, the same file as the draw (DRAW_DB_PATH, default ./data/draw.db),
its own tables with the rw_ prefix:

  rw_draws         — draws (key: UTC day of weights 'YYYY-MM-DD'): the day's blocks, seed, list_hash, winner, payout;
  rw_participants  — draw participants with weights (for verification);
  rw_burns         — dev burns (key: transaction + log index);
  rw_meta          — internal: burn cursor, DEV_WALLETS set, burned supply, token launch, decimals.
Everything is idempotent: writing the same day or the same transaction again duplicates nothing.
No DB: draws are recomputed from the blockchain deterministically (rewards_service.compute_day).
"""
import json, os, time

import db

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
# weights, sums and counts are stored as strings: 18 decimals do not fit in INTEGER (int64)
BIG = ("r", "winner_weight", "total_weight", "payout_amount", "amount")


def _row(r):
    return {k: (int(r[k]) if k in BIG and r[k] is not None else r[k]) for k in r.keys()} if r else None


class RewardsStore:
    def __init__(self, path=None):
        self.path = path or os.environ.get("DRAW_DB_PATH") or DEFAULT_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = db.lock_for(self.path)   # one lock per file for all stores
        self.db = db.connect(self.path)
        with self._lock, self.db:
            self.db.executescript(SCHEMA)
            # DB from before ETH payouts: no currency column; old payouts (NULL) are in the token
            if "payout_currency" not in {r["name"] for r in self.db.execute("PRAGMA table_info(rw_draws)")}:
                self.db.execute("ALTER TABLE rw_draws ADD COLUMN payout_currency TEXT")

    def close(self):
        with self._lock:
            self.db.close()

    # ---------- internal ----------
    def meta(self, key, default=None):
        with self._lock:
            r = self.db.execute("SELECT value FROM rw_meta WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    def set_meta(self, key, value):
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO rw_meta(key, value) VALUES (?,?)", (key, json.dumps(value)))

    # ---------- draws ----------
    def get_draw(self, day):
        with self._lock:
            return _row(self.db.execute("SELECT * FROM rw_draws WHERE day=?", (day,)).fetchone())

    def save_draw(self, rec, participants):
        """The day's draw and participants in one transaction, once. -> True if written now."""
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
        """Page of draw history: days <= max_day (None means unbounded), newest first, limit + 1 rows
        (the extra one signals a next page)."""
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
        """{(tx, log_index)} of payouts already recorded."""
        with self._lock:
            return {(r["payout_tx"], r["payout_log_index"]) for r in self.db.execute(
                "SELECT payout_tx, payout_log_index FROM rw_draws WHERE payout_tx IS NOT NULL")}

    def set_payout(self, day, t, ts):
        """Draw payout: transfer t (from the blockchain), token or ETH (t["currency"], amount in wei / token base
        units). Already recorded: not changed. -> True if written now."""
        with self._lock, self.db:
            cur = self.db.execute(
                "UPDATE rw_draws SET payout_amount=?, payout_tx=?, payout_log_index=?, payout_block=?, payout_from=?, "
                "paid_at=?, payout_currency=? WHERE day=? AND winner IS NOT NULL AND payout_tx IS NULL",
                (str(t["amount"]), t["tx"], t["log_index"], t["block"], t["frm"], int(ts),
                 t.get("currency") or "CRAWLSCAN", day))
            return cur.rowcount > 0

    # ---------- burns ----------
    def save_burns(self, token, burns, ts_of):
        """-> how many new ones were written (a repeat of the same transaction and log is not duplicated)."""
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
        """Page of burn history: time < before (None means unbounded), newest first. Burns with the same
        time are not split across pages (the cursor is time): a page pulls in all rows with the last time,
        so it may be longer than limit. -> (rows, whether there are more)."""
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
        """(total burned by the dev, number of burns)."""
        with self._lock:
            rows = self.db.execute("SELECT amount FROM rw_burns WHERE token=?", (token,)).fetchall()
        return sum(int(r["amount"]) for r in rows), len(rows)

    def clear_burns(self, token):
        with self._lock, self.db:
            self.db.execute("DELETE FROM rw_burns WHERE token=?", (token,))
