"""Telegram alerts: SQLite, тот же файл, что у розыгрыша (DRAW_DB_PATH, по умолчанию ./data/draw.db),
таблицы alert_snapshots (один токен — одна строка: последний снимок cur и предыдущий prev, JSON) и
alert_watches (подписки чатов: до WATCH_LIMIT токенов на чат, живут WATCH_DAYS дней).
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
        self._lock = db.lock_for(self.path)   # один замок на файл для всех хранилищ
        self.db = db.connect(self.path)
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
        """Действующие подписки чата; ticker — из последнего снимка токена (нет снимка — None)."""
        rows = self.db.execute(
            "SELECT w.token, w.chain, w.created_at, w.expires_at, json_extract(s.cur, '$.ticker') AS ticker "
            "FROM alert_watches w LEFT JOIN alert_snapshots s ON s.token = w.token "
            "WHERE w.chat_id = ? AND w.expires_at > ? ORDER BY w.created_at, w.rowid", (chat_id, now)).fetchall()
        return [dict(r) for r in rows]

    def watch(self, chat_id, token, chain, now=None, limit=WATCH_LIMIT, days=WATCH_DAYS):
        """Подписать чат на токен на days дней. Уже подписан — продлить (created_at прежний).
        → ("ok" | "renewed", подписка, все подписки чата) или ("limit", None, все подписки чата)."""
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
        """Отписать чат от токена. → была ли подписка."""
        with self._lock, self.db:
            return self.db.execute("DELETE FROM alert_watches WHERE chat_id = ? AND token = ?",
                                   (chat_id, token)).rowcount > 0

    def watches(self, chat_id, now=None):
        """Действующие подписки чата, старые первыми."""
        with self._lock:
            return self._watches(chat_id, int(now if now is not None else time.time()))

    def watchers(self, token, now=None):
        """chat_id действующих подписчиков токена (истёкшие не получают; удаляет их expire — с сообщением)."""
        now = int(now if now is not None else time.time())
        with self._lock:
            return [r["chat_id"] for r in self.db.execute(
                "SELECT chat_id FROM alert_watches WHERE token = ? AND expires_at > ? ORDER BY rowid",
                (token, now)).fetchall()]

    def expire(self, now=None):
        """Удалить истёкшие подписки. → [{"chat_id", "token", "chain", "ticker"}] — кому написать."""
        now = int(now if now is not None else time.time())
        with self._lock, self.db:
            rows = [dict(r) for r in self.db.execute(
                "SELECT w.chat_id, w.token, w.chain, json_extract(s.cur, '$.ticker') AS ticker FROM alert_watches w "
                "LEFT JOIN alert_snapshots s ON s.token = w.token WHERE w.expires_at <= ? ORDER BY w.rowid",
                (now,)).fetchall()]
            self.db.execute("DELETE FROM alert_watches WHERE expires_at <= ?", (now,))
        return rows

    def unwatch_token(self, token):
        """Отписать всех от токена (стал TOO_ESTABLISHED). → chat_id действовавших подписчиков."""
        now = int(time.time())
        with self._lock, self.db:
            chats = [r["chat_id"] for r in self.db.execute(
                "SELECT chat_id FROM alert_watches WHERE token = ? AND expires_at > ? ORDER BY rowid",
                (token, now)).fetchall()]
            self.db.execute("DELETE FROM alert_watches WHERE token = ?", (token,))
        return chats

    def recheck_queue(self, now=None):
        """Токены с действующими подписками: [{"token", "chain", "ts"}], ts — время последнего снимка
        (None — снимка нет); давно проверенные (и без снимка) первыми."""
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
        """Удалить все подписки чата (заблокировал бота). → сколько удалено."""
        with self._lock, self.db:
            return self.db.execute("DELETE FROM alert_watches WHERE chat_id = ?", (chat_id,)).rowcount
