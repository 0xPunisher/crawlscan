"""The "Recently scanned" feed: SQLite, the same file as the draw (DRAW_DB_PATH, default ./data/draw.db),
table recent_scans. One token, one row: a new scan of the same token updates it and moves it to the top.
Only a completed scan with a verdict is written (errors and "not a token address" never get here).
launchpad: "flap" / "bankr" for Flap and Bankr tokens (result["launchpad"]), else NULL; the column is added to an old DB at startup.
"""
import os, time

import db

from draw_store import DEFAULT_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS recent_scans (
  token TEXT PRIMARY KEY, chain TEXT NOT NULL, ticker TEXT, name TEXT,
  score INTEGER, band TEXT NOT NULL, rug INTEGER NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS recent_scans_ts ON recent_scans(ts DESC);
"""
MAX_LIMIT = 50


def entry(result, ts=None):
    """engine.scan result -> feed row, or None (no token or verdict)."""
    if not isinstance(result, dict) or not result.get("token") or not result.get("band"):
        return None
    h = result.get("header") or {}
    score = result.get("score")
    return {"token": result["token"], "chain": result.get("chain") or "robinhood", "launchpad": result.get("launchpad"),
            "ticker": h.get("ticker") or None, "name": h.get("name") or None,
            "score": int(score) if isinstance(score, (int, float)) else None, "band": result["band"],
            "rug": bool(result.get("rug")), "ts": int(ts if ts is not None else time.time())}


class RecentStore:
    def __init__(self, path=None):
        self.path = path or os.environ.get("DRAW_DB_PATH") or DEFAULT_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = db.lock_for(self.path)   # one lock per file for all stores
        self.db = db.connect(self.path)
        with self._lock, self.db:
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(recent_scans)")}
            if "launchpad" not in cols:
                self.db.execute("ALTER TABLE recent_scans ADD COLUMN launchpad TEXT")

    def close(self):
        with self._lock:
            self.db.close()

    def record(self, result, ts=None):
        """Save a scan (or update the token's row). -> the row, or None if there is nothing to save."""
        e = entry(result, ts)
        if e is None:
            return None
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO recent_scans(token, chain, launchpad, ticker, name, score, band, rug, ts) "
                            "VALUES (:token, :chain, :launchpad, :ticker, :name, :score, :band, :rug, :ts)", {**e, "rug": int(e["rug"])})
        return e

    def recent(self, limit=12):
        """Latest unique tokens, newest on top."""
        limit = min(MAX_LIMIT, max(1, int(limit)))
        with self._lock:
            rows = self.db.execute("SELECT * FROM recent_scans ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "rug": bool(r["rug"])} for r in rows]
