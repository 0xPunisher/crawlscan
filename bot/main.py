"""Telegram-бот CrawlScan — отдельный сервис, тонкий клиент API сайта (бот сам в блокчейн не ходит).

  python bot/main.py

env: TG_BOT_TOKEN (обязателен, из .env через env.py; нигде не печатается), CRAWLSCAN_API (по умолчанию
https://crawlscan.fun). Long polling getUpdates (timeout=30): одновременно может работать только один
экземпляр бота, второй получает 409 Conflict.

Личка: адрес токена (или /scan <адрес>) → скан; /start, /help. Группы: только /scan <адрес>
(и /scan@имябота). Скан: сразу ответ "crawling…", потом это же сообщение редактируется в вердикт.
Лимиты: 1 скан на пользователя в USER_COOLDOWN секунд, не больше WORKERS сканов одновременно (остальные — в очереди).
"""
import math, os, queue, sys, threading, time, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import env                                          # noqa: E402
from bot import text as T                           # noqa: E402
from bot.api import ApiError, CrawlScan, Rejected   # noqa: E402
from bot.tg import Telegram, TelegramError          # noqa: E402

WORKERS = 3            # сканов одновременно на весь бот
MAX_QUEUE = 30         # ждущих в очереди; больше — "too many scans"
USER_COOLDOWN = 20     # секунд между сканами одного пользователя
SCAN_TIMEOUT = 60      # секунд на скан (от начала, без ожидания в очереди)
BANNER = os.path.join(ROOT, "bot", "assets", "banner.png")   # картинка /start
POLL_EVERY = 1.5       # секунд между запросами /api/result
LONG_POLL = 30         # getUpdates timeout
COMMANDS = [{"command": "start", "description": "What this bot does"},
            {"command": "scan", "description": "Scan a token: /scan <address>"},
            {"command": "help", "description": "How to read a verdict"}]

_log_lock = threading.Lock()


def log(msg):
    with _log_lock:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def parse_command(text):
    """"/scan@bot 0x..." → ("scan", "bot", "0x..."); не команда → (None, None, text)."""
    if not text.startswith("/"):
        return None, None, text
    head, _, arg = text.partition(" ")
    cmd, _, target = head[1:].partition("@")
    return cmd.lower(), target or None, arg.strip()


