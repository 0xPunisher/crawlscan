"""Rug Replay: tokens CrawlScan flagged DANGER that actually dropped after the scan. Disabled by
default (REPLAY_ENABLED); when disabled -- no thread, no page, no /api/replay.

Source -- operator memory (opmem.py, memory.db): the scans table with the market at scan time. Own replay table in
the same file, own connection (WAL, busy_timeout) -- nothing shared with draw.db. The scan path, bot and draw are untouched:
the replay-check thread does not hit RPC (neither Alchemy nor Solana) -- only the public DexScreener, in batches of up to 30 tokens
per request (/tokens/v1/{chainId}/{a,b,...}; DexScreener limit -- 300 requests per minute, we make a handful,
with PAUSE between requests). Errors -- just a log line.

Every REPLAY_CHECK_MIN minutes (60 by default):
  1. sync: from scans of the last 7 days -- tokens with a DANGER verdict and market cap at scan >= REPLAY_MIN_MCAP (group
     danger; the first such scan in the window is the reference point) and CLEAN / OK tokens with the same threshold (group clean -- for
     stats; a DANGER scan of the same token moves it to danger).
  2. Current market cap from DexScreener: all danger, clean -- at most REPLAY_MAX_CLEAN_PER_HOUR checks per rolling hour.
     Each token -- at most once per REPLAY_CHECK_MIN, only for 7 days after the scan. The minimum market cap over 7 days is stored.
  3. danger token: minimum <= (1 - 0.90) x market cap at scan -> confirmed (time, market cap at confirmation, drop, how many
     hours passed after the scan).

/api/replay: the last 30 confirmed (newest first) and 7-day stats -- share that dropped >= 90% among danger and
clean (only tokens older than 24 h after the scan and checked at least once); ready -- each group has >= 20 tokens.
"""
import collections, os, threading, time

import db
import market
import opmem

WINDOW_S = 7 * 86400       # window: scans over 7 days, checks -- 7 days after the scan
MIN_AGE_S = 24 * 3600      # stats include only tokens older than 24 h after the scan
DROP = 0.90                # drop from market cap at scan that counts as confirmed
LEFT = 1 - DROP + 1e-9     # what is left of market cap at scan: exactly -90% is confirmed too (1 - 0.9 in float < 0.1)
MIN_GROUP = 20             # stats are shown when each group has this many tokens
CHECK_MIN = 60             # minutes between checks (env REPLAY_CHECK_MIN)
MIN_MCAP = 25_000.0        # market cap at scan, USD (env REPLAY_MIN_MCAP)
MAX_CLEAN_PER_HOUR = 300   # clean-token checks per rolling hour (env REPLAY_MAX_CLEAN_PER_HOUR)
BATCH = 30                 # tokens per DexScreener request (/tokens/v1 maximum)
PAUSE = 1.5                # seconds between DexScreener requests
DS_BUDGET = 10.0           # seconds per request (with retries on 429 and network -- market._ds)
FIRST_DELAY = 60           # seconds from server start to the first check
CONFIRMED_LIMIT = 30       # cases in /api/replay
KEEP_S = 30 * 86400        # unconfirmed rows older than this are deleted
DANGER_BANDS, CLEAN_BANDS = ("DANGER",), ("CLEAN", "OK")

SCHEMA = """
CREATE TABLE IF NOT EXISTS replay (
  token TEXT PRIMARY KEY, chain TEXT NOT NULL, launchpad TEXT, grp TEXT NOT NULL, scan_ts INTEGER NOT NULL,
  band TEXT, score INTEGER, rug_drop REAL, name TEXT, ticker TEXT, mcap_scan REAL NOT NULL,
  last_mcap REAL, last_check INTEGER, min_mcap REAL, min_ts INTEGER, checks INTEGER NOT NULL DEFAULT 0,
  confirmed_ts INTEGER, confirmed_mcap REAL, confirmed_drop REAL, hours_after REAL);
CREATE INDEX IF NOT EXISTS replay_due ON replay(grp, last_check);
CREATE INDEX IF NOT EXISTS replay_confirmed ON replay(confirmed_ts);
"""


def log(msg):
    print(msg, flush=True)


