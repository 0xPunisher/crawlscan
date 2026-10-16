"""Telegram alerts: scheduled rechecks of watched tokens (step A4). Only with ALERTS_ENABLED.

A background thread (its name starts with chains.priority.BG, so its network requests yield to live scans) every TICK seconds:
  - deletes expired subscriptions and sends the subscriber "Stopped watching … after 7 days" with a Watch button;
  - takes tokens with active subscriptions whose last snapshot is older than ALERTS_RECHECK_MIN minutes (env, 15);
    least recently checked (and those without a snapshot) first;
  - rechecks them one by one while: the ALERTS_MAX_RECHECKS_PER_HOUR cap (env, 60; sliding hour,
    site-wide) is not exhausted, there are no live user scans, and there is no pause;
  - recheck = a regular engine.scan (same caches), the result goes the same path as a live scan but without the feed
    (server.record_snapshot: snapshot -> diff -> notifications; TOO_ESTABLISHED: auto-unsubscribe);
  - RPC answered rate-limit / 429 / throughput: the result is not used, pause for PAUSE seconds (logged);
  - a live scan started during the recheck, its requests waited for it, and some wallets were not read
    (unread): the result is incomplete and not used; the token is requeued in a minute.
A failed attempt (scan error) is retried no sooner than ALERTS_RECHECK_MIN.
"""
import collections, os, threading, time, traceback

import alerts
from chains import priority

TICK = 60                 # seconds between passes
PAUSE = 600               # seconds of pause after an RPC rate-limit
HOUR = 3600
RECHECK_MIN = 15          # minutes: default for ALERTS_RECHECK_MIN
MAX_PER_HOUR = 60         # default for ALERTS_MAX_RECHECKS_PER_HOUR
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
                 yields=lambda: priority.YIELDS[0], clock=time.time, sleep=time.sleep, log=None, cfg=None,
                 memory_high=lambda: False):
        """store() -> AlertsStore; scan(token) -> engine result; after(result): live scan path without the feed;
        notify(chat_id, text, markup): service message (or None: without TG_BOT_TOKEN we do not send);
        rate_limited(): counter of RPC rate-limit responses (sum over adapters);
        memory_high(): process memory above the threshold (memguard): rechecks do not start."""
        cfg = cfg or config()
        self.store, self.scan, self.after, self.notify = store, scan, after, notify
        self.live_count, self.rate_limited, self.yields = live_count, rate_limited, yields
        self.clock, self.sleep, self.memory_high = clock, sleep, memory_high
        self.log = log or (lambda m: print(m, flush=True))
        self.min_age, self.max_per_hour = cfg["recheck_min"] * 60, cfg["max_per_hour"]
        self.starts = collections.deque()     # clock() of recheck starts within the last hour
        self.attempted = {}                   # token -> clock() of the last attempt
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

    # --- one pass ------------------------------------------------------------------------------------

    def used(self, now):
        while self.starts and now - self.starts[0] >= HOUR:
            self.starts.popleft()
        return len(self.starts)

    def due(self, now):
        """Queue: tokens with subscriptions, snapshot older than min_age, last attempt not younger than min_age."""
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
            if self.memory_high():
                self.log(f"alerts: recheck skipped: memory high, {left} waiting")
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
            self.attempted[token] = now - self.min_age + TICK     # incomplete due to live scans: retry in a minute
            self.log(f"alerts: recheck {token} discarded: yielded to live scans, {unread} wallets unread")
            return
        self.after(res)
        self.log(f"alerts: recheck {token} done: {res.get('band')} {res.get('score')}, "
                 f"{time.time() - t0:.1f}s, rpc {res.get('rpc_requests')}")
