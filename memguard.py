"""Memory guard before starting a scan: process RSS from /proc/self/statm against two thresholds.

Container limit is cgroup memory.max if readable, else DEFAULT_LIMIT_MB. Soft threshold (env MEMORY_SOFT_LIMIT_MB,
megabytes; 0 disables the check) defaults to SOFT_SHARE of the limit, hard (env MEMORY_HARD_LIMIT_MB) to HARD_SHARE,
but not below soft. Above soft: drop caches (scan history, transfer cache), gc and malloc_trim, re-measure
(memory before and after goes to the log). Still above soft but below hard: scans run up to cap (MAX_CONCURRENT), no queue;
above hard: busy. A drop that did not reduce memory is repeated no more often than RELIEVE_IDLE. No /proc (macOS, tests):
the check does not interfere. Every MONITOR_EVERY: one log line with memory and scans (start_monitor).
"""
import ctypes, gc, os, threading, time

DEFAULT_LIMIT_MB = 6000    # container limit if cgroup is not readable (Railway: 8 GB service)
SOFT_SHARE = 0.75
HARD_SHARE = 0.9
RELIEVE_EVERY = 5.0        # seconds: drop caches and gc no more often than this under a request surge
RELIEVE_IDLE = 60.0        # seconds: after a drop that did not bring memory below the soft threshold
MONITOR_EVERY = 60.0       # seconds: memory line to the log
CGROUP_FILES = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")

_state = {"relieved": 0.0, "pause": RELIEVE_EVERY}
_lock = threading.Lock()
_droppers = []             # cache drop functions (server registers adapters)
_libc = []                 # [CDLL glibc] or [None]; loaded once


def rss_mb():
    """Process resident memory, MB, or None (no /proc)."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 2 ** 20
    except (OSError, ValueError, IndexError):
        return None


def cgroup_limit_mb():
    """Container memory limit, MB, or None (not readable or "no limit")."""
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
    """Soft threshold, MB; 0 disables the check."""
    v = _env_mb("MEMORY_SOFT_LIMIT_MB")
    return int(container_limit_mb() * SOFT_SHARE) if v is None else v


def hard_limit_mb():
    """Hard threshold, MB (not below soft)."""
    v = _env_mb("MEMORY_HARD_LIMIT_MB")
    if not v:
        v = int(container_limit_mb() * HARD_SHARE)
    return max(v, soft_limit_mb())


def on_relieve(fn):
    """Register a cache drop (called above the threshold)."""
    _droppers.append(fn)


def _trim():
    """Return freed memory to the OS (glibc malloc_trim(0)); False if not glibc."""
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


def trim():
    """gc and return freed memory to the OS (after the background Bankr index build: getLogs pages)."""
    gc.collect()
    _trim()


def relieve():
    """Drop caches, gc, malloc_trim; log memory before and after each step. Returns RSS after (or None)."""
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


NEAR_SHARE = 0.85         # near(): share of the threshold above which background builds (Bankr index) do not run


def near(share=NEAR_SHARE):
    """True if process memory is above share × threshold (without dropping caches): the background index build stops
    and does not start, so live scans are not pushed into refusals. No /proc or threshold disabled: False."""
    limit = soft_limit_mb()
    rss = rss_mb()
    return bool(limit) and rss is not None and rss >= limit * share


def over(live=0, cap=0):
    """True: do not admit a new scan. live: how many scans are running or waiting for a slot; cap: how many scans to admit between
    the soft and hard thresholds (server: MAX_CONCURRENT; 0 means busy right above soft). With no live scans the memory
    is not held by a scan and refusing will not free it, so the scan is admitted."""
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
    """Thread: one log line every `every` seconds. stats() -> (active scans, line tail); stop is an Event."""
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
