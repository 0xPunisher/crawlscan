"""Приоритет живых сканов над фоновыми перепроверками alerts в общих лимитерах RPS адаптеров.

Фоновая работа узнаётся по имени потока (BG в начале: поток перепроверок и пул кошельков движка,
который он запускает). Перед каждым запросом в сеть такой поток ждёт (wait_turn), пока идут живые сканы
пользователей (live() — счётчик в server). Живые потоки не ждут никогда: для них — одна проверка имени потока.
RATE_LIMITED в адаптерах — сколько раз нода ответила rate-limit (для паузы перепроверок).
"""
import threading
from contextlib import contextmanager

BG = "recheck"
YIELD_MAX = 60.0          # секунд: дольше одного ожидания фоновый поток не ждёт (дальше — снова проверка)
_cond = threading.Condition()
_live = [0]
YIELDS = [0]              # сколько раз фоновые запросы уступили живым сканам


def is_background():
    return threading.current_thread().name.startswith(BG)


@contextmanager
def live():
    """Живой скан пользователя (или /api/early) идёт: фоновые запросы ждут."""
    with _cond:
        _live[0] += 1
    try:
        yield
    finally:
        with _cond:
            _live[0] -= 1
            _cond.notify_all()


def live_count():
    with _cond:
        return _live[0]


def wait_turn():
    """Вызывается адаптером перед запросом в сеть. Живой поток — сразу; фоновый — ждёт конца живых сканов."""
    if not is_background():
        return
    with _cond:
        if _live[0] == 0:
            return
        YIELDS[0] += 1
        _cond.wait_for(lambda: _live[0] == 0, timeout=YIELD_MAX)
