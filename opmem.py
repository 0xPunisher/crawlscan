"""Operator memory: we accumulate which wallets and operators show up in which tokens. Write-only -- does not
affect the verdict. Disabled by default (OPMEM_ENABLED); when disabled -- the database is not opened, the thread does not start.

Own SQLite database (OPMEM_DB_PATH, default memory.db next to DRAW_DB_PATH), own connection, WAL -- not a single
lock shared with draw.db. The scan puts the finished result into a bounded queue (record: O(1), no database) and moves
on; parsing and writing happen in the background opmem-writer thread. Queue full -- the result is lost (a log line at most
once a minute). Any write error -- just a log line. No new network requests: everything comes from the scan result.

One token -- at most once per OPMEM_MIN_INTERVAL_H hours (6 by default), wherever the scan came from (live or
alerts recheck). TOO_ESTABLISHED and TOO_ACTIVE are not written; limited / partial scan -- with a flag. A partial scan (Bankr:
holder index is being built) does not close the interval for a full one: a full scan of the same token after it is written.

Tables:
  scans(token, chain, launchpad, ts, band, score, limited, partial, holders_total, unread, deployer,
        mcap_usd, fdv_usd, price_usd, liquidity_usd, market_source, rug_drop, name, ticker)
  operators(token, ts, operator_id, wallets JSON, n_wallets, level, kinds, share, share_supply)
  wallets(wallet, token, ts, roles, operator_id, share, share_supply)
operator_id -- sha1 of the sorted wallets (first 16 hex): the same set of wallets in another token -- the same id.
roles -- comma-separated: top_holder, dev, early_buyer (entry within the launch bundle window), sniper, virgin,
transfer_received, linked (in an operator of >= 2 wallets or a token distributor of such an operator).
Bankr: dev -- deployer (LP fee recipient) and vesting beneficiaries; dev share outside the top -- from dev_holding.
Shares: share -- of turnover, share_supply -- of supply.
Market at scan time (mcap_usd, fdv_usd, price_usd, liquidity_usd, market_source, name and ticker -- from the result header,
rug_drop -- the probably rug drop, if any) -- for Rug Replay (replay.py). An old database gets the columns added on
open (migrate), rows are left untouched.
"""
import hashlib, json, os, queue, sqlite3, threading, time

import db
from draw_store import DEFAULT_PATH as DRAW_DEFAULT_PATH

QUEUE_MAX = 500            # results waiting to be written; more -- dropped
DROP_LOG_EVERY = 60        # seconds: overflow log line at most this often
MIN_INTERVAL_H = 6.0       # hours between writes of the same token (env OPMEM_MIN_INTERVAL_H)
SKIP_BANDS = ("TOO_ESTABLISHED", "TOO_ACTIVE")   # no verdict: no holders or operators

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
  token TEXT NOT NULL, chain TEXT NOT NULL, launchpad TEXT, ts INTEGER NOT NULL, band TEXT NOT NULL, score INTEGER,
  limited INTEGER NOT NULL, partial INTEGER NOT NULL, holders_total INTEGER, unread INTEGER, deployer TEXT);
CREATE INDEX IF NOT EXISTS scans_token ON scans(token, ts);
CREATE INDEX IF NOT EXISTS scans_ts ON scans(ts);
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
# scans columns added after the first version (Rug Replay): market at scan time; old database -- ALTER TABLE
MARKET_COLUMNS = (("mcap_usd", "REAL"), ("fdv_usd", "REAL"), ("price_usd", "REAL"), ("liquidity_usd", "REAL"),
                  ("market_source", "TEXT"), ("rug_drop", "REAL"), ("name", "TEXT"), ("ticker", "TEXT"))
SCAN_COLUMNS = ("token", "chain", "launchpad", "ts", "band", "score", "limited", "partial", "holders_total", "unread",
                "deployer") + tuple(c for c, _ in MARKET_COLUMNS)


