"""Telegram alerts: отправка уведомлений подписчикам (шаг A3). Сайт шлёт сам через Bot API (TG_BOT_TOKEN).

Поток скана только кладёт событие в очередь (push — O(1), без базы и сети); отдельный фоновый поток:
  - находит действующих подписчиков токена (истёкшие подписки удаляются);
  - не чаще одного уведомления по токену одному чату за COOLDOWN секунд: изменения внутри окна копятся,
    по окончании окна уходит одно сводное — diff(снимок до первого отложенного изменения, последний снимок);
    за окно всё вернулось как было — сводка пустая, ничего не шлём;
  - не больше RATE сообщений в секунду суммарно (лимит Telegram — 30); 429 — ждём retry_after и повторяем;
  - 403 (пользователь заблокировал бота) — удаляем все подписки чата.
Состояние окна (кто когда получил, отложенные сводки) — в памяти процесса: после рестарта окно начинается заново.
"""
import queue, threading, time, traceback

import alerts
import trade
from bot.tg import Telegram, TelegramError

COOLDOWN = 15 * 60     # секунд между уведомлениями по одному токену одному чату
RATE = 20              # сообщений в секунду на весь сайт
QUEUE_MAX = 1000       # событий в очереди; больше — новые отбрасываются (с записью в лог)
RETRIES = 2            # повторов отправки на 429 / сбой сети


def telegram(token):
    """send(chat_id, html, markup) через Bot API. Токен — только в URL запроса, в ошибки не попадает (redact)."""
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
        self.last_sent = {}      # (chat_id, token) -> clock() последнего уведомления
        self.pending = {}        # (chat_id, token) -> {"base": снимок, "cur": снимок, "due": clock()}
        self.next_slot = 0.0     # clock(), раньше которого следующее сообщение не уходит (RATE)
        self.thread = None
        self.sent = 0

    # --- из потока скана ---------------------------------------------------------------------------

    def push(self, prev, cur, changes):
        """Новый снимок с непустым diff. Не блокирует: очередь полна — событие теряется (в лог)."""
        try:
            self.events.put_nowait((prev, cur, changes))
        except queue.Full:
            self.log(f"alerts: notify queue full, dropped {cur.get('token')}")

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True, name="alerts-notify")
            self.thread.start()
        return self

    # --- фоновый поток -----------------------------------------------------------------------------

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
                    self.process(*ev)
                self.flush()
            except Exception:
                self.log("alerts: notify loop:\n" + traceback.format_exc())
                self.sleep(1)

    def process(self, prev, cur, changes):
        """Событие: отправить подписчикам сразу или отложить в сводку (cooldown)."""
        token, now = cur["token"], self.clock()
        for chat_id in self.store().watchers(token, int(self.clock())):
            key = (chat_id, token)
            if key in self.pending:
                self.pending[key]["cur"] = cur                        # сводка: база прежняя, снимок новый
            elif now - self.last_sent.get(key, -1e18) < self.cooldown:
                self.pending[key] = {"base": prev, "cur": cur, "due": self.last_sent[key] + self.cooldown}
            else:
                self.deliver(chat_id, cur, changes)
        self._prune(now)

    def flush(self):
        """Отложенные сводки, у которых кончилось окно."""
        now = self.clock()
        for key in [k for k, p in self.pending.items() if p["due"] <= now]:
            p = self.pending.pop(key)
            chat_id, token = key
            if not self.store().is_watching(chat_id, token, int(self.clock())):
                continue                                              # отписался или подписка истекла
            changes = alerts.diff(p["base"], p["cur"])
            if changes:
                self.deliver(chat_id, p["cur"], changes)

    def deliver(self, chat_id, cur, changes):
        text, markup = alerts.message(cur, changes, trade.url(cur.get("chain"), cur["token"]))
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
            except Exception as e:   # не сеть Telegram, а наш сбой: остальные подписчики всё равно получат
                self.log(f"alerts: send to {chat_id} failed: {type(e).__name__}: {e}")
                return False
            self.last_sent[(chat_id, cur["token"])] = self.clock()
            self.sent += 1
            self.log(f"alerts: sent {cur['token']} to {chat_id}: {', '.join(c['kind'] for c in changes)}")
            return True
        return False

    def _prune(self, now):
        if len(self.last_sent) > 10000:
            for k in [k for k, t in self.last_sent.items() if now - t >= self.cooldown]:
                del self.last_sent[k]
