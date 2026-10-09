"""Приоритет живых сканов над фоновой работой в лимитерах RPS адаптеров.

Фоновая работа — поток с именем на BG (поток перепроверок alerts и пул кошельков, который движок запускает
из фона) или код внутри background() (/api/early без истории живого скана).
Перед каждым запросом в сеть такой поток ждёт (wait_turn), пока идут живые сканы пользователей
(live() — счётчик в server), и проходит свой отдельный лимит BACKGROUND_RPS (Throttle адаптера).
Живые потоки не ждут никогда: для них — одна проверка потока.
Критичная работа (critical(): планировщик наград — розыгрыш, сжигания, выплаты) живым сканам не уступает и не
считается фоном: проходит свой лимит CRITICAL_RPS (гарантированная доля) и общий лимит адаптера.
Строгий фон (background(strict=True): построение индекса Bankr) ждёт конца живых сканов без потолка YIELD_MAX:
пока идёт хоть один живой скан, ни одного запроса не начинает.
RATE_LIMITED в адаптерах — сколько раз нода ответила rate-limit (для паузы перепроверок).
"""
import os, threading, time
from contextlib import contextmanager

BG = "recheck"
YIELD_MAX = 60.0          # секунд: дольше одного ожидания фоновый поток не ждёт (дальше — снова проверка)
BACKGROUND_RPS = 2.0      # по умолчанию env BACKGROUND_RPS: запросов/сек на всю фоновую работу адаптера; 0 — без своего лимита
_cond = threading.Condition()
_live = [0]
_live_time = [0.0, None]  # секунд, когда шёл хоть один живой скан (сумма), и с какого момента идёт текущий
_tls = threading.local()
CRITICAL_RPS = 2.0        # по умолчанию env CRITICAL_RPS: запросов/сек критичной работы адаптера; 0 — без своего лимита
YIELDS = [0]              # сколько раз фоновые запросы уступили живым сканам


def is_critical():
    return getattr(_tls, "crit", False)


def is_strict():
    return getattr(_tls, "strict", False)


def is_background():
    if is_critical():
        return False
    return getattr(_tls, "bg", False) or threading.current_thread().name.startswith(BG)


@contextmanager
def critical():
    """Критичная работа в текущем потоке (планировщик наград): не ждёт живых сканов, свой лимит CRITICAL_RPS."""
    prev = getattr(_tls, "crit", False)
    _tls.crit = True
    try:
        yield
    finally:
        _tls.crit = prev


@contextmanager
def background(strict=False):
    """Фоновая работа в текущем потоке (/api/early без истории скана).
    BACKGROUND_RPS=0 — выключатель: такая работа не фоновая, как раньше (перепроверки alerts — фон всегда).
    strict — фон всегда (и при BACKGROUND_RPS=0) и ждёт живых сканов без потолка YIELD_MAX (индекс Bankr)."""
    prev, prev_strict = getattr(_tls, "bg", False), getattr(_tls, "strict", False)
    _tls.bg = prev or strict or background_rps() > 0
    _tls.strict = prev_strict or strict
    try:
        yield
    finally:
        _tls.bg, _tls.strict = prev, prev_strict


@contextmanager
def live():
    """Живой скан пользователя (или /api/early по его истории) идёт: фоновые запросы ждут."""
    with _cond:
        _live[0] += 1
        if _live[0] == 1:
            _live_time[1] = time.time()
    try:
        yield
    finally:
        with _cond:
            _live[0] -= 1
            if _live[0] == 0 and _live_time[1] is not None:
                _live_time[0] += time.time() - _live_time[1]
                _live_time[1] = None
            _cond.notify_all()


def live_seconds():
    """Сколько секунд всего шёл хоть один живой скан (для ETA фона: его паузы)."""
    with _cond:
        return _live_time[0] + (time.time() - _live_time[1] if _live_time[1] is not None else 0.0)


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
        if is_strict():
            while _live[0]:
                _cond.wait(YIELD_MAX)
            return
        _cond.wait_for(lambda: _live[0] == 0, timeout=YIELD_MAX)


def background_rps():
    """BACKGROUND_RPS из env (пусто или неверно — дефолт; 0 или меньше — своего лимита у фона нет)."""
    try:
        v = float(os.environ.get("BACKGROUND_RPS", "").strip() or BACKGROUND_RPS)
    except ValueError:
        return BACKGROUND_RPS
    return v if v > 0 else 0.0


def critical_rps():
    """CRITICAL_RPS из env (пусто или неверно — дефолт; 0 или меньше — своего лимита нет, только общий)."""
    try:
        v = float(os.environ.get("CRITICAL_RPS", "").strip() or CRITICAL_RPS)
    except ValueError:
        return CRITICAL_RPS
    return v if v > 0 else 0.0


def background_scale(rps):
    """Во сколько раз фон медленнее живого скана под лимитом адаптера rps: бюджеты фона растягиваются на столько же,
    чтобы фоновый скан успевал прочитать столько же, сколько живой. Своего лимита нет или он не ниже rps — 1."""
    bg = background_rps()
    return rps / bg if 0 < bg < rps else 1.0


class Throttle:
    """Лимит запросов во времени: под замком только резерв слота, сон — вне замка (ждущий не держит остальных).
    n — вес запроса (провайдеры считают каждый элемент батча). rps <= 0 — без лимита."""

    def __init__(self, rps):
        self.gap = 1.0 / rps if rps and rps > 0 else 0.0
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self, n=1):
        if not self.gap:
            return
        with self.lock:
            now = time.time()
            slot = max(now, self.next)
            self.next = slot + self.gap * n
        if slot > now:
            time.sleep(slot - now)


def rate_limit(main, bg, n=1, crit=None):
    """Общий путь лимитеров адаптеров: критичная работа — свой лимит crit и общий main, без ожидания живых сканов;
    фон уступает живым сканам и проходит свой лимит bg, затем — общий main."""
    if is_critical():
        if crit is not None:
            crit.wait(n)
        main.wait(n)
        return
    wait_turn()
    if is_background() and bg.gap:
        bg.wait(n)
        wait_turn()      # пока ждали свой слот, мог начаться живой скан
    main.wait(n)