def migrate(con):
    """Operator memory schema on connection con: tables, indexes and missing scans columns (data untouched)."""
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(scans)")}
    for col, typ in MARKET_COLUMNS:
        if col not in have:
            try:
                con.execute(f"ALTER TABLE scans ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as e:   # another connection (Rug Replay thread) got there first
                if "duplicate column" not in str(e):
                    raise


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
    """Whether to write the result: has a token and a verdict, not TOO_ESTABLISHED / TOO_ACTIVE."""
    return isinstance(result, dict) and bool(result.get("token")) and bool(result.get("band")) \
        and result["band"] not in SKIP_BANDS


def operator_id(wallets):
    return hashlib.sha1(",".join(sorted(wallets)).encode()).hexdigest()[:16]


def _f(x):
    return round(float(x), 6) if isinstance(x, (int, float)) else None


def _usd(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _txt(x, n=64):
    return x.strip()[:n] or None if isinstance(x, str) else None


def rows(result, ts):
    """engine.scan result -> (scan, [operators], [wallets]) for writing. Pure function, no network."""
    token = result["token"]
    launch = result.get("launch") or {}
    dev = launch.get("deployer")
    score = result.get("score")
    scan = {"token": token, "chain": result.get("chain") or "robinhood", "launchpad": result.get("launchpad"),
            "ts": ts, "band": result["band"], "score": int(score) if isinstance(score, (int, float)) else None,
            "limited": int(bool(result.get("limited"))), "partial": int(bool(result.get("partial_scan"))),
            "holders_total": result.get("holders_total"), "unread": len(result.get("unread") or []), "deployer": dev}
    head, rug = result.get("header") or {}, result.get("rug") or {}
    scan |= {"mcap_usd": _usd(head.get("mcap_usd")), "fdv_usd": _usd(head.get("fdv_usd")),
             "price_usd": _usd(head.get("price_usd")), "liquidity_usd": _usd(head.get("liquidity_usd")),
             "market_source": result.get("market_source"), "rug_drop": _f(rug.get("drop")) if rug else None,
             "name": _txt(head.get("name")), "ticker": _txt(head.get("ticker"), 32)}

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
        for l in links:      # the operator's token distributor is its wallet too
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
        bought = s.get("kind") not in ("vesting", "fees")   # Bankr: vesting allocation / LP fees -- not a buy
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
        add(b, "dev")   # beneficiary of dev vesting on the token contract
    dh = bk.get("dev_holding") or {}
    if dh.get("wallet") in wal and wal[dh["wallet"]]["share_supply"] is None:
        wal[dh["wallet"]]["share_supply"] = _f(dh.get("share_supply"))   # wallet + vesting, dev not in the top
    for w in op_of:
        add(w, "linked")
    for e in wal.values():
        e["roles"] = ",".join(e["roles"])
    return scan, ops, list(wal.values())


class Store:
    """Own operator memory database: own connection, only in the writer thread."""

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = db.connect(path)
        with self.db:
            migrate(self.db)

    def close(self):
        self.db.close()

    def last_ts(self, token, full_only=False):
        """Time of the token's last write; full_only -- only full scans (not partial)."""
        q = "SELECT MAX(ts) FROM scans WHERE token = ?" + (" AND partial = 0" if full_only else "")
        return self.db.execute(q, (token,)).fetchone()[0]

    def write(self, result, ts, interval):
        """Write a scan; the token was written less than interval seconds ago -- False (a full scan after a partial one
        is written: a partial does not close the interval)."""
        last = self.last_ts(result["token"], full_only=not result.get("partial_scan"))
        if last is not None and ts - last < interval:
            return False
        scan, ops, wal = rows(result, ts)
        with self.db:
            self.db.execute(f"INSERT INTO scans ({', '.join(SCAN_COLUMNS)}) "
                            f"VALUES ({', '.join(':' + c for c in SCAN_COLUMNS)})", scan)
            self.db.executemany("INSERT INTO operators VALUES (:token, :ts, :operator_id, :wallets, :n_wallets, "
                                ":level, :kinds, :share, :share_supply)", ops)
            self.db.executemany("INSERT INTO wallets VALUES (:wallet, :token, :ts, :roles, :operator_id, :share, "
                                ":share_supply)", wal)
        return True


class Writer:
    """Bounded queue and background writer thread. push -- no database and no waiting."""

    def __init__(self, path, interval=None, clock=time.time, qmax=QUEUE_MAX):
        self.path, self.clock = path, clock
        self.interval = min_interval_s() if interval is None else interval
        self.q = queue.Queue(maxsize=qmax)
        self._lock = threading.Lock()
        self._seen = {}               # token -> (time last queued, partial) -- dedup before the database
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
        """-> True if the result was queued. Never raises and never waits."""
        try:
            if not worth(result):
                return False
            now, token, partial = self.clock(), result["token"], bool(result.get("partial_scan"))
            with self._lock:
                last = self._seen.get(token)
                if last is not None and now - last[0] < self.interval and (partial or not last[1]):
                    return False      # partial after partial, or anything after full
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
    """Scan finished -> result into the operator memory queue (only with OPMEM_ENABLED). Does not raise, does not wait."""
    if not enabled():
        return False
    try:
        return writer().push(result)
    except Exception as e:
        log(f"opmem: push failed: {type(e).__name__}: {e}")
        return False
