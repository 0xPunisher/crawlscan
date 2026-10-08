"""Защита по памяти перед стартом скана: RSS процесса из /proc/self/statm против двух порогов.

Лимит контейнера — cgroup memory.max, если читается, иначе DEFAULT_LIMIT_MB. Мягкий порог (env MEMORY_SOFT_LIMIT_MB,
мегабайты; 0 — проверка выключена) по умолчанию SOFT_SHARE лимита, жёсткий (env MEMORY_HARD_LIMIT_MB) — HARD_SHARE,
но не ниже мягкого. Выше мягкого: сбросить кэши (история сканов, кэш переводов), gc и malloc_trim, перемерить
(в лог — память до и после). Всё ещё выше мягкого, но ниже жёсткого — сканы идут до cap (MAX_CONCURRENT), без очереди;
выше жёсткого — занято. Сброс, который не снизил память, повторяется не чаще RELIEVE_IDLE. Нет /proc (macOS, тесты)
— проверка не мешает. Раз в MONITOR_EVERY — одна строка в лог с памятью и сканами (start_monitor).
"""
import ctypes, gc, os, threading, time

DEFAULT_LIMIT_MB = 6000    # лимит контейнера, если cgroup не читается (Railway: сервис 8 ГБ)
SOFT_SHARE = 0.75
HARD_SHARE = 0.9
RELIEVE_EVERY = 5.0        # секунд: не чаще сбрасывать кэши и gc под наплывом запросов
RELIEVE_IDLE = 60.0        # секунд: после сброса, который не снизил память ниже мягкого порога
MONITOR_EVERY = 60.0       # секунд: строка памяти в лог
CGROUP_FILES = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")

_state = {"relieved": 0.0, "pause": RELIEVE_EVERY}
_lock = threading.Lock()
_droppers = []             # функции сброса кэшей (server регистрирует адаптеры)
_libc = []                 # [CDLL glibc] или [None] — загружается один раз


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


def container_limit_mb():
    return cgroup_limit_mb() or DEFAULT_LIMIT_MB


def _env_mb(name):
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return None


def soft_limit_mb():
    """Мягкий порог, МБ; 0 — проверка выключена."""
    v = _env_mb("MEMORY_SOFT_LIMIT_MB")
    return int(container_limit_mb() * SOFT_SHARE) if v is None else v


def hard_limit_mb():
    """Жёсткий порог, МБ (не ниже мягкого)."""
    v = _env_mb("MEMORY_HARD_LIMIT_MB")
    if not v:
        v = int(container_limit_mb() * HARD_SHARE)
    return max(v, soft_limit_mb())


def on_relieve(fn):
    """Зарегистрировать сброс кэша (вызывается выше порога)."""
    _droppers.append(fn)


def _trim():
    """Вернуть ОС освобождённую память (glibc malloc_trim(0)); False — не glibc."""
    if not _libc:
        try:
            lib = ctypes.CDLL("libc.so.6")
            lib.malloc_trim.argtypes = [ctypes.c_size_t]
            lib.malloc_trim.restype = ctypes.c_int
            _libc.append(lib)
        except (OSError, AttributeError):
            _libc.append(None)
    if _libc[0] is None:
        return False
    _libc[0].malloc_trim(0)
    return True


def _mb(v):
    return "?" if v is None else f"{v:.0f}"


def relieve():
    """Сбросить кэши, gc, malloc_trim; в лог — память до и после каждого шага. Возвращает RSS после (или None)."""
    before = rss_mb()
    for fn in _droppers:
        try:
            fn()
        except Exception as e:
            print(f"memory: cache drop failed: {type(e).__name__}: {e}", flush=True)
    gc.collect()
    dropped = rss_mb()
    trimmed = _trim()
    after = rss_mb()
    print(f"memory: relieve {_mb(before)} MB -> caches+gc {_mb(dropped)} MB -> "
          f"malloc_trim {_mb(after) if trimmed else 'n/a'}{' MB' if trimmed else ''}", flush=True)
    return after


def over(live=0, cap=0):
    """True — новый скан не пускать. live — сколько сканов идёт или ждёт слота, cap — сколько сканов пускать между
    мягким и жёстким порогом (server: MAX_CONCURRENT; 0 — выше мягкого сразу занято). Без живых сканов память
    держит не скан, отказ её не освободит — скан пускаем."""
    soft = soft_limit_mb()
    rss = rss_mb()
    if not soft or rss is None or rss < soft:
        return False
    with _lock:
        now = time.time()
        if now - _state["relieved"] >= _state["pause"]:
            _state["relieved"] = now
            after = relieve()
            rss = after if after is not None else rss
            _state["pause"] = RELIEVE_EVERY if rss < soft else RELIEVE_IDLE
    if rss < soft or not live:
        return False
    return live >= cap or rss >= hard_limit_mb()


def status_line(active, extra=""):
    soft = soft_limit_mb()
    lim = f"soft {soft} MB, hard {hard_limit_mb()} MB, limit {container_limit_mb():.0f} MB" if soft else "guard off"
    return f"memory: rss {_mb(rss_mb())} MB ({lim}), scans active {active}{extra}"


def start_monitor(stats, every=MONITOR_EVERY, stop=None):
    """Поток: раз в every секунд одна строка в лог. stats() -> (активных сканов, хвост строки); stop — Event."""
    stop = stop or threading.Event()

    def run():
        while not stop.wait(every):
            try:
                print(status_line(*stats()), flush=True)
            except Exception as e:
                print(f"memory: monitor failed: {type(e).__name__}: {e}", flush=True)

    t = threading.Thread(target=run, name="memguard-monitor", daemon=True)
    t.start()
    return t
