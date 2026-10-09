"""Память операторов: копим, какие кошельки и операторы встречаются в каких токенах. Только запись — в вердикт
не идёт. Выключено по умолчанию (OPMEM_ENABLED); выключено — база не открывается, поток не стартует.

Своя база SQLite (OPMEM_DB_PATH, по умолчанию memory.db рядом с DRAW_DB_PATH), своё соединение, WAL — ни одной
общей блокировки с draw.db. Скан кладёт готовый результат в ограниченную очередь (record: O(1), без базы) и идёт
дальше; разбор и запись — в фоновом потоке opmem-writer. Очередь полна — результат теряется (строка в лог не
чаще раза в минуту). Любая ошибка записи — только строка в лог. Новых запросов в сеть нет: всё из результата скана.

Один токен — не чаще раза в OPMEM_MIN_INTERVAL_H часов (по умолчанию 6), откуда бы ни пришёл скан (живой или
перепроверка alerts). TOO_ESTABLISHED не пишется; limited / частичный скан — с пометкой. Частичный скан (Bankr:
индекс холдеров строится) не закрывает интервал для полного: полный скан того же токена после него пишется.

Таблицы:
  scans(token, chain, launchpad, ts, band, score, limited, partial, holders_total, unread, deployer)
  operators(token, ts, operator_id, wallets JSON, n_wallets, level, kinds, share, share_supply)
  wallets(wallet, token, ts, roles, operator_id, share, share_supply)
operator_id — sha1 отсортированных кошельков (первые 16 hex): тот же набор кошельков в другом токене — тот же id.
roles — через запятую: top_holder, dev, early_buyer (вход в окне бандла запуска), sniper, virgin,
transfer_received, linked (в операторе из ≥ 2 кошельков или раздатчик токена такого оператора).
Bankr: dev — деплоер (получатель комиссий LP) и бенефициары вестинга; доля дева вне топа — из dev_holding.
Доли: share — от оборота, share_supply — от сапплая.
"""
import hashlib, json, os, queue, sqlite3, threading, time

import db
from draw_store import DEFAULT_PATH as DRAW_DEFAULT_PATH

QUEUE_MAX = 500            # результатов ждут записи; больше — отбрасываются
DROP_LOG_EVERY = 60        # секунд: строка о переполнении не чаще
MIN_INTERVAL_H = 6.0       # часов между записями одного токена (env OPMEM_MIN_INTERVAL_H)
SKIP_BANDS = ("TOO_ESTABLISHED",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  token TEXT NOT NULL, chain TEXT NOT NULL, launchpad TEXT, ts INTEGER NOT NULL, band TEXT NOT NULL, score INTEGER,
  limited INTEGER NOT NULL, partial INTEGER NOT NULL, holders_total INTEGER, unread INTEGER, deployer TEXT);
CREATE INDEX IF NOT EXISTS scans_token ON scans(token, ts);
CREATE TABLE IF NOT EXISTS operators (
  token TEXT NOT NULL, ts INTEGER NOT NULL, operator_id TEXT NOT NULL, wallets TEXT NOT NULL, n_wallets INTEGER NOT NULL,
  level TEXT, kinds TEXT, share REAL, share_supply REAL);
CREATE INDEX IF NOT EXISTS operators_token ON operators(token);
CREATE INDEX IF NOT EXISTS operators_id ON operators(operator_id);
CREATE TABLE IF NOT EXISTS wallets (
  wallet TEXT NOT NULL, token TEXT NOT NULL, ts INTEGER NOT NULL, roles TEXT NOT NULL, operator_id TEXT,
  share REAL, share_supply REAL);
