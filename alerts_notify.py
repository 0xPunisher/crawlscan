"""Telegram alerts: sending notifications to subscribers (step A3). The site sends them itself via the Bot API (TG_BOT_TOKEN).

The scan thread only puts an event in the queue (push is O(1), no DB or network); a separate background thread:
  - finds the token's active subscribers (expired subscriptions are deleted);
  - at most one notification per token per chat every COOLDOWN seconds: changes inside the window accumulate,
    when the window ends a single summary goes out: diff(snapshot before the first deferred change, last snapshot);
    if everything reverted within the window the summary is empty and nothing is sent;
  - at most RATE messages per second in total (Telegram limit is 30); on 429 wait retry_after and retry;
  - 403 (user blocked the bot): delete all of the chat's subscriptions.
Window state (who got what when, deferred summaries) is in process memory: after a restart the window starts over.
"""
import queue, threading, time, traceback

import alerts
import trade
from bot.tg import Telegram, TelegramError

COOLDOWN = 15 * 60     # seconds between notifications for one token to one chat
RATE = 20              # messages per second for the whole site
QUEUE_MAX = 1000       # events in the queue; beyond that new ones are dropped (logged)
RETRIES = 2            # send retries on 429 / network failure


def telegram(token):
    """send(chat_id, html, markup) via the Bot API. The token is only in the request URL and never ends up in errors (redact)."""
    tg = Telegram(token)

    def send(chat_id, text, markup):
        return tg.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup,
                       link_preview_options={"is_disabled": True})
    return send


class Notifier:
    def __init__(self, store, send, clock=time.time, sleep=time.sleep, cooldown=COOLDOWN, rate=RATE, log=None):
        self.store, self.send, self.clock, self.sleep = store, send, clock, sleep   # store() → AlertsStore
        self.cooldown, self.gap = cooldown, 1.0 / rate
        self.log = log or (lambda m: print(m, flush=True))
        self.events = queue.Queue(QUEUE_MAX)
        self.last_sent = {}      # (chat_id, token) -> clock() of the last notification
        self.pending = {}        # (chat_id, token) -> {"base": snapshot, "cur": snapshot, "due": clock()}
        self.next_slot = 0.0     # clock() before which the next message does not go out (RATE)
        self.thread = None
        self.sent = 0

    # --- from the scan thread ------------------------------------------------------------------------

    def push(self, prev, cur, changes):
        """New snapshot with a non-empty diff. Non-blocking: if the queue is full the event is lost (logged)."""
        self._put(("diff", prev, cur, changes), cur.get("token"))

    def push_message(self, chat_id, text, markup=None):
        """Service message to one chat (auto-unsubscribe, subscription expiry): no cooldown, RATE-limited."""
        self._put(("msg", chat_id, text, markup), chat_id)

    def _put(self, ev, what):
        try:
            self.events.put_nowait(ev)
        except queue.Full:
            self.log(f"alerts: notify queue full, dropped {what}")

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True, name="alerts-notify")
            self.thread.start()
        return self

    # --- background thread -------------------------------------------------------------------------

    def run(self):
        while True:
            try:
                due = min((p["due"] for p in self.pending.values()), default=None)
                wait = None if due is None else max(0.0, due - self.clock())
                try:
                    ev = self.events.get(timeout=wait)
                except queue.Empty:
                    ev = None
                if ev is not None:
                    kind, *args = ev
                    if kind == "diff":
                        self.process(*args)
                    else:
                        self.send_rated(*args)
                self.flush()
            except Exception:
                self.log("alerts: notify loop:\n" + traceback.format_exc())
                self.sleep(1)

    def process(self, prev, cur, changes):
        """Event: send to subscribers right away or defer into a summary (cooldown)."""
        token, now = cur["token"], self.clock()
        for chat_id in self.store().watchers(token, int(self.clock())):
            key = (chat_id, token)
            if key in self.pending:
                self.pending[key]["cur"] = cur                        # summary: base unchanged, new snapshot
            elif now - self.last_sent.get(key, -1e18) < self.cooldown:
                self.pending[key] = {"base": prev, "cur": cur, "due": self.last_sent[key] + self.cooldown}
            else:
                self.deliver(chat_id, cur, changes)
        self._prune(now)

    def flush(self):
        """Deferred summaries whose window has ended."""
        now = self.clock()
        for key in [k for k, p in self.pending.items() if p["due"] <= now]:
            p = self.pending.pop(key)
            chat_id, token = key
            if not self.store().is_watching(chat_id, token, int(self.clock())):
                continue                                              # unsubscribed or subscription expired
            changes = alerts.diff(p["base"], p["cur"])
            if changes:
                self.deliver(chat_id, p["cur"], changes)

    def deliver(self, chat_id, cur, changes):
        text, markup = alerts.message(cur, changes, trade.url(cur.get("chain"), cur["token"]))
        if not self.send_rated(chat_id, text, markup):
            return False
        self.last_sent[(chat_id, cur["token"])] = self.clock()
        self.sent += 1
        self.log(f"alerts: sent {cur['token']} to {chat_id}: {', '.join(c['kind'] for c in changes)}")
        return True

    def send_rated(self, chat_id, text, markup=None):
        """Send with the RATE limit, retries on 429 / network failure; 403 deletes the chat's subscriptions. -> whether it was sent."""
        for attempt in range(RETRIES + 1):
            wait = self.next_slot - self.clock()
            if wait > 0:
                self.sleep(wait)
            self.next_slot = self.clock() + self.gap
            try:
                self.send(chat_id, text, markup)
            except TelegramError as e:
                if e.code == 403:
                    n = self.store().unwatch_chat(chat_id)
                    self.log(f"alerts: chat {chat_id} blocked the bot, removed {n} watches")
                    return False
                if attempt < RETRIES and (e.code == 429 or e.code == 0):
                    self.sleep(min(30, e.retry_after or 1))
                    continue
                self.log(f"alerts: send to {chat_id} failed: {e}")
                return False
            except Exception as e:   # not a Telegram network issue but our own failure: other subscribers still get theirs
                self.log(f"alerts: send to {chat_id} failed: {type(e).__name__}: {e}")
                return False
            return True
        return False

    def _prune(self, now):
        if len(self.last_sent) > 10000:
            for k in [k for k, t in self.last_sent.items() if now - t >= self.cooldown]:
                del self.last_sent[k]
