"""Защита по памяти перед стартом скана: RSS процесса из /proc/self/statm против MEMORY_SOFT_LIMIT_MB.

Порог (env MEMORY_SOFT_LIMIT_MB, мегабайты; 0 — выключено): по умолчанию меньшее из DEFAULT_MB и 70% лимита
контейнера (cgroup memory.max), если он читается. Выше порога: сбросить кэши (история сканов, кэш переводов),
gc и malloc_trim, перемерить; всё ещё выше — занято. Нет /proc (macOS, тесты) — проверка не мешает.
"""
import ctypes, gc, os, threading, time

DEFAULT_MB = 1536          # Railway: запас до лимита сервиса; меньше лимита контейнера ×0.7, если он меньше
CGROUP_SHARE = 0.7
RELIEVE_EVERY = 5.0        # секунд: не чаще сбрасывать кэши и gc под наплывом запросов
CGROUP_FILES = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")

_state = {"relieved": 0.0}
_lock = threading.Lock()
_droppers = []             # функции сброса кэшей (server регистрирует адаптеры)


def rss_mb():
    """Резидентная память процесса, МБ, или None (нет /proc)."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 2 ** 20
    except (OSError, ValueError, IndexError):
        return None


def cgroup_limit_mb():
    """Лимит памяти контейнера, МБ, или None (не читается или «без лимита»)."""
    for path in CGROUP_FILES:
        try:
            with open(path) as f:
                raw = f.read().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 50:
            return int(raw) / 2 ** 20
    return None


def soft_limit_mb():
    """Порог, МБ; 0 — проверка выключена."""
    raw = os.environ.get("MEMORY_SOFT_LIMIT_MB", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    cg = cgroup_limit_mb()
    return int(min(DEFAULT_MB, cg * CGROUP_SHARE)) if cg else DEFAULT_MB


def on_relieve(fn):
    """Зарегистрировать сброс кэша (вызывается выше порога)."""
    _droppers.append(fn)


def _trim():
    """Вернуть ОС освобождённую память (glibc); на других libc — ничего."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def trim():
    """gc и возврат освобождённой памяти ОС (после фонового построения индекса Bankr: страницы getLogs)."""
    gc.collect()
    _trim()


def relieve():
    for fn in _droppers:
        try:
            fn()
        except Exception as e:
            print(f"memory: cache drop failed: {type(e).__name__}: {e}", flush=True)
    gc.collect()
    _trim()


NEAR_SHARE = 0.85         # near(): доля порога, выше которой фоновые построения (индекс Bankr) не идут


def near(share=NEAR_SHARE):
    """True — память процесса выше share × порога (без сброса кэшей): фоновое построение индекса прерывается
    и не стартует, чтобы не довести до отказа живым сканам. Нет /proc или порог выключен — False."""
    limit = soft_limit_mb()
    rss = rss_mb()
    return bool(limit) and rss is not None and rss >= limit * share


def over(live=0):
    """True — память выше порога и после сброса кэшей (занято, скан не стартует). live — сколько сканов идёт:
    без живых сканов память держит не скан, отказ её не освободит — скан пускаем (с записью в лог)."""
    limit = soft_limit_mb()
    rss = rss_mb()
    if not limit or rss is None or rss < limit:
        return False
    with _lock:
        if time.time() - _state["relieved"] >= RELIEVE_EVERY:
            _state["relieved"] = time.time()
            relieve()
            after = rss_mb() or 0
            print(f"memory: {rss:.0f} MB >= {limit} MB, caches dropped -> {after:.0f} MB", flush=True)
            rss = after
    if rss < limit:
        return False
    if not live:
        print(f"memory: {rss:.0f} MB >= {limit} MB with no scans running, letting the scan start", flush=True)
        return False
    return True