def enabled():
    return os.environ.get("REPLAY_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _env_num(name, default, cast=float):
    try:
        v = cast(os.environ.get(name, "").strip())
        return v if v >= 0 else default
    except ValueError:
        return default


def config():
    return {"check_min": max(1, _env_num("REPLAY_CHECK_MIN", CHECK_MIN, int)),
            "min_mcap": _env_num("REPLAY_MIN_MCAP", MIN_MCAP),
            "max_clean_per_hour": _env_num("REPLAY_MAX_CLEAN_PER_HOUR", MAX_CLEAN_PER_HOUR, int)}


def db_path():
    return opmem.db_path()


def fetch_mcaps(chain, tokens, budget=DS_BUDGET):
    """Current market cap of tokens of one chain in a single DexScreener request (up to BATCH addresses) -> {token: mcap}.
    Market cap -- marketCap, otherwise fdv, from the most liquid pair where the token is base (like market._ds_market). Token missing from
    the response -- it is missing from the dict (no data, not zero). Network / HTTP error -- exception."""
    net = market.DS_CHAIN.get(chain)
    if not net or not tokens:
        return {}
    key = str.lower if chain == "robinhood" else (lambda x: x)
    want = {key(t): t for t in tokens}
    f = lambda v: float(v) if v not in (None, "") else None
    best = {}
    for p in market._ds(f"{market.DS}/{net}/{','.join(tokens)}", budget) or []:
        if not isinstance(p, dict) or p.get("chainId") != net:
            continue
        tok = want.get(key((p.get("baseToken") or {}).get("address") or ""))
        if tok is None:
            continue
        mcap = f(p.get("marketCap"))
        if mcap is None:
            mcap = f(p.get("fdv"))
        liq = f((p.get("liquidity") or {}).get("usd")) or 0.0
        if mcap is not None and (tok not in best or liq > best[tok][0]):
            best[tok] = (liq, mcap)
    return {t: m for t, (_, m) in best.items()}


def migrate(con):
    opmem.migrate(con)
    con.executescript(SCHEMA)


class Store:
    """replay table in memory.db: own connection (only in the replay-check thread or within a single API request)."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = db.connect(path)
        with self.db:
            migrate(self.db)

    def close(self):
        self.db.close()

    def sync(self, now, min_mcap):
        """New scans from scans -> replay rows. danger beats clean; a row whose window ended without confirmation
        is replaced by the new scan. -> number of rows added or replaced."""
        q = (f"SELECT token, chain, launchpad, ts, band, score, rug_drop, name, ticker, mcap_usd FROM scans "
             f"WHERE ts >= ? AND mcap_usd >= ? AND band IN ({','.join('?' * len(DANGER_BANDS + CLEAN_BANDS))}) "
             f"ORDER BY ts")
        first = {}
        for r in self.db.execute(q, (now - WINDOW_S, min_mcap, *DANGER_BANDS, *CLEAN_BANDS)):
            grp = "danger" if r["band"] in DANGER_BANDS else "clean"
            have = first.get(r["token"])
            if have is None or (have["grp"] == "clean" and grp == "danger"):
                first[r["token"]] = dict(r) | {"grp": grp}
        n = 0
        with self.db:
            for r in first.values():
                cur = self.db.execute(
                    "INSERT INTO replay (token, chain, launchpad, grp, scan_ts, band, score, rug_drop, name, ticker, "
                    "mcap_scan) VALUES (:token, :chain, :launchpad, :grp, :ts, :band, :score, :rug_drop, :name, "
                    ":ticker, :mcap_usd) "
                    "ON CONFLICT(token) DO UPDATE SET chain = excluded.chain, launchpad = excluded.launchpad, "
                    "grp = excluded.grp, scan_ts = excluded.scan_ts, band = excluded.band, score = excluded.score, "
                    "rug_drop = excluded.rug_drop, name = excluded.name, ticker = excluded.ticker, "
                    "mcap_scan = excluded.mcap_scan, last_mcap = NULL, last_check = NULL, min_mcap = NULL, "
                    "min_ts = NULL, checks = 0, confirmed_ts = NULL, confirmed_mcap = NULL, confirmed_drop = NULL, "
                    "hours_after = NULL "
                    "WHERE replay.confirmed_ts IS NULL AND excluded.scan_ts > replay.scan_ts AND "
                    "((replay.grp = 'clean' AND excluded.grp = 'danger') OR replay.scan_ts < :expired)",
                    r | {"expired": now - WINDOW_S})
                n += cur.rowcount
            self.db.execute("DELETE FROM replay WHERE confirmed_ts IS NULL AND scan_ts < ?", (now - KEEP_S,))
        return n

    def due(self, grp, now, interval, limit):
        """Tokens of the group due for a check: within the 7-day window after the scan, not checked for interval seconds;
        unchecked and longest-unchecked first. -> [(token, chain)]."""
        if limit <= 0:
            return []
        rows = self.db.execute(
            "SELECT token, chain FROM replay WHERE grp = ? AND scan_ts >= ? AND (last_check IS NULL OR last_check <= ?) "
            "ORDER BY last_check IS NOT NULL, last_check, scan_ts LIMIT ?",
            (grp, now - WINDOW_S, now - interval, int(limit)))
        return [(r["token"], r["chain"]) for r in rows]

    def update(self, token, mcap, now):
        """Current market cap of the token: minimum over the window, for danger -- confirmation of a drop >= DROP. -> True if confirmed."""
        r = self.db.execute("SELECT * FROM replay WHERE token = ?", (token,)).fetchone()
        if r is None:
            return False
        if r["min_mcap"] is None or mcap < r["min_mcap"]:
            low, low_ts = mcap, now
        else:
            low, low_ts = r["min_mcap"], r["min_ts"]
        confirm = (r["grp"] == "danger" and r["confirmed_ts"] is None and r["mcap_scan"] > 0
                   and now - r["scan_ts"] <= WINDOW_S and low <= r["mcap_scan"] * LEFT)
        with self.db:
            self.db.execute("UPDATE replay SET last_mcap = ?, last_check = ?, min_mcap = ?, min_ts = ?, "
                            "checks = checks + 1 WHERE token = ?", (mcap, now, low, low_ts, token))
            if confirm:
                self.db.execute("UPDATE replay SET confirmed_ts = ?, confirmed_mcap = ?, confirmed_drop = ?, "
                                "hours_after = ? WHERE token = ?",
                                (now, low, round(1 - low / r["mcap_scan"], 4),
                                 round((now - r["scan_ts"]) / 3600, 1), token))
        return confirm

    def mark(self, token, now):
        """Checked, but DexScreener gave no market cap: leave the minimum alone, back in the queue after the interval."""
        with self.db:
            self.db.execute("UPDATE replay SET last_check = ? WHERE token = ?", (now, token))

    def confirmed(self, limit=CONFIRMED_LIMIT):
        rows = self.db.execute("SELECT * FROM replay WHERE confirmed_ts IS NOT NULL "
                               "ORDER BY confirmed_ts DESC, token LIMIT ?", (int(limit),))
        out = []
        for r in rows:
            now_m = r["last_mcap"]
            out.append({"token": r["token"], "chain": r["chain"], "launchpad": r["launchpad"], "name": r["name"],
                        "ticker": r["ticker"], "band": r["band"], "score": r["score"], "rug_drop": r["rug_drop"],
                        "scan_ts": r["scan_ts"], "mcap_scan": r["mcap_scan"], "mcap_now": now_m,
                        "now_ts": r["last_check"], "mcap_min": r["min_mcap"],
                        "drop": r["confirmed_drop"],
                        "drop_now": round(1 - now_m / r["mcap_scan"], 4) if now_m is not None else None,
                        "confirmed_ts": r["confirmed_ts"], "hours_after": r["hours_after"]})
        return out

    def stats(self, now):
        """Share of tokens with a drop >= DROP over 7 days: danger and clean, only older than MIN_AGE_S after the scan."""
        out = {}
        for grp in ("danger", "clean"):
            n, fell = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(min_mcap <= mcap_scan * ?), 0) FROM replay WHERE grp = ? "
                "AND scan_ts >= ? AND scan_ts <= ? AND min_mcap IS NOT NULL AND mcap_scan > 0",
                (LEFT, grp, now - WINDOW_S, now - MIN_AGE_S)).fetchone()
            out[grp] = {"tokens": n, "fell": fell, "share": round(fell / n, 4) if n else None}
        return {"window_days": WINDOW_S // 86400, "min_age_h": MIN_AGE_S // 3600, "drop": DROP,
                "min_tokens": MIN_GROUP, "ready": all(out[g]["tokens"] >= MIN_GROUP for g in out), **out}

    def since(self):
        return self.db.execute("SELECT MIN(scan_ts) FROM replay").fetchone()[0]


def empty_stats():
    z = {"tokens": 0, "fell": 0, "share": None}
    return {"window_days": WINDOW_S // 86400, "min_age_h": MIN_AGE_S // 3600, "drop": DROP, "min_tokens": MIN_GROUP,
            "ready": False, "danger": dict(z), "clean": dict(z)}


def api(path, now=None):
    """Body of /api/replay. No database yet -- empty (the file is not created)."""
    now = int(now if now is not None else time.time())
    if not os.path.exists(path):
        return {"items": [], "stats": empty_stats(), "since": None}
    store = Store(path)
    try:
        return {"items": store.confirmed(), "stats": store.stats(now), "since": store.since()}
    finally:
        store.close()


class Checker:
    """replay-check thread: every check_min minutes sync -> market cap from DexScreener -> minimum and confirmations."""

    def __init__(self, path, cfg=None, fetch=fetch_mcaps, clock=time.time, sleep=None, pause=PAUSE):
        self.path, self.cfg = path, cfg or config()
        self.fetch, self.clock, self.pause = fetch, clock, pause
        self._stop = threading.Event()
        self.sleep = sleep or self._stop.wait
        self.clean_done = collections.deque()   # times of clean checks over the rolling hour
        self.store = None
        self.thread = None

    def start(self, first_delay=FIRST_DELAY):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, args=(first_delay,), daemon=True, name="replay-check")
            self.thread.start()
        return self

    def stop(self):
        self._stop.set()

    def run(self, first_delay=FIRST_DELAY):
        self.sleep(first_delay)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:
                log(f"replay: check failed: {type(e).__name__}: {e}")
                if self.store is not None:   # the connection may have gone bad -- reopen it
                    try:
                        self.store.close()
                    except Exception:
                        pass
                    self.store = None
            self.sleep(self.cfg["check_min"] * 60)

    def tick(self):
        now = int(self.clock())
        if self.store is None:
            self.store = Store(self.path)
        st, interval = self.store, self.cfg["check_min"] * 60 * 0.9   # small tolerance for tick drift
        added = st.sync(now, self.cfg["min_mcap"])
        while self.clean_done and now - self.clean_done[0] >= 3600:
            self.clean_done.popleft()
        danger = st.due("danger", now, interval, 100_000)
        clean = st.due("clean", now, interval, self.cfg["max_clean_per_hour"] - len(self.clean_done))
        grp = {t: "danger" for t, _ in danger} | {t: "clean" for t, _ in clean}
        by_chain = collections.defaultdict(list)
        for t, chain in danger + clean:
            by_chain[chain].append(t)
        seen = missing = failed = confirmed = requests = 0
        for chain, toks in by_chain.items():
            for i in range(0, len(toks), BATCH):
                part = toks[i:i + BATCH]
                if requests:
                    self.sleep(self.pause)
                    if self._stop.is_set():
                        return
                requests += 1
                try:
                    caps = self.fetch(chain, part)
                except Exception as e:
                    failed += len(part)
                    log(f"replay: dexscreener {chain} ({len(part)} tokens) failed: {market._why(e)}")
                    continue
                t_now = int(self.clock())
                for t in part:
                    if grp[t] == "clean":
                        self.clean_done.append(t_now)
                    m = caps.get(t)
                    if m is None:
                        missing += 1
                        st.mark(t, t_now)
                        continue
                    seen += 1
                    if st.update(t, m, t_now):
                        confirmed += 1
                        log(f"replay: confirmed {t} ({chain}): market cap fell 90%+ after the scan")
        if danger or clean or added:
            log(f"replay: checked {len(danger)} danger + {len(clean)} clean in {requests} requests: {seen} with "
                f"market cap, {missing} without, {failed} failed, {confirmed} confirmed; {added} new from scans")
