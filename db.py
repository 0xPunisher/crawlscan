"""Shared bits for SQLite stores: draw, rewards, feed and alerts write to one file (DRAW_DB_PATH).
WAL (readers do not wait for the writer), busy_timeout of BUSY_TIMEOUT seconds instead of an instant "database is locked",
synchronous=NORMAL (safe under WAL, faster commits) and one lock per file: all of the process's stores over one
file write in turn instead of fighting over the SQLite lock.
"""
import os, sqlite3, threading

BUSY_TIMEOUT = 5.0   # seconds a connection waits for another writer before OperationalError
_locks, _locks_lock = {}, threading.Lock()


def lock_for(path):
    """Shared lock (RLock) for all of the process's stores over the file path."""
    key = path if path == ":memory:" else os.path.realpath(path)
    with _locks_lock:
        return _locks.setdefault(key, threading.RLock())


def connect(path):
    """Connection for a store (across threads, under lock_for(path)); WAL and busy_timeout."""
    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT * 1000)}")
    try:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError as e:   # file busy in another process: the next connection will enable WAL
        print(f"db: WAL not enabled for {os.path.basename(path)}: {e}", flush=True)
    return db
