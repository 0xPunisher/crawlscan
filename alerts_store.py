"""Telegram alerts: SQLite, тот же файл, что у розыгрыша (DRAW_DB_PATH, по умолчанию ./data/draw.db),
таблицы alert_snapshots (один токен — одна строка: последний снимок cur и предыдущий prev, JSON) и
alert_watches (подписки чатов: до WATCH_LIMIT токенов на чат, живут WATCH_DAYS дней).
"""
import json, os, sqlite3, threading, time

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
WATCH_COLS = "token, chain, created_at, expires_at"


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

    def set_early(self, token, share):
        """Дописать долю ранних покупателей (посчитал /api/early) в последний снимок токена. → есть ли снимок."""
        with self._lock, self.db:
            row = self.db.execute("SELECT cur FROM alert_snapshots WHERE token = ?", (token,)).fetchone()
            if not row:
                return False
            cur = json.loads(row["cur"]) | {"early_share": round(float(share), ROUND)}
            self.db.execute("UPDATE alert_snapshots SET cur = ? WHERE token = ?",
                            (json.dumps(cur, separators=(",", ":")), token))
        return True

    # --- подписки -------------------------------------------------------------------------------------

    def _watches(self, chat_id, now):
        rows = self.db.execute(f"SELECT {WATCH_COLS} FROM alert_watches WHERE chat_id = ? AND expires_at > ? "
                               "ORDER BY created_at, rowid", (chat_id, now)).fetchall()
        return [dict(r) for r in rows]

    def watch(self, chat_id, token, chain, now=None, limit=WATCH_LIMIT, days=WATCH_DAYS):
        """Подписать чат на токен на days дней. Уже подписан — продлить (created_at прежний).
        → ("ok" | "renewed", подписка, все подписки чата) или ("limit", None, все подписки чата)."""
        now = int(now if now is not None else time.time())
        with self._lock, self.db:
            self.db.execute("DELETE FROM alert_watches WHERE expires_at <= ?", (now,))
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
        """Отписать чат от токена. → была ли подписка."""
        with self._lock, self.db:
            return self.db.execute("DELETE FROM alert_watches WHERE chat_id = ? AND token = ?",
                                   (chat_id, token)).rowcount > 0

    def watches(self, chat_id, now=None):
        """Действующие подписки чата, старые первыми."""
        with self._lock:
            return self._watches(chat_id, int(now if now is not None else time.time()))
