"""Premium: Trending (PREMIUM_TRENDING) — самые сканируемые токены за последний час.

Лёгкий счётчик в памяти процесса: каждый принятый запрос скана (/api/scan, включая ответы из 10-минутного кэша и
подключение к идущему скану) — hit(токен): поминутные корзины, свой замок, O(1), без базы и сети. Считается только
при включённом выключателе. После рестарта счёт начинается заново.
"""
import threading, time

WINDOW = 3600            # секунд: «за последний час»
BUCKET = 60              # секунд в корзине
MAX_TOKENS = 20_000      # токенов в счётчике; больше — старые корзины чистятся


class Counter:
    def __init__(self, clock=time.time):
        self.clock, self.lock = clock, threading.Lock()
        self.hits = {}           # (сеть, токен) -> {минута: сколько}

    def hit(self, chain, token):
        m = int(self.clock()) // BUCKET
        with self.lock:
            b = self.hits.setdefault((chain, token), {})
            b[m] = b.get(m, 0) + 1
            if len(self.hits) > MAX_TOKENS:
                self._prune(m)

    def _prune(self, m):
        lo = m - WINDOW // BUCKET
        for k in list(self.hits):
            b = {x: n for x, n in self.hits[k].items() if x > lo}
            if b:
                self.hits[k] = b
            else:
                del self.hits[k]

    def top(self, n=10):
        """[(сеть, токен, сканов за WINDOW)] — больше первыми (при равенстве — свежее)."""
        m = int(self.clock()) // BUCKET
        with self.lock:
            self._prune(m)
            rows = [(k[0], k[1], sum(b.values()), max(b)) for k, b in self.hits.items()]
        rows.sort(key=lambda r: (-r[2], -r[3]))
        return [(c, t, k) for c, t, k, _ in rows[:n]]