class Bot:
    def __init__(self, tg, api, username="", workers=WORKERS, cooldown=USER_COOLDOWN, scan_timeout=SCAN_TIMEOUT,
                 poll_every=POLL_EVERY, clock=time.monotonic, sleep=time.sleep, log=log, banner=BANNER):
        self.tg, self.api, self.username = tg, api, username
        self.workers, self.cooldown, self.scan_timeout, self.poll_every = workers, cooldown, scan_timeout, poll_every
        self.clock, self.sleep, self.log = clock, sleep, log
        self.jobs = queue.Queue()
        self.busy = 0                # сканов в работе
        self.last_scan = {}          # user_id -> clock() последнего принятого скана
        self.lock = threading.Lock()
        self.banner = banner
        self.banner_id = None        # file_id баннера после первой загрузки: дальше шлём без файла

    # --- Telegram ---------------------------------------------------------------------------------

    def call(self, method, upload=None, **params):
        """Вызов Bot API; ошибка логируется, бот живёт. → result или None.
        upload=(поле, путь) — с файлом (multipart)."""
        try:
            if upload:
                return self.tg.upload(method, *upload, **params)
            return self.tg.call(method, **params)
        except TelegramError as e:
            if "message is not modified" not in e.description:
                self.log(f"telegram {method}: {e}")
        except Exception as e:
            self.log(f"telegram {method}: {type(e).__name__}: {self.tg.redact(e)}")
        return None

    def send(self, chat_id, html, markup=None, reply_to=None):
        msg = self.call("sendMessage", chat_id=chat_id, text=html, parse_mode="HTML", reply_markup=markup,
                        link_preview_options={"is_disabled": True},
                        reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
                        if reply_to else None)
        return msg.get("message_id") if isinstance(msg, dict) else None

    def send_start(self, chat_id):
        """Баннер с приветствием в подписи; по file_id, если баннер уже загружали.
        Нет файла или sendPhoto не прошёл — то же приветствие обычным сообщением."""
        params = dict(chat_id=chat_id, caption=T.START, parse_mode="HTML", reply_markup=T.START_BUTTONS)
        msg = None
        if self.banner_id:
            msg = self.call("sendPhoto", photo=self.banner_id, **params)
            if msg is None:
                self.banner_id = None    # file_id не принят — в следующий раз загрузим файл заново
        elif self.banner and os.path.isfile(self.banner):
            msg = self.call("sendPhoto", upload=("photo", self.banner), **params)
            sizes = msg.get("photo") if isinstance(msg, dict) else None
            if sizes:
                self.banner_id = sizes[-1]["file_id"]   # самый большой размер
        if msg is None:
            return self.send(chat_id, T.START, T.START_BUTTONS)
        return msg.get("message_id") if isinstance(msg, dict) else None

    def edit(self, chat_id, message_id, html, markup=None):
        self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=html, parse_mode="HTML",
                  reply_markup=markup, link_preview_options={"is_disabled": True})

    # --- обновления -------------------------------------------------------------------------------

    def handle_update(self, u):
        if "message" in u:
            self.handle_message(u["message"])
        elif "callback_query" in u:
            self.handle_callback(u["callback_query"])

    def handle_message(self, m):
        chat, user = m.get("chat") or {}, m.get("from") or {}
        txt = (m.get("text") or "").strip()
        if not txt or user.get("is_bot"):
            return
        cmd, target, arg = parse_command(txt)
        if target and target.lower() != self.username.lower():
            return                               # команда другому боту
        chat_id, mid = chat.get("id"), m.get("message_id")
        if chat.get("type") == "private":
            if cmd == "start":
                return self.send_start(chat_id)
            if cmd == "help":
                return self.send(chat_id, T.HELP)
            if cmd == "scan":
                return self.scan_command(chat_id, user.get("id"), arg, None)
            found = T.find_address(txt) if cmd is None else None
            if found:
                return self.request_scan(chat_id, user.get("id"), found[1], None)
            return self.send(chat_id, T.HINT)
        if chat.get("type") in ("group", "supergroup") and cmd == "scan":
            return self.scan_command(chat_id, user.get("id"), arg, mid)

    def handle_callback(self, cq):
        self.call("answerCallbackQuery", callback_query_id=cq.get("id"))
        chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id")
        if chat_id is None:
            return
        if cq.get("data") == "scan":
            self.send(chat_id, T.ASK_ADDRESS)
        elif cq.get("data") == "help":
            self.send(chat_id, T.HELP)

    def scan_command(self, chat_id, user_id, arg, reply_to):
        found = T.find_address(arg) if arg else None
        if not found:
            return self.send(chat_id, T.SCAN_USAGE, reply_to=reply_to)
        self.request_scan(chat_id, user_id, found[1], reply_to)

    # --- сканы ------------------------------------------------------------------------------------

    def request_scan(self, chat_id, user_id, addr, reply_to):
        """Лимиты и очередь: сразу ответ crawling/queued, скан — в рабочем потоке."""
        now = self.clock()
        with self.lock:
            last = self.last_scan.get(user_id)
            if last is not None and now - last < self.cooldown:
                left = math.ceil(self.cooldown - (now - last))
                busy = None
            elif self.jobs.qsize() >= MAX_QUEUE:
                left, busy = None, True
            else:
                left, busy = None, False
                self.last_scan[user_id] = now
                waiting = self.busy + self.jobs.qsize() >= self.workers
        if left is not None:
            return self.send(chat_id, T.wait(left), reply_to=reply_to)
        if busy:
            return self.send(chat_id, T.BUSY, reply_to=reply_to)
        self.log(f"scan {addr} chat {chat_id}" + (" queued" if waiting else ""))
        mid = self.send(chat_id, T.queued(addr) if waiting else T.crawling(addr), reply_to=reply_to)
        if mid is None:
            return
        self.jobs.put((chat_id, mid, addr, waiting))

    def worker(self):
        while True:
            job = self.jobs.get()
            with self.lock:
                self.busy += 1
            try:
                self.run_scan(*job)
            except Exception:
                self.log("scan crashed:\n" + self.tg.redact(traceback.format_exc()))
            finally:
                with self.lock:
                    self.busy -= 1
                self.jobs.task_done()

    def run_scan(self, chat_id, mid, addr, was_queued):
        if was_queued:
            self.edit(chat_id, mid, T.crawling(addr))
        html, markup = self.scan_text(addr)
        self.edit(chat_id, mid, html, markup)

    def scan_text(self, addr):
        """Скан через API сайта → (html, кнопки)."""
        deadline = self.clock() + self.scan_timeout
        try:
            job = self.api.scan(addr)
            while True:
                r = self.api.result(job)
                if r.get("done"):
                    break
                if self.clock() >= deadline:
                    return T.TIMEOUT, None
                self.sleep(self.poll_every)
        except Rejected as e:
            return T.rejected(addr, str(e)), None
        except ApiError as e:
            self.log(f"api {addr}: {e}")
            return T.UNREACHABLE, None
        if r.get("error") or not isinstance(r.get("result"), dict):
            self.log(f"scan {addr}: {r.get('error')}")
            return T.scan_error(addr, r.get("error")), None
        res = r["result"]
        return T.verdict(res), T.report_button(res.get("token") or addr)

    # --- цикл опроса ------------------------------------------------------------------------------

    def start_workers(self):
        for _ in range(self.workers):
            threading.Thread(target=self.worker, daemon=True).start()

    def poll_once(self, offset):
        """Один getUpdates → новый offset. Ошибки обработки одного апдейта не мешают остальным."""
        updates = self.tg.call("getUpdates", http_timeout=LONG_POLL + 10, offset=offset, timeout=LONG_POLL,
                               allowed_updates=["message", "callback_query"]) or []
        for u in updates:
            offset = max(offset or 0, u.get("update_id", 0) + 1)
            try:
                self.handle_update(u)
            except Exception:
                self.log("update failed:\n" + self.tg.redact(traceback.format_exc()))
        return offset

    def run(self):
        while not self.username:
            me = self.call("getMe")
            if me:
                self.username = me.get("username") or ""
            else:
                self.sleep(5)
        self.call("setMyCommands", commands=COMMANDS)
        self.start_workers()
        self.log(f"@{self.username} polling, site {self.api.base}")
        offset, backoff = None, 1
        while True:
            try:
                offset = self.poll_once(offset)
                backoff = 1
            except TelegramError as e:
                if e.code == 409:
                    self.log("getUpdates: 409 Conflict — another instance of this bot is running")
                    self.sleep(5)
                    continue
                self.log(f"getUpdates: {e}")
                self.sleep(backoff)
                backoff = min(30, backoff * 2)
            except Exception:
                self.log("poll loop:\n" + self.tg.redact(traceback.format_exc()))
                self.sleep(backoff)
                backoff = min(30, backoff * 2)


def main():
    env.load_dotenv()
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not token:
        print("TG_BOT_TOKEN is not set", file=sys.stderr)
        sys.exit(1)
    api = CrawlScan(os.environ.get("CRAWLSCAN_API", "").strip() or "https://crawlscan.fun")
    try:
        Bot(Telegram(token), api).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
