"""Telegram alerts: SQLite, тот же файл, что у розыгрыша (DRAW_DB_PATH, по умолчанию ./data/draw.db),
таблица alert_snapshots. Один токен — одна строка: последний снимок (cur) и предыдущий (prev), JSON.
"""
import json, os, sqlite3, threading

from draw_store import DEFAULT_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_snapshots (
  token TEXT PRIMARY KEY, chain TEXT NOT NULL, ts INTEGER NOT NULL, cur TEXT NOT NULL, prev TEXT);
"""


class AlertsStore:
    def __init__(self, path=None):
        self.path = path or os.environ.get("DRAW_DB_PATH") or DEFAULT_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock, self.db:
            self.db.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self.db.close()

    def record(self, snap):
        """Сохранить снимок: прежний последний становится предыдущим. → (prev, cur)."""
        with self._lock, self.db:
            row = self.db.execute("SELECT cur FROM alert_snapshots WHERE token = ?", (snap["token"],)).fetchone()
            prev = row["cur"] if row else None
            self.db.execute("INSERT OR REPLACE INTO alert_snapshots(token, chain, ts, cur, prev) VALUES (?, ?, ?, ?, ?)",
                            (snap["token"], snap["chain"], snap["ts"], json.dumps(snap, separators=(",", ":")), prev))
        return (json.loads(prev) if prev else None), snap

    def get(self, token):
        """(prev, cur) по токену; нет снимков — (None, None)."""
        with self._lock:
            row = self.db.execute("SELECT cur, prev FROM alert_snapshots WHERE token = ?", (token,)).fetchone()
        if not row:
            return None, None
        return (json.loads(row["prev"]) if row["prev"] else None), json.loads(row["cur"])
