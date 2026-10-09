"""Premium: SQLite, своя база (premium.db, premium.db_path()), не draw.db: своё соединение и свой замок, ни одной общей
блокировки с розыгрышем, наградами и alerts. Храним только Telegram ID, адрес кошелька, время, хеш транзакции
верификации и баланс.

  premium_reservations — брони: один кошелёк — одна бронь, у пользователя одна бронь; живёт RESERVE_MIN минут.
  premium_links        — привязки: один кошелёк — один Telegram ID, один Telegram ID — один кошелёк.
  premium_prefs        — /digest on|off (нет строки — сводка включена).
  premium_digest_base  — снимок alerts каждого токена на момент прошлой утренней сводки (с чем сравнивать «за сутки»).
  premium_meta         — день последней сводки (после рестарта второй раз не уходит).
"""
import json, os, threading

import db
import premium

SCHEMA = """
CREATE TABLE IF NOT EXISTS premium_reservations (
  wallet TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE,
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, scanned_to INTEGER);
CREATE TABLE IF NOT EXISTS premium_links (
  wallet TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE, linked_at INTEGER NOT NULL, verify_tx TEXT NOT NULL,
  balance TEXT NOT NULL, checked_at INTEGER NOT NULL, next_check_at INTEGER NOT NULL,
  premium INTEGER NOT NULL, below_since INTEGER, downgraded INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS premium_links_due ON premium_links(next_check_at);
CREATE TABLE IF NOT EXISTS premium_prefs (user_id INTEGER PRIMARY KEY, digest INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS premium_digest_base (token TEXT PRIMARY KEY, ts INTEGER NOT NULL, snap TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS premium_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
LINK_FIELDS = ("premium", "below_since", "downgraded")


class PremiumStore:
    def __init__(self, path=None):
        self.path = path or premium.db_path()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.RLock()        # свой замок: с draw.db ничего общего
        self.db = db.connect(self.path)
        with self._lock, self.db:
            self.db.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self.db.close()

    def _one(self, sql, args):
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    # --- брони --------------------------------------------------------------------------------------

    def reserve(self, user_id, wallet, now, minutes=premium.RESERVE_MIN):
        """Забронировать кошелёк за пользователем. → (состояние, бронь | привязка | None):
        "reserved" — новая бронь (прежняя бронь пользователя на другой кошелёк снимается);
        "pending" — этот кошелёк уже забронирован им же (прежняя бронь); "yours" — уже привязан к нему;
        "taken" — забронирован или привязан к другому аккаунту (кто первый, того и кошелёк)."""
        with self._lock, self.db:
            link = self._one("SELECT * FROM premium_links WHERE wallet = ?", (wallet,))
            if link:
                return ("yours" if link["user_id"] == user_id else "taken"), (link if link["user_id"] == user_id else None)
            res = self._one("SELECT * FROM premium_reservations WHERE wallet = ?", (wallet,))
            if res:
                return ("pending", res) if res["user_id"] == user_id else ("taken", None)
            self.db.execute("DELETE FROM premium_reservations WHERE user_id = ?", (user_id,))
            self.db.execute("INSERT INTO premium_reservations(wallet, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                            (wallet, user_id, now, now + minutes * 60))
            return "reserved", self._one("SELECT * FROM premium_reservations WHERE wallet = ?", (wallet,))

    def reservations(self):
        with self._lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM premium_reservations ORDER BY created_at")]

    def reservation_of(self, user_id):
        with self._lock:
            return self._one("SELECT * FROM premium_reservations WHERE user_id = ?", (user_id,))

    def set_scanned(self, wallets, block):
        """Логи по этим броням прочитаны до блока block (после рестарта чтение продолжается отсюда)."""
        with self._lock, self.db:
            self.db.executemany("UPDATE premium_reservations SET scanned_to = ? WHERE wallet = ?",
                                [(block, w) for w in wallets])

    def drop_reservation(self, wallet):
        with self._lock, self.db:
            return self.db.execute("DELETE FROM premium_reservations WHERE wallet = ?", (wallet,)).rowcount > 0

    # --- привязки -----------------------------------------------------------------------------------

    def link(self, reservation, tx, balance, threshold_raw, now):
        """Бронь подтверждена покупкой: привязать кошелёк. Прежний кошелёк пользователя отвязывается.
        Баланс ниже порога: премиума нет; если премиум (или его последние дни) был у прежнего кошелька —
        Watchlist остаётся на GRACE_DAYS. → (привязка, прежний кошелёк | None) или (None, None), если брони уже нет."""
        user_id, wallet = reservation["user_id"], reservation["wallet"]
        with self._lock, self.db:
            res = self._one("SELECT * FROM premium_reservations WHERE wallet = ? AND user_id = ?", (wallet, user_id))
            if not res:
                return None, None
            old = self._one("SELECT * FROM premium_links WHERE user_id = ?", (user_id,))
            was = bool(old and (old["premium"] or premium.in_grace(old, now)))
            if balance >= threshold_raw:
                fields = {"premium": 1, "below_since": None, "downgraded": 0}
            elif was:
                fields = {"premium": 0, "below_since": (old["below_since"] or now), "downgraded": 0}
            else:
                fields = {"premium": 0, "below_since": None, "downgraded": 1}
            self.db.execute("DELETE FROM premium_links WHERE user_id = ?", (user_id,))
            self.db.execute("DELETE FROM premium_reservations WHERE wallet = ?", (wallet,))
            self.db.execute(
                "INSERT INTO premium_links(wallet, user_id, linked_at, verify_tx, balance, checked_at, next_check_at, "
                "premium, below_since, downgraded) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (wallet, user_id, now, tx, str(balance), now, now + premium.CHECK_EVERY,
                 fields["premium"], fields["below_since"], fields["downgraded"]))
            return self._link_row("wallet", wallet), (old["wallet"] if old else None)

    def _link_row(self, key, value):
        row = self._one(f"SELECT * FROM premium_links WHERE {key} = ?", (value,))
        if row:
            row["balance"] = int(row["balance"])
        return row

    def link_of(self, user_id):
        with self._lock:
            return self._link_row("user_id", user_id)

    def link_by_wallet(self, wallet):
        with self._lock:
            return self._link_row("wallet", wallet)

    def unlink(self, user_id):
        """Отвязать кошелёк пользователя (и снять его бронь). → прежняя привязка | None."""
        with self._lock, self.db:
            row = self._link_row("user_id", user_id)
            self.db.execute("DELETE FROM premium_links WHERE user_id = ?", (user_id,))
            self.db.execute("DELETE FROM premium_reservations WHERE user_id = ?", (user_id,))
            return row

    def unlink_wallet(self, wallet):
        """Админ: отвязать кошелёк (и снять бронь на него). → прежняя привязка | None."""
        with self._lock, self.db:
            row = self._link_row("wallet", wallet)
            self.db.execute("DELETE FROM premium_links WHERE wallet = ?", (wallet,))
            self.db.execute("DELETE FROM premium_reservations WHERE wallet = ?", (wallet,))
            return row

    def due(self, now, limit=500):
        """Привязки, которым пора проверить баланс (давно проверенные первыми)."""
        with self._lock:
            rows = [dict(r) for r in self.db.execute(
                "SELECT * FROM premium_links WHERE next_check_at <= ? ORDER BY next_check_at LIMIT ?", (now, limit))]
        for r in rows:
            r["balance"] = int(r["balance"])
        return rows

    def checked(self, wallet, balance, now, fields):
        """Записать проверку баланса и поля (premium, below_since, downgraded); следующая — через CHECK_EVERY."""
        sets = ["balance = ?", "checked_at = ?", "next_check_at = ?"] + [f"{k} = ?" for k in fields if k in LINK_FIELDS]
        args = [str(balance), now, now + premium.CHECK_EVERY] + [fields[k] for k in fields if k in LINK_FIELDS]
        with self._lock, self.db:
            self.db.execute(f"UPDATE premium_links SET {', '.join(sets)} WHERE wallet = ?", args + [wallet])

    def watch_terms(self, user_id, now):
        """(лимит, дней | None) Watchlist пользователя — см. premium.watch_terms."""
        return premium.watch_terms(self.link_of(user_id), now)

    def premium_users(self):
        """Telegram ID с активным премиумом (баланс ≥ порога)."""
        with self._lock:
            return [r["user_id"] for r in self.db.execute(
                "SELECT user_id FROM premium_links WHERE premium = 1 ORDER BY linked_at")]

    # --- утренняя сводка -----------------------------------------------------------------------------

    def digest_on(self, user_id):
        """Включена ли сводка у пользователя (по умолчанию — да)."""
        with self._lock:
            r = self.db.execute("SELECT digest FROM premium_prefs WHERE user_id = ?", (user_id,)).fetchone()
        return True if r is None else bool(r["digest"])

    def set_digest(self, user_id, on):
        with self._lock, self.db:
            self.db.execute("INSERT INTO premium_prefs(user_id, digest) VALUES (?, ?) "
                            "ON CONFLICT(user_id) DO UPDATE SET digest = excluded.digest", (user_id, int(bool(on))))

    def digest_bases(self, tokens):
        """{токен: снимок на момент прошлой сводки}."""
        tokens = list(tokens)
        out = {}
        with self._lock:
            for i in range(0, len(tokens), 500):
                part = tokens[i:i + 500]
                for r in self.db.execute(f"SELECT token, snap FROM premium_digest_base WHERE token IN "
                                         f"({','.join('?' * len(part))})", part):
                    out[r["token"]] = json.loads(r["snap"])
        return out

    def set_digest_bases(self, snaps):
        """{токен: снимок} — база для следующей сводки."""
        with self._lock, self.db:
            self.db.executemany("INSERT OR REPLACE INTO premium_digest_base(token, ts, snap) VALUES (?, ?, ?)",
                                [(t, int(s.get("ts") or 0), json.dumps(s, separators=(",", ":")))
                                 for t, s in snaps.items()])

    def meta(self, key):
        with self._lock:
            r = self.db.execute("SELECT value FROM premium_meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key, value):
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO premium_meta(key, value) VALUES (?, ?)", (key, str(value)))
