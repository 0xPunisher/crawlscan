"""Слоты живых сканов с приоритетом премиума (PREMIUM_PRIORITY, только при PREMIUM_ENABLED).

Меняется только порядок в очереди ожидания: число слотов то же (MAX_CONCURRENT), защита по памяти и лимит по IP —
до очереди, в server.start_scan, и не меняются. Когда слот освобождается, его получает ждущий скан премиум-холдера
(запрос от нашего бота: секрет + Telegram ID, статус проверяет сайт), а если премиумов нет — самый старый обычный.
Среди премиумов первым идёт тот, у кого больше $CrawlScan на привязанном кошельке (баланс из уже сохранённой привязки,
без новых запросов), при равном балансе — кто раньше встал. Обычные — FIFO.
Справедливость: после STREAK_MAX премиум-сканов подряд слот обязательно получает обычный скан, если он ждёт.
Слот передаётся ждущему из рук в руки (release не освобождает его «в общий котёл»), поэтому новый запрос не может
проскочить мимо очереди.
Выключено — server.py берёт слот у прежнего threading.BoundedSemaphore, этот модуль не используется.
"""
import collections, contextlib, heapq, itertools, threading

STREAK_MAX = 2           # премиум-сканов подряд, после которых слот получает ждущий обычный


class FairGate:
    def __init__(self, slots, streak_max=STREAK_MAX):
        self.cond = threading.Condition()
        self.free = slots
        self.streak_max = streak_max
        # премиум: куча (−баланс, номер, билет) — больший баланс первым; обычные: очередь билетов по порядку
        self.waiting = {True: [], False: collections.deque()}
        self.seq = itertools.count()
        self.granted = set()     # билеты, которым передан слот
        self.streak = 0          # премиум-сканов подряд с последнего обычного
        self.order = []          # для тестов и лога: (премиум ли) в порядке получения слота; не больше ORDER_MAX
        self.order_max = 1000

    def _took(self, prio):
        self.streak = self.streak + 1 if prio else 0
        if len(self.order) < self.order_max:
            self.order.append(prio)

    def _pick(self):
        """Кому отдать освободившийся слот: премиум, если он ждёт и подряд их было меньше streak_max (или обычных
        нет), иначе обычный. Никто не ждёт — None."""
        prem, norm = self.waiting[True], self.waiting[False]
        if prem and (self.streak < self.streak_max or not norm):
            return True
        if norm:
            return False
        return None

    def acquire(self, prio=False):
        """prio: False / 0 — обычный скан; иначе премиум, число — баланс $CrawlScan (больше — раньше; True = 1)."""
        weight, prio = int(prio or 0), bool(prio)
        with self.cond:
            if self.free > 0 and not self.waiting[True] and not self.waiting[False]:
                self.free -= 1
                self._took(prio)
                return
            ticket = object()
            if prio:
                heapq.heappush(self.waiting[True], (-weight, next(self.seq), ticket))
            else:
                self.waiting[False].append(ticket)
            while ticket not in self.granted:
                self.cond.wait()
            self.granted.discard(ticket)

    def release(self):
        with self.cond:
            who = self._pick()
            if who is None:
                self.free += 1
                return
            self.granted.add(heapq.heappop(self.waiting[True])[2] if who else self.waiting[False].popleft())
            self._took(who)
            self.cond.notify_all()

    @contextlib.contextmanager
    def slot(self, prio=False):
        self.acquire(prio)
        try:
            yield
        finally:
            self.release()

    def waiting_count(self):
        with self.cond:
            return len(self.waiting[True]), len(self.waiting[False])
