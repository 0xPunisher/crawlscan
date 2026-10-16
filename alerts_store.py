"""Telegram alerts: SQLite, the same file as the draw (DRAW_DB_PATH, default ./data/draw.db),
tables alert_snapshots (one token, one row: last snapshot cur and previous prev, JSON) and
alert_watches (chat subscriptions: up to WATCH_LIMIT tokens per chat, live for WATCH_DAYS days).
"""
import json, os, time

import db

from alerts import ROUND, WATCH_DAYS, WATCH_LIMIT
from draw_store import DEFAULT_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_snapshots (
  token TEXT PRIMARY KEY, chain TEXT NOT NULL, ts INTEGER NOT NULL, cur TEXT NOT NULL, prev TEXT);
CREATE TABLE IF NOT EXISTS alert_watches (
  chat_id INTEGER NOT NULL, token TEXT NOT NULL, chain TEXT NOT NULL,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, PRIMARY KEY (chat_id, token));
CREATE INDEX IF NOT EXISTS alert_watches_token ON alert_watches(token);
"""


class AlertsStore:
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

    def record(self, snap):
        """Save a snapshot: the previous last one becomes previous. -> (prev, cur)."""
        with self._lock, self.db:
            row = self.db.execute("SELECT cur FROM alert_snapshots WHERE token = ?", (snap["token"],)).fetchone()
            prev = row["cur"] if row else None
            self.db.execute("INSERT OR REPLACE INTO alert_snapshots(token, chain, ts, cur, prev) VALUES (?, ?, ?, ?, ?)",
                            (snap["token"], snap["chain"], snap["ts"], json.dumps(snap, separators=(",", ":")), prev))
        return (json.loads(prev) if prev else None), snap

    def get(self, token):
        """(prev, cur) for the token; no snapshots: (None, None)."""
        with self._lock:
            row = self.db.execute("SELECT cur, prev FROM alert_snapshots WHERE token = ?", (token,)).fetchone()
        if not row:
            return None, None
        return (json.loads(row["prev"]) if row["prev"] else None), json.loads(row["cur"])

    def set_early(self, token, share):
        """Add the early buyers' share (computed by /api/early) to the token's last snapshot. -> whether a snapshot exists."""
        with self._lock, self.db:
            row = self.db.execute("SELECT cur FROM alert_snapshots WHERE token = ?", (token,)).fetchone()
            if not row:
                return False
            cur = json.loads(row["cur"]) | {"early_share": round(float(share), ROUND)}
            self.db.execute("UPDATE alert_snapshots SET cur = ? WHERE token = ?",
                            (json.dumps(cur, separators=(",", ":")), token))
        return True

    # --- subscriptions --------------------------------------------------------------------------------

    def _watches(self, chat_id, now):
        """The chat's active subscriptions; ticker from the token's last snapshot (no snapshot: None)."""
        rows = self.db.execute(
            "SELECT w.token, w.chain, w.created_at, w.expires_at, json_extract(s.cur, '$.ticker') AS ticker "
            "FROM alert_watches w LEFT JOIN alert_snapshots s ON s.token = w.token "
            "WHERE w.chat_id = ? AND w.expires_at > ? ORDER BY w.created_at, w.rowid", (chat_id, now)).fetchall()
        return [dict(r) for r in rows]

    def watch(self, chat_id, token, chain, now=None, limit=WATCH_LIMIT, days=WATCH_DAYS):
        """Subscribe the chat to the token for days days. Already subscribed: extend (created_at unchanged).
        -> ("ok" | "renewed", subscription, all chat subscriptions) or ("limit", None, all chat subscriptions)."""
        now = int(now if now is not None else time.time())
        with self._lock, self.db:
            items = self._watches(chat_id, now)
            renewed = any(w["token"] == token for w in items)
            if not renewed and len(items) >= limit:
                return "limit", None, items
            self.db.execute("INSERT INTO alert_watches(chat_id, token, chain, created_at, expires_at) "
                            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(chat_id, token) DO UPDATE SET expires_at = excluded.expires_at",
                            (chat_id, token, chain, now, now + days * 86400))
            items = self._watches(chat_id, now)
        return ("renewed" if renewed else "ok"), next(w for w in items if w["token"] == token), items

    def unwatch(self, chat_id, token):
        """Unsubscribe the chat from the token. -> whether there was a subscription."""
        with self._lock, self.db:
            return self.db.execute("DELETE FROM alert_watches WHERE chat_id = ? AND token = ?",
                                   (chat_id, token)).rowcount > 0

    def watches(self, chat_id, now=None):
        """The chat's active subscriptions, oldest first."""
        with self._lock:
            return self._watches(chat_id, int(now if now is not None else time.time()))

    def watchers(self, token, now=None):
        """chat_ids of the token's active subscribers (expired ones get nothing; expire deletes them, with a message)."""
        now = int(now if now is not None else time.time())
        with self._lock:
            return [r["chat_id"] for r in self.db.execute(
                "SELECT chat_id FROM alert_watches WHERE token = ? AND expires_at > ? ORDER BY rowid",
                (token, now)).fetchall()]

    def expire(self, now=None):
        """Delete expired subscriptions. -> [{"chat_id", "token", "chain", "ticker"}]: who to message."""
        now = int(now if now is not None else time.time())
        with self._lock, self.db:
            rows = [dict(r) for r in self.db.execute(
                "SELECT w.chat_id, w.token, w.chain, json_extract(s.cur, '$.ticker') AS ticker FROM alert_watches w "
                "LEFT JOIN alert_snapshots s ON s.token = w.token WHERE w.expires_at <= ? ORDER BY w.rowid",
                (now,)).fetchall()]
            self.db.execute("DELETE FROM alert_watches WHERE expires_at <= ?", (now,))
        return rows

    def unwatch_token(self, token):
        """Unsubscribe everyone from the token (it became TOO_ESTABLISHED). -> chat_ids of the active subscribers."""
        now = int(time.time())
        with self._lock, self.db:
            chats = [r["chat_id"] for r in self.db.execute(
                "SELECT chat_id FROM alert_watches WHERE token = ? AND expires_at > ? ORDER BY rowid",
                (token, now)).fetchall()]
            self.db.execute("DELETE FROM alert_watches WHERE token = ?", (token,))
        return chats

    def recheck_queue(self, now=None):
        """Tokens with active subscriptions: [{"token", "chain", "ts"}], ts is the time of the last snapshot
        (None if no snapshot); least recently checked (and those without a snapshot) first."""
        now = int(now if now is not None else time.time())
        with self._lock:
            rows = self.db.execute(
                "SELECT w.token, MIN(w.chain) AS chain, s.ts FROM alert_watches w "
                "LEFT JOIN alert_snapshots s ON s.token = w.token WHERE w.expires_at > ? "
                "GROUP BY w.token ORDER BY s.ts IS NOT NULL, s.ts, w.token", (now,)).fetchall()
        return [dict(r) for r in rows]

    def is_watching(self, chat_id, token, now=None):
        now = int(now if now is not None else time.time())
        with self._lock:
            return self.db.execute("SELECT 1 FROM alert_watches WHERE chat_id = ? AND token = ? AND expires_at > ?",
                                   (chat_id, token, now)).fetchone() is not None

    def unwatch_chat(self, chat_id):
        """Delete all of the chat's subscriptions (it blocked the bot). -> how many were deleted."""
        with self._lock, self.db:
            return self.db.execute("DELETE FROM alert_watches WHERE chat_id = ?", (chat_id,)).rowcount