CREATE INDEX IF NOT EXISTS wallets_wallet ON wallets(wallet);
CREATE INDEX IF NOT EXISTS wallets_token ON wallets(token);
"""


def enabled():
    return os.environ.get("OPMEM_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def db_path():
    p = os.environ.get("OPMEM_DB_PATH", "").strip()
    if p:
        return p
    draw = os.environ.get("DRAW_DB_PATH", "").strip() or DRAW_DEFAULT_PATH
    return os.path.join(os.path.dirname(os.path.abspath(draw)), "memory.db")


def min_interval_s():
    try:
        h = float(os.environ.get("OPMEM_MIN_INTERVAL_H", "").strip())
        return (h if h >= 0 else MIN_INTERVAL_H) * 3600
    except ValueError:
        return MIN_INTERVAL_H * 3600


def log(msg):
    print(msg, flush=True)


def worth(result):
    """Писать ли результат: есть токен и вердикт, не TOO_ESTABLISHED."""
    return isinstance(result, dict) and bool(result.get("token")) and bool(result.get("band")) \
        and result["band"] not in SKIP_BANDS


def operator_id(wallets):
    return hashlib.sha1(",".join(sorted(wallets)).encode()).hexdigest()[:16]


def _f(x):
    return round(float(x), 6) if isinstance(x, (int, float)) else None


def rows(result, ts):
    """Результат engine.scan → (scan, [operators], [wallets]) для записи. Чистая функция, без сети."""
    token = result["token"]
    launch = result.get("launch") or {}
    dev = launch.get("deployer")
    score = result.get("score")
    scan = {"token": token, "chain": result.get("chain") or "robinhood", "launchpad": result.get("launchpad"),
            "ts": ts, "band": result["band"], "score": int(score) if isinstance(score, (int, float)) else None,
            "limited": int(bool(result.get("limited"))), "partial": int(bool(result.get("partial_scan"))),
            "holders_total": result.get("holders_total"), "unread": len(result.get("unread") or []), "deployer": dev}

    links = result.get("links") or []
    ops, op_of = [], {}
    for o in result.get("operators") or []:
        ws = [w for w in o.get("wallets") or [] if w]
        if len(ws) < 2:
            continue
        oid, wset = operator_id(ws), set(ws)
        kinds = sorted({l.get("kind") for l in links if l.get("a") in wset and l.get("b") in wset and l.get("kind")})
        ops.append({"token": token, "ts": ts, "operator_id": oid, "wallets": json.dumps(ws), "n_wallets": len(ws),
                    "level": o.get("level"), "kinds": ",".join(kinds),
                    "share": _f(o.get("share")), "share_supply": _f(o.get("share_supply"))})
        for w in ws:
            op_of[w] = oid
        for l in links:      # раздатчик токена оператора — тоже его кошелёк
            if l.get("kind") == "distributor" and l.get("a") in wset and l.get("via"):
                op_of.setdefault(l["via"], oid)

    wal = {}

    def add(w, role, share=None, share_supply=None):
        if not w:
            return
        e = wal.setdefault(w, {"wallet": w, "token": token, "ts": ts, "roles": [], "operator_id": op_of.get(w),
                               "share": None, "share_supply": None})
        if role not in e["roles"]:
            e["roles"].append(role)
        if share is not None:
            e["share"], e["share_supply"] = _f(share), _f(share_supply)

    for h in result.get("holders") or []:
        w, s = h.get("wallet"), h.get("signals") or {}
        add(w, "top_holder", h.get("share"), h.get("share_supply"))
        if s.get("is_deployer"):
            add(w, "dev")
        bought = s.get("kind") not in ("vesting", "fees")   # Bankr: выделение вестинга / комиссии LP — не покупка
        if s.get("launch_bundle") and bought:
            add(w, "early_buyer")
        if s.get("sniper") and bought:
            add(w, "sniper")
        if s.get("virgin"):
            add(w, "virgin")
        if s.get("kind") == "transfer":
            add(w, "transfer_received")
    add(dev, "dev")
    bk = result.get("bankr") or {}
    for b in sorted((bk.get("vesting") or {}).get("beneficiaries") or {}):
        add(b, "dev")   # бенефициар вестинга дева на контракте токена
    dh = bk.get("dev_holding") or {}
    if dh.get("wallet") in wal and wal[dh["wallet"]]["share_supply"] is None:
        wal[dh["wallet"]]["share_supply"] = _f(dh.get("share_supply"))   # кошелёк + вестинг, дев не в топе
    for w in op_of:
        add(w, "linked")
    for e in wal.values():
        e["roles"] = ",".join(e["roles"])
    return scan, ops, list(wal.values())


class Store:
    """Своя база памяти операторов: своё соединение, только в потоке писателя."""

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = db.connect(path)
        with self.db:
            self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def last_ts(self, token, full_only=False):
        """Время последней записи токена; full_only — только полных сканов (не partial)."""
        q = "SELECT MAX(ts) FROM scans WHERE token = ?" + (" AND partial = 0" if full_only else "")
        return self.db.execute(q, (token,)).fetchone()[0]

    def write(self, result, ts, interval):
        """Записать скан; токен уже писали меньше interval секунд назад — False (полный скан после частичного
        пишется: частичный интервал не закрывает)."""
        last = self.last_ts(result["token"], full_only=not result.get("partial_scan"))
        if last is not None and ts - last < interval:
            return False
        scan, ops, wal = rows(result, ts)
        with self.db:
            self.db.execute("INSERT INTO scans VALUES (:token, :chain, :launchpad, :ts, :band, :score, :limited, "
                            ":partial, :holders_total, :unread, :deployer)", scan)
            self.db.executemany("INSERT INTO operators VALUES (:token, :ts, :operator_id, :wallets, :n_wallets, "
                                ":level, :kinds, :share, :share_supply)", ops)
            self.db.executemany("INSERT INTO wallets VALUES (:wallet, :token, :ts, :roles, :operator_id, :share, "
                                ":share_supply)", wal)
        return True


class Writer:
    """Ограниченная очередь и фоновый поток записи. push — без базы и ожидания."""

    def __init__(self, path, interval=None, clock=time.time, qmax=QUEUE_MAX):
        self.path, self.clock = path, clock
        self.interval = min_interval_s() if interval is None else interval
        self.q = queue.Queue(maxsize=qmax)
        self._lock = threading.Lock()
        self._seen = {}               # token -> (время последней постановки в очередь, частичный) — дедупликация до базы
        self.dropped, self._drop_logged = 0, 0.0
        self.written = 0
        self.store = None
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True, name="opmem-writer")
            self.thread.start()
        return self

    def push(self, result):
        """→ True, если результат поставлен в очередь. Никогда не бросает и не ждёт."""
        try:
            if not worth(result):
                return False
            now, token, partial = self.clock(), result["token"], bool(result.get("partial_scan"))
            with self._lock:
                last = self._seen.get(token)
                if last is not None and now - last[0] < self.interval and (partial or not last[1]):
                    return False      # частичный после частичного или любой после полного
                try:
                    self.q.put_nowait((result, int(now)))
                except queue.Full:
                    self.dropped += 1
                    if now - self._drop_logged >= DROP_LOG_EVERY:
                        self._drop_logged = now
                        log(f"opmem: queue full ({self.q.maxsize}), dropped {self.dropped} so far")
                    return False
                self._seen[token] = (now, partial)
                if len(self._seen) > 50_000:
                    self._seen = {t: v for t, v in self._seen.items() if now - v[0] < self.interval}
            return True
        except Exception as e:
            log(f"opmem: push failed: {type(e).__name__}: {e}")
            return False

    def run(self):
        while True:
            result, ts = self.q.get()
            try:
                self.write(result, ts)
            except Exception as e:
                log(f"opmem: not saved {result.get('token')}: {type(e).__name__}: {e}")
            finally:
                self.q.task_done()

    def write(self, result, ts):
        if self.store is None:
            self.store = Store(self.path)
        if self.store.write(result, ts, self.interval):
            self.written += 1


_writer = {"obj": None}
_writer_lock = threading.Lock()


def writer():
    with _writer_lock:
        if _writer["obj"] is None:
            path = db_path()
            log(f"opmem: writing operator memory to {path}, one write per token per "
                f"{min_interval_s() / 3600:g} h")
            _writer["obj"] = Writer(path).start()
        return _writer["obj"]


def record(result):
    """Скан завершён → результат в очередь памяти операторов (только при OPMEM_ENABLED). Не бросает, не ждёт."""
    if not enabled():
        return False
    try:
        return writer().push(result)
    except Exception as e:
        log(f"opmem: push failed: {type(e).__name__}: {e}")
        return False
