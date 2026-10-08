"""Общее для SQLite-хранилищ: розыгрыш, награды, лента и alerts пишут в один файл (DRAW_DB_PATH).
WAL (читатели не ждут писателя), busy_timeout BUSY_TIMEOUT секунд вместо мгновенного "database is locked",
synchronous=NORMAL (в WAL безопасно, быстрее коммит) и один замок на файл: все хранилища процесса над одним
файлом пишут по очереди, а не дерутся за блокировку SQLite.
"""
import os, sqlite3, threading

BUSY_TIMEOUT = 5.0   # секунд: столько соединение ждёт чужую запись, потом OperationalError
_locks, _locks_lock = {}, threading.Lock()


def lock_for(path):
    """Общий замок (RLock) для всех хранилищ процесса над файлом path."""
    key = path if path == ":memory:" else os.path.realpath(path)
    with _locks_lock:
        return _locks.setdefault(key, threading.RLock())


def connect(path):
    """Соединение для хранилища (между потоками — под lock_for(path)); WAL и busy_timeout."""
    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT * 1000)}")
    try:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.OperationalError as e:   # файл занят другим процессом: WAL включит следующее соединение
        print(f"db: WAL not enabled for {os.path.basename(path)}: {e}", flush=True)
    return db
