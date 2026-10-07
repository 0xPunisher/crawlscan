"""Telegram alerts: плановые перепроверки отслеживаемых токенов (шаг A4). Только при ALERTS_ENABLED.

Фоновый поток (имя начинается с chains.priority.BG — его запросы в сеть уступают живым сканам) раз в TICK секунд:
  - удаляет истёкшие подписки и пишет подписчику «Stopped watching … after 7 days» с кнопкой Watch;
  - берёт токены с действующими подписками, чей последний снимок старше ALERTS_RECHECK_MIN минут (env, 15);
    давно проверенные (и без снимка) первыми;
  - перепроверяет их по одному, пока: не исчерпан потолок ALERTS_MAX_RECHECKS_PER_HOUR (env, 60; скользящий час,
    на весь сайт), нет живых сканов пользователей, нет паузы;
  - перепроверка = обычный engine.scan (кэши те же), результат — тем же путём, что живой скан, но без ленты
    (server.record_snapshot: снимок → diff → уведомления; TOO_ESTABLISHED — авто-отписка);
  - RPC ответил rate-limit / 429 / throughput — результат не используется, пауза PAUSE секунд (лог);
  - во время перепроверки начался живой скан и её запросы ждали его, а часть кошельков не успела прочитаться
    (unread) — результат неполный, не используется; токен — снова в очередь через минуту.
Неудачная попытка (ошибка скана) повторяется не раньше чем через ALERTS_RECHECK_MIN.
"""
import collections, os, threading, time, traceback

import alerts
from chains import priority

TICK = 60                 # секунд между проходами
PAUSE = 600               # секунд паузы после rate-limit RPC
HOUR = 3600
RECHECK_MIN = 15          # минут: по умолчанию ALERTS_RECHECK_MIN
MAX_PER_HOUR = 60         # по умолчанию ALERTS_MAX_RECHECKS_PER_HOUR
RATE_WORDS = ("429", "rate limit", "rate-limit", "throughput", "per second", "too many requests",
              "compute units")


def _env_int(name, default):
    try:
        v = int(os.environ.get(name, "").strip())
        return v if v > 0 else default
    except ValueError:
        return default


def config():
    return {"recheck_min": _env_int("ALERTS_RECHECK_MIN", RECHECK_MIN),
            "max_per_hour": _env_int("ALERTS_MAX_RECHECKS_PER_HOUR", MAX_PER_HOUR)}


def _ago(sec):
    return "new" if sec is None else f"{int(sec // 60)}m"


class Rechecker:
    def __init__(self, store, scan, after, notify, live_count=priority.live_count, rate_limited=lambda: 0,
                 yields=lambda: priority.YIELDS[0], clock=time.time, sleep=time.sleep, log=None, cfg=None):
        """store() → AlertsStore; scan(token) → результат движка; after(result) — путь живого скана без ленты;
        notify(chat_id, text, markup) — служебное сообщение (или None: без TG_BOT_TOKEN не пишем);
        rate_limited() — счётчик ответов rate-limit RPC (сумма по адаптерам)."""
        cfg = cfg or config()
        self.store, self.scan, self.after, self.notify = store, scan, after, notify
        self.live_count, self.rate_limited, self.yields = live_count, rate_limited, yields
        self.clock, self.sleep = clock, sleep
        self.log = log or (lambda m: print(m, flush=True))
        self.min_age, self.max_per_hour = cfg["recheck_min"] * 60, cfg["max_per_hour"]
        self.starts = collections.deque()     # clock() начала перепроверок за последний час
        self.attempted = {}                   # token -> clock() последней попытки
        self.paused_until = 0.0
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True, name=f"{priority.BG}-alerts")
            self.thread.start()
        return self

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:
                self.log("alerts: recheck loop:\n" + traceback.format_exc())
            self.sleep(TICK)

    # --- один проход --------------------------------------------------------------------------------

    def used(self, now):
        while self.starts and now - self.starts[0] >= HOUR:
            self.starts.popleft()
        return len(self.starts)

    def due(self, now):
        """Очередь: токены с подписками, снимок старше min_age, последняя попытка не моложе min_age."""
        out = []
        for q in self.store().recheck_queue(int(now)):
            age = None if q["ts"] is None else now - q["ts"]
            if (age is None or age >= self.min_age) and now - self.attempted.get(q["token"], -1e18) >= self.min_age:
                out.append(q | {"age": age})
        return out

    def tick(self):
        now = self.clock()
        self.expire(now)
        if now < self.paused_until:
            return
        queue = self.due(now)
        for i, q in enumerate(queue):
            now, left = self.clock(), len(queue) - i
            if now < self.paused_until:
                return
            if self.used(now) >= self.max_per_hour:
                self.log(f"alerts: recheck cap reached ({self.max_per_hour}/{self.max_per_hour} this hour), "
                         f"{left} waiting")
                return
            live = self.live_count()
            if live:
                self.log(f"alerts: recheck skipped: {live} live scan{'s' if live > 1 else ''}, {left} waiting")
                return
            self.recheck(q, left, now)

    def expire(self, now):
        for w in self.store().expire(int(now)):
            self.log(f"alerts: watch expired {w['token']} chat {w['chat_id']}")
            if self.notify:
                self.notify(w["chat_id"], *alerts.expired_message(w["token"], w.get("ticker"), w.get("chain")))

    def pause(self, token, why):
        self.paused_until = self.clock() + PAUSE
        self.log(f"alerts: RPC rate limit during recheck {token} ({why}), pausing rechecks for {PAUSE // 60} min")

    def recheck(self, q, left, now):
        token = q["token"]
        self.starts.append(now)
        self.attempted[token] = now
        self.log(f"alerts: recheck {token} (age {_ago(q['age'])}, queue {left}, "
                 f"used {self.used(now)}/{self.max_per_hour} this hour)")
        rl0, y0, t0 = self.rate_limited(), self.yields(), time.time()
        try:
            res = self.scan(token)
        except Exception as e:
            text = str(e)
            if self.rate_limited() > rl0 or any(w in text.lower() for w in RATE_WORDS):
                return self.pause(token, f"{type(e).__name__}: {text[:120]}")
            self.log(f"alerts: recheck {token} failed: {type(e).__name__}: {text[:200]}")
            return
        if self.rate_limited() > rl0:
            return self.pause(token, f"{self.rate_limited() - rl0} rate-limited responses")
        unread = len(res.get("unread") or [])
        if self.yields() > y0 and unread:
            self.attempted[token] = now - self.min_age + TICK     # неполный из-за живых сканов — через минуту
            self.log(f"alerts: recheck {token} discarded: yielded to live scans, {unread} wallets unread")
            return
        self.after(res)
        self.log(f"alerts: recheck {token} done: {res.get('band')} {res.get('score')}, "
                 f"{time.time() - t0:.1f}s, rpc {res.get('rpc_requests')}")
