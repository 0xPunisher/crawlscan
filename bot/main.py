"""Telegram-бот CrawlScan — отдельный сервис, тонкий клиент API сайта (бот сам в блокчейн не ходит).

  python bot/main.py

env: TG_BOT_TOKEN (обязателен, из .env через env.py; нигде не печатается), CRAWLSCAN_API (по умолчанию
https://crawlscan.fun), TRADE_URL_ROBINHOOD / TRADE_URL_SOLANA — шаблоны кнопки Trade on Axiom (trade.py),
ALERTS_API_SECRET — секрет API подписок сайта (нет — алертов и Premium в боте нет; нигде не печатается),
PREMIUM_ADMIN_ID — Telegram ID админов Premium через запятую (/admin_unlink). Long polling getUpdates (timeout=30): одновременно может работать только один
экземпляр бота, второй получает 409 Conflict.

Личка: адрес токена (или /scan <адрес>) → скан; /start, /help, /rewards. Группы: только /scan <адрес>
и /rewards (и /scan@имябота, /rewards@имябота). Алерты (только личка, если на сайте /api/config → alerts и задан
ALERTS_API_SECRET): /watch <адрес>, /watchlist, /unwatch <адрес>, кнопка [Watch] под вердиктом, [Unwatch] под
уведомлением (уведомления шлёт сайт); иначе — «coming soon». Premium (только личка, если /api/config → premium):
/verify [кошелёк] — бронь кошелька на 15 минут (покупку ищет и сообщение шлёт сайт), /premium, /unlink,
/admin_unlink <кошелёк> (только PREMIUM_ADMIN_ID); значок ⭐ Premium под вердиктом. Функции премиума — по полям
/api/config: premium_priority (Telegram ID в /api/scan), premium_import ([Pick tokens] и /picktokens в /premium и Watchlist),
premium_digest (/digest on|off). Выключен — как раньше (подсказка). /rewards — статус наград и сжиганий с сайта (/api/rewards/status). Скан: сразу ответ "crawling…", потом это же сообщение редактируется в вердикт.
Лимиты: 1 скан на пользователя в USER_COOLDOWN секунд, не больше WORKERS сканов одновременно (остальные — в очереди).
"""
import math, os, queue, sys, threading, time, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import env                                          # noqa: E402
import trade                                        # noqa: E402
from bot import text as T                           # noqa: E402
from bot.api import AlertsOff, ApiError, Busy, CrawlScan, PremiumOff, Rejected, TooFast   # noqa: E402
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
            {"command": "help", "description": "How to read a verdict"},
            {"command": "rewards", "description": "Next holder draw, last winner, burns"}]
WATCH_COMMANDS = [{"command": "watch", "description": "Get alerts for a token: /watch <address>"},
                  {"command": "watchlist", "description": "Tokens you watch"},
                  {"command": "unwatch", "description": "Stop alerts: /unwatch <address>"}]
PREMIUM_COMMANDS = [{"command": "verify", "description": "Link your $CrawlScan wallet for Premium"},
                    {"command": "premium", "description": "Premium features and your status"},
                    {"command": "unlink", "description": "Unlink your wallet"}]
DIGEST_COMMAND = {"command": "digest", "description": "Morning digest of your watchlist: /digest on|off"}
PICK_COMMAND = {"command": "picktokens", "description": "Pick tokens from your wallet to watch"}
DEV_COMMAND = {"command": "dev", "description": "What else this token's dev launched: /dev <address>"}
TRENDING_COMMAND = {"command": "trending", "description": "Most scanned tokens right now"}
PREMIUM_CMDS = ("verify", "premium", "unlink", "admin_unlink", "digest")
PICK_CMDS = ("picktokens", "import")   # /import — прежнее название (Import from wallet)
PREMIUM_FEATURES = ("premium_priority", "premium_import", "premium_digest", "premium_devcheck", "premium_memory",
                    "premium_trending", "premium_fresh", "premium_bot_rate")   # поля /api/config (только включённые)
SCAN_USER_FEATURES = ("premium_priority", "premium_bot_rate", "premium_fresh")   # с ними /api/scan получает user_id
WATCHLIST_BUTTON = {"inline_keyboard": [[{"text": "🔔 Watchlist", "callback_data": "watchlist"}]]}
ALERTS_CHECK_TTL = 60  # секунд: сколько помнить, включены ли алерты на сайте
PREMIUM_TTL = 300      # секунд: сколько помнить, премиум ли пользователь (значок под вердиктом)
AWAIT_WATCH = 300      # секунд: после [➕ New] следующий адрес от чата — подписка, а не скан
TICKERS_MAX = 2000     # тикеров из сканов в памяти (для списка подписок)

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
                 poll_every=POLL_EVERY, clock=time.monotonic, sleep=time.sleep, log=log, banner=BANNER, spawn=None, trade_urls=None,
                 admin_ids=None):
        self.tg, self.api, self.username = tg, api, username
        self.workers, self.cooldown, self.scan_timeout, self.poll_every = workers, cooldown, scan_timeout, poll_every
        self.clock, self.sleep, self.log = clock, sleep, log
        self.jobs = queue.Queue()
        self.busy = 0                # сканов в работе
        self.last_scan = {}          # user_id -> clock() последнего принятого скана
        self.lock = threading.Lock()
        self.banner = banner
        self.banner_id = None        # file_id баннера после первой загрузки: дальше шлём без файла
        # запросы к сайту вне цикла опроса (/rewards): по умолчанию — свой поток
        self.trade_urls = trade_urls or trade.templates()   # {сеть: шаблон} для [Trade on Axiom]
        self.spawn = spawn or (lambda f: threading.Thread(target=f, daemon=True).start())
        self.alerts_seen = None      # (clock(), включены ли алерты на сайте)
        self.flap_seen = None        # (clock(), (Flap, Bankr) включены на сайте: строка о площадках в /help)
        self.awaiting = {}           # chat_id -> clock() до которого ждём адрес для подписки ([➕ New])
        self.tickers = {}            # адрес токена -> тикер из последнего скана (сайт тикеры подписок не хранит)
        self.premium_seen = None     # (clock(), включён ли Premium на сайте) — вместе с alerts_seen
        self.premium_feats = set()   # включённые функции премиума (/api/config: premium_priority, ...) — с alerts_seen
        self.premium_users = {}      # user_id -> (clock(), премиум ли): значок под вердиктом
        self.awaiting_wallet = {}    # chat_id -> clock() до которого ждём адрес кошелька после /verify
        self.admin_ids = admin_ids if admin_ids is not None else admin_ids_env()

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
        Нет файла или sendPhoto не прошёл — то же приветствие обычным сообщением.
        Алерты включены — кнопка [🔔 Watchlist]."""
        buttons = T.start_buttons(self.alerts_on(), self.premium_on())
        params = dict(chat_id=chat_id, caption=T.START, parse_mode="HTML", reply_markup=buttons)
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
            return self.send(chat_id, T.START, buttons)
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
            if cmd is None and self.awaiting_verify(chat_id):     # после /verify: адрес — кошелёк, не скан
                return self.premium_command(chat_id, user.get("id"), "verify", txt)
            if cmd in PREMIUM_CMDS:
                return self.premium_command(chat_id, user.get("id"), cmd, arg)
            if cmd in PICK_CMDS:   # выключено (или Premium выключен) — подсказка, как у неизвестной команды
                return self.import_command(chat_id, user.get("id"))
            if cmd in ("dev", "trending"):
                return self.extra_command(chat_id, user.get("id"), cmd, arg)
            if cmd is None and self.awaiting_watch(chat_id):       # после [➕ New]: адрес — подписка, не скан
                found = T.find_address(txt)
                if not found:
                    return self.send(chat_id, T.ASK_WATCH_AGAIN)
                self.awaiting.pop(chat_id, None)
                return self.alerts_command(chat_id, "new", found[1])
            if cmd == "start":   # проверка алертов (кнопка Watchlist) может сходить на сайт — не в цикле опроса
                return self.spawn(lambda: self.send_start(chat_id))
            if cmd == "help":   # флаг Flap может сходить на сайт — не в цикле опроса
                return self.spawn(lambda: self.send(chat_id, T.help_text(*self.launchpads())))
            if cmd == "scan":
                return self.scan_command(chat_id, user.get("id"), arg, None, private=True)
            if cmd == "rewards":
                return self.rewards_command(chat_id, None)
            if cmd in ("watch", "unwatch", "watchlist"):
                return self.alerts_command(chat_id, cmd, arg)
            found = T.find_address(txt) if cmd is None else None
            if found:
                return self.request_scan(chat_id, user.get("id"), found[1], None, private=True)
            return self.send(chat_id, T.HINT)
        if chat.get("type") in ("group", "supergroup") and cmd == "scan":
            return self.scan_command(chat_id, user.get("id"), arg, mid)
        if chat.get("type") in ("group", "supergroup") and cmd == "rewards":
            return self.rewards_command(chat_id, mid)

    def handle_callback(self, cq):
        self.call("answerCallbackQuery", callback_query_id=cq.get("id"))
        chat = (cq.get("message") or {}).get("chat") or {}
        chat_id, data = chat.get("id"), cq.get("data") or ""
        if chat_id is None:
            return
        cmd, _, addr = data.partition(":")
        private = chat.get("type") == "private"
        if cmd in ("watch", "unwatch", "rm") and addr and private:
            # [Watch] под вердиктом, [Unwatch] под уведомлением сайта (alerts_notify), [Remove] в списке подписок
            found = T.find_address(addr)
            if found:
                mid = (cq.get("message") or {}).get("message_id") if cmd == "rm" else None
                self.alerts_command(chat_id, "remove" if cmd == "rm" else cmd, found[1], edit=mid)
        elif data == "watchlist" and private:
            self.alerts_command(chat_id, "watchlist", "")
        elif cmd in ("dev", "fresh", "sc") and addr and private:
            # [Dev history] и [Fresh scan] под вердиктом премиума, [Scan $X] в Trending
            uid = (cq.get("from") or {}).get("id", chat_id)
            found = T.find_address(addr)
            if found and cmd == "dev":
                self.extra_command(chat_id, uid, "dev", found[1])
            elif found:
                self.request_scan(chat_id, uid, found[1], None, private=True, fresh=cmd == "fresh")
        elif data == "trending" and private:
            self.extra_command(chat_id, (cq.get("from") or {}).get("id", chat_id), "trending", "")
        elif data in ("pick", "imp") and private:     # imp — кнопка прежних сообщений (Import from wallet)
            self.import_command(chat_id, (cq.get("from") or {}).get("id", chat_id))
        elif cmd in ("premium", "verify", "unlink", "digest") and private:
            # [⭐ Premium features] в /start, кнопки под /premium: [Verify wallet], [Unlink] → подтверждение,
            # [Daily digest: on/off], [Cancel] под подтверждением (premium:cancel — это же сообщение)
            uid = (cq.get("from") or {}).get("id", chat_id)
            mid = (cq.get("message") or {}).get("message_id")
            if data == "premium":
                self.premium_command(chat_id, uid, "premium", "")
            elif data == "premium:cancel":
                self.premium_command(chat_id, uid, "premium", "", edit=mid)
            elif data == "verify":
                self.premium_command(chat_id, uid, "verify", "")
            elif data == "unlink":
                self.premium_command(chat_id, uid, "unlink_ask", "")
            elif data == "unlink:yes":
                self.premium_command(chat_id, uid, "unlink", "", edit=mid)
            elif data in ("digest:on", "digest:off"):
                self.premium_command(chat_id, uid, "digest_toggle", addr, edit=mid)
        elif cmd == "ia" and addr and private:
            found = T.find_address(addr) if addr != "all" else (None, "all")
            if found:
                self.import_command(chat_id, (cq.get("from") or {}).get("id", chat_id),
                                    "all" if addr == "all" else [found[1]])
        elif data == "new" and private:
            self.awaiting[chat_id] = self.clock() + AWAIT_WATCH    # сразу: адрес может прийти раньше ответа сайта
            self.awaiting_wallet.pop(chat_id, None)
            self.alerts_command(chat_id, "ask", "")
        elif data == "scan":
            self.send(chat_id, T.ASK_ADDRESS)
        elif data == "help":
            self.spawn(lambda: self.send(chat_id, T.help_text(*self.launchpads())))

    def scan_command(self, chat_id, user_id, arg, reply_to, private=False):
        found = T.find_address(arg) if arg else None
        if not found:
            return self.send(chat_id, T.SCAN_USAGE, reply_to=reply_to)
        self.request_scan(chat_id, user_id, found[1], reply_to, private)

    def rewards_command(self, chat_id, reply_to):
        self.spawn(lambda: self.send(chat_id, *self.rewards_text(), reply_to=reply_to))

    def rewards_text(self):
        """/api/rewards/status сайта → (html, кнопки). Сайт недоступен — UNREACHABLE без кнопок."""
        try:
            st = self.api.rewards_status()
        except (ApiError, Rejected) as e:
            self.log(f"api rewards: {e}")
            return T.UNREACHABLE, None
        try:
            return T.rewards(st), T.REWARDS_BUTTON
        except Exception:
            self.log("rewards text:\n" + self.tg.redact(traceback.format_exc()))
            return T.UNREACHABLE, None

    # --- алерты: подписки (сайт хранит, бот только просит) ---------------------------------------------

    def alerts_on(self):
        """Алерты включены: у бота есть секрет и сайт отвечает /api/config → alerts. Помним ALERTS_CHECK_TTL секунд;
        сайт недоступен — выключены (и тоже помним)."""
        if not getattr(self.api, "alerts_secret", ""):
            return False
        now = self.clock()
        seen = self.alerts_seen
        if seen and now - seen[0] < ALERTS_CHECK_TTL:
            return seen[1]
        try:
            cfg = self.api.config()
            on, prem = bool(cfg.get("alerts")), bool(cfg.get("premium"))
            feats = {k for k in PREMIUM_FEATURES if cfg.get(k)} if prem else set()
        except (ApiError, Rejected, AttributeError) as e:
            self.log(f"api config: {e}")
            on = prem = False
            feats = set()
        self.alerts_seen, self.premium_seen, self.premium_feats = (now, on), (now, prem), feats
        return on

    def launchpads(self):
        """(Flap, Bankr) включены на сайте (/api/config → flap, bankr). Помним ALERTS_CHECK_TTL секунд;
        сайт недоступен — нет."""
        now = self.clock()
        if self.flap_seen and now - self.flap_seen[0] < ALERTS_CHECK_TTL:
            return self.flap_seen[1]
        try:
            cfg = self.api.config()
            on = (bool(cfg.get("flap")), bool(cfg.get("bankr")))
        except (ApiError, Rejected, AttributeError) as e:
            self.log(f"api config: {e}")
            on = (False, False)
        self.flap_seen = (now, on)
        return on

    def flap_on(self):
        return self.launchpads()[0]

    def awaiting_watch(self, chat_id):
        """Ждём ли от чата адрес для подписки ([➕ New], AWAIT_WATCH секунд); истекло — сбрасываем."""
        until = self.awaiting.get(chat_id)
        if until is None:
            return False
        if self.clock() >= until:
            self.awaiting.pop(chat_id, None)
            return False
        return True

    def alerts_command(self, chat_id, cmd, arg, edit=None):
        """Подписки — запрос к сайту в отдельном потоке. edit — message_id: обновить это сообщение ([Remove]).
        cmd: watch / unwatch / watchlist (команды и кнопки), new (адрес после [➕ New]), ask ([➕ New]),
        remove ([Remove] в списке)."""
        def run():
            html, markup = self.alerts_text(chat_id, cmd, arg)
            if edit:
                self.edit(chat_id, edit, html, markup)
            else:
                self.send(chat_id, html, markup)
        self.spawn(run)

    def alerts_text(self, chat_id, cmd, arg, now=None):
        """→ (html, кнопки)."""
        if not self.alerts_on():
            self.awaiting.pop(chat_id, None)
            return T.ALERTS_SOON, None
        now = time.time() if now is None else now
        found = T.find_address(arg) if arg else None
        if cmd in ("watch", "unwatch", "new", "remove") and not found:
            return (T.UNWATCH_USAGE if cmd == "unwatch" else T.WATCH_USAGE), None
        try:
            if cmd == "watchlist":
                return self.with_import(chat_id, T.watchlist_view(self.api.watch_list(chat_id), now, self.tickers))
            if cmd == "ask":
                r = self.api.watch_list(chat_id)
                if len(r.get("items") or []) >= r.get("limit", 3):
                    self.awaiting.pop(chat_id, None)
                    return T.watch_limit_view(r, now, self.tickers)
                return T.ASK_WATCH, None
            if cmd == "unwatch":
                return T.unwatched(self.api.unwatch(chat_id, found[1])), None
            if cmd == "remove":
                self.api.unwatch(chat_id, found[1])
                self.log(f"unwatch {found[1]} chat {chat_id}")
                return self.with_import(chat_id, T.watchlist_view(self.api.watch_list(chat_id), now, self.tickers))
            r = self.api.watch(chat_id, found[1])
            if r.get("status") == 409:
                if cmd == "new":
                    return T.watch_limit_view(r, now, self.tickers)
                return T.watch_limit(r, now), None
            if r.get("status") == 422:
                if r.get("error") == "too active":
                    return T.watch_active(found[1]), None
                return T.watch_established(found[1]), None
            self.log(f"watch {found[1]} chat {chat_id}" + (" renewed" if r.get("renewed") else ""))
            return T.watching(r), None
        except Rejected as e:
            return T.rejected(found[1] if found else "", str(e)), None
        except AlertsOff:
            self.alerts_seen = (self.clock(), False)
            self.awaiting.pop(chat_id, None)
            return T.ALERTS_SOON, None
        except ApiError as e:
            self.log(f"api {cmd}: {e}")
            return T.UNREACHABLE, None
        except Exception:
            self.log(f"{cmd} text:\n" + self.tg.redact(traceback.format_exc()))
            return T.UNREACHABLE, None

    # --- Premium (сайт хранит брони и привязки, ищет покупку и шлёт сообщения; бот только просит) ---------

    def premium_on(self):
        """Premium включён: у бота есть секрет и сайт отвечает /api/config → premium. Тот же запрос и кэш, что у
        alerts_on (ALERTS_CHECK_TTL)."""
        if not getattr(self.api, "alerts_secret", ""):
            return False
        seen = self.premium_seen
        if not (seen and self.clock() - seen[0] < ALERTS_CHECK_TTL):
            self.alerts_seen = None
            self.alerts_on()
        return bool(self.premium_seen and self.premium_seen[1])

    def feature(self, name):
        """Функция премиума включена на сайте (premium_priority / premium_import / premium_digest): тот же запрос
        /api/config и кэш, что у premium_on. Выключена — бот ведёт себя как раньше."""
        return self.premium_on() and name in self.premium_feats

    def is_premium(self, user_id):
        """Премиум ли пользователь (значок под вердиктом): /api/premium/status, помним PREMIUM_TTL секунд.
        Выключено, сбой — нет."""
        if user_id is None or not self.premium_on():
            return False
        now = self.clock()
        seen = self.premium_users.get(user_id)
        if seen and now - seen[0] < PREMIUM_TTL:
            return seen[1]
        try:
            on = bool(self.api.premium_status(user_id).get("premium"))
        except (ApiError, Rejected, PremiumOff, AttributeError) as e:
            self.log(f"api premium status: {e}")
            on = False
        if len(self.premium_users) > 10000:
            self.premium_users.clear()
        self.premium_users[user_id] = (now, on)
        return on

    def awaiting_verify(self, chat_id):
        """Ждём ли от чата адрес кошелька после /verify (AWAIT_WATCH секунд); истекло — сбрасываем."""
        until = self.awaiting_wallet.get(chat_id)
        if until is None:
            return False
        if self.clock() >= until:
            self.awaiting_wallet.pop(chat_id, None)
            return False
        return True

    def premium_command(self, chat_id, user_id, cmd, arg, edit=None):
        """/verify, /premium, /unlink, /admin_unlink, /digest и кнопки Premium — запрос к сайту в отдельном потоке.
        edit — message_id: обновить это сообщение (подтверждение Unlink, переключатель digest)."""
        def run():
            html, markup = self.premium_text(chat_id, user_id, cmd, arg)
            if edit:
                self.edit(chat_id, edit, html, markup)
            else:
                self.send(chat_id, html, markup)
        self.spawn(run)

    def buy_url(self):
        """[Buy $CrawlScan] под /premium: Trade on Axiom для $CrawlScan (шаблон trade.py)."""
        return trade.url("robinhood", T.OFFICIAL_CA.lower(), self.trade_urls)

    def premium_text(self, chat_id, user_id, cmd, arg, now=None):
        """→ (html, кнопки). Premium выключен или /admin_unlink не от админа — подсказка, как раньше."""
        if cmd == "admin_unlink" and user_id not in self.admin_ids:
            return T.HINT, None
        if not self.premium_on():
            self.awaiting_wallet.pop(chat_id, None)
            return T.HINT, None
        if cmd in ("digest", "digest_toggle") and not self.feature("premium_digest"):
            return T.HINT, None
        now = time.time() if now is None else now
        wallet = (arg or "").strip()
        valid = bool(T.ADDR_RE.match(wallet))
        try:
            if cmd == "digest_toggle":            # [Daily digest: on/off] → обновлённый /premium в том же сообщении
                self.api.premium_digest(user_id, arg == "on")
                self.log(f"digest {arg}")
                return T.premium_view(self.api.premium_status(user_id), now, self.buy_url())
            if cmd == "unlink_ask":
                st = self.api.premium_status(user_id)
                if not st.get("linked"):
                    return T.NO_WALLET, None
                return T.UNLINK_CONFIRM.format(wallet=T.e(st["wallet"])), T.UNLINK_BUTTONS
            if cmd == "digest":
                want = {"on": True, "off": False, "": None}.get(wallet.lower(), "usage")
                if want == "usage":
                    return T.DIGEST_USAGE, None
                r = self.api.premium_digest(user_id, want)
                if want is not None:
                    self.log(f"digest {'on' if want else 'off'}")
                return T.digest_state(r), None
            if cmd == "verify":
                if not valid:
                    self.awaiting_wallet[chat_id] = self.clock() + AWAIT_WATCH
                    self.awaiting.pop(chat_id, None)
                    return (T.ASK_WALLET_AGAIN if wallet else T.ASK_WALLET), None
                self.awaiting_wallet.pop(chat_id, None)
                r = self.api.premium_reserve(user_id, wallet)
                if r.get("status") == 409:
                    return T.WALLET_TAKEN, None
                if r.get("status") == 429:
                    return T.TOO_MANY_VERIFY, None
                self.log(f"verify {T.short(wallet)} {r.get('state')}")
                return T.reserved(r), (None if r.get("state") == "yours" else T.BUY_BUTTONS)
            if cmd == "premium":
                st = self.api.premium_status(user_id)
                self.premium_users[user_id] = (self.clock(), bool(st.get("premium")))
                return T.premium_view(st, now, self.buy_url())
            if cmd == "unlink":
                r = self.api.premium_unlink(user_id)
                self.premium_users.pop(user_id, None)
                return T.unlinked(r), None
            if not valid:
                return T.ADMIN_UNLINK_USAGE, None
            r = self.api.premium_admin_unlink(wallet)
            self.log(f"admin_unlink {T.short(wallet)} found {r.get('found')}")
            return T.admin_unlinked(r), None
        except Rejected:
            return T.ASK_WALLET_AGAIN, None
        except PremiumOff:
            self.alerts_seen = self.premium_seen = None
            self.awaiting_wallet.pop(chat_id, None)
            return T.HINT, None
        except ApiError as e:
            self.log(f"api {cmd}: {e}")
            return T.UNREACHABLE, None
        except Exception:
            self.log(f"{cmd} text:\n" + self.tg.redact(traceback.format_exc()))
            return T.UNREACHABLE, None

    # --- Premium: импорт из кошелька (сайт смотрит кошелёк и добавляет; бот только просит) --------------

    def import_command(self, chat_id, user_id, tokens=None):
        """[Pick tokens] и /picktokens (tokens=None), [➕ …] / [➕ Add all] (tokens — список адресов или "all")."""
        def run():
            html, markup = self.import_text(user_id, tokens)
            self.send(chat_id, html, markup)
        self.spawn(run)

    def import_text(self, user_id, tokens=None):
        """→ (html, кнопки). PREMIUM_IMPORT выключен — подсказка, как раньше."""
        if not self.feature("premium_import"):
            return T.HINT, None
        try:
            if tokens is None:
                r = self.api.premium_import(user_id)
                if r.get("status") == 429:
                    return T.import_wait(r.get("retry_in") or 600), None
                if r.get("status") == 403:
                    return (T.IMPORT_NOT_PREMIUM if r.get("error") == "not premium" else T.UNREACHABLE), None
                self.log(f"import {len(r.get('items') or [])} shown" + (" cached" if r.get("cached") else ""))
                return T.import_view(r)
            r = self.api.premium_import_add(user_id, tokens)
            if r.get("status") == 410:
                return T.IMPORT_EXPIRED, {"inline_keyboard": [[T.IMPORT_BUTTON]]}
            if r.get("status") == 403:
                return (T.IMPORT_NOT_PREMIUM if r.get("error") == "not premium" else T.UNREACHABLE), None
            self.log(f"import added {len(r.get('added') or [])}")
            return T.import_added(r), WATCHLIST_BUTTON
        except PremiumOff:
            self.alerts_seen = self.premium_seen = None
            return T.HINT, None
        except (ApiError, Rejected) as e:
            self.log(f"api import: {e}")
            return T.UNREACHABLE, None
        except Exception:
            self.log("import text:\n" + self.tg.redact(traceback.format_exc()))
            return T.UNREACHABLE, None

    # --- Premium: Dev history, Trending (сайт читает memory.db / свой счётчик; бот только просит) ----------

    def extra_command(self, chat_id, user_id, cmd, arg):
        """/dev <адрес>, [Dev history], /trending, [Trending] — запрос к сайту в отдельном потоке."""
        def run():
            html, markup = self.extra_text(user_id, cmd, arg)
            self.send(chat_id, html, markup)
        self.spawn(run)

    def extra_text(self, user_id, cmd, arg):
        """→ (html, кнопки). Функция выключена (или Premium) — подсказка, как у неизвестной команды."""
        if not self.feature("premium_devcheck" if cmd == "dev" else "premium_trending"):
            return T.HINT, None
        try:
            if cmd == "dev":
                found = T.find_address(arg) if arg else None
                if not found:
                    return T.DEV_USAGE, None
                r = self.api.premium_dev(user_id, found[1])
                if r.get("status") == 403:
                    return T.PREMIUM_ONLY, None
                return T.dev_view(r), None
            r = self.api.premium_trending(user_id)
            if r.get("status") == 403:
                return T.PREMIUM_ONLY, None
            return T.trending_view(r)
        except PremiumOff:
            self.alerts_seen = self.premium_seen = None
            return T.HINT, None
        except Rejected as e:
            return T.rejected(arg, str(e)), None
        except ApiError as e:
            self.log(f"api {cmd}: {e}")
            return T.UNREACHABLE, None
        except Exception:
            self.log(f"{cmd} text:\n" + self.tg.redact(traceback.format_exc()))
            return T.UNREACHABLE, None

    def memory_text(self, user_id, token):
        """Блок Memory под вердиктом премиума (PREMIUM_MEMORY_INSIGHTS) — отдельный запрос после вердикта;
        ничего примечательного, сбой, выключено — None."""
        try:
            r = self.api.premium_memory(user_id, token)
        except (ApiError, Rejected, PremiumOff) as e:
            self.log(f"api memory: {e}")
            return None
        return None if r.get("status") else T.memory_block(r)

    def with_import(self, chat_id, view):
        """Watchlist премиум-холдера при PREMIUM_IMPORT — ещё ряд [Pick tokens]; иначе как был."""
        html, markup = view
        if markup is not None and self.feature("premium_import") and self.is_premium(chat_id):
            markup = {"inline_keyboard": markup["inline_keyboard"] + [[T.IMPORT_BUTTON]]}
        return html, markup

    # --- сканы ------------------------------------------------------------------------------------

    def request_scan(self, chat_id, user_id, addr, reply_to, private=False, fresh=False):
        """Лимиты и очередь: сразу ответ crawling/queued, скан — в рабочем потоке. При PREMIUM_BOT_RATE
        (по последнему /api/config, без запроса в цикле опроса) свой лимит бота 1 скан в USER_COOLDOWN не
        действует: новые сканы считает сайт по Telegram ID, ответы из кэша лимит не тратят.
        fresh — [Fresh scan] премиума (мимо кэша результата сайта)."""
        now = self.clock()
        site_limit = "premium_bot_rate" in self.premium_feats
        with self.lock:
            last = self.last_scan.get(user_id)
            if not site_limit and last is not None and now - last < self.cooldown:
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
        self.jobs.put((chat_id, mid, addr, waiting, private, user_id) + ((True,) if fresh else ()))

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

    def run_scan(self, chat_id, mid, addr, was_queued, private=False, user_id=None, fresh=False):
        if was_queued:
            self.edit(chat_id, mid, T.crawling(addr))
        html, markup, token = self._scan(addr, watch=private, user_id=user_id, fresh=fresh)
        verdict = markup is not None and markup is not T.PREMIUM_UPSELL   # вердикт, а не ошибка / лимит
        premium_user = verdict and self.is_premium(user_id)               # ... премиум-пользователю
        if premium_user:
            html += "\n\n" + T.PREMIUM_BADGE
            row = T.premium_row(token, dev=private and self.feature("premium_devcheck"),
                                fresh=private and self.feature("premium_fresh"))
            if row:
                markup = {"inline_keyboard": markup["inline_keyboard"] + [row]}
        self.edit(chat_id, mid, html, markup)
        if premium_user and private and self.feature("premium_memory"):   # после вердикта, отдельным запросом
            block = self.memory_text(user_id, token)
            if block:
                self.edit(chat_id, mid, html + "\n\n" + block, markup)

    def scan_text(self, addr, watch=False, user_id=None):
        """Скан через API сайта → (html, кнопки). watch — личка: кнопка [Watch], если алерты включены
        и токен не too established. user_id — при PREMIUM_PRIORITY / PREMIUM_BOT_RATE / PREMIUM_FRESH уходит
        на сайт (премиум и лимиты проверяет сайт)."""
        return self._scan(addr, watch, user_id)[:2]

    def _scan(self, addr, watch=False, user_id=None, fresh=False):
        """→ (html, кнопки, адрес токена)."""
        deadline = self.clock() + self.scan_timeout
        try:
            if user_id is not None and any(self.feature(f) for f in SCAN_USER_FEATURES):
                job = self.api.scan(addr, user_id=user_id, **({"fresh": True} if fresh else {}))
            else:
                job = self.api.scan(addr)
            while True:
                r = self.api.result(job)
                if r.get("done"):
                    break
                if self.clock() >= deadline:
                    return T.TIMEOUT, None, addr
                self.sleep(self.poll_every)
        except Rejected as e:
            return T.rejected(addr, str(e)), None, addr
        except TooFast as e:                    # лимит сканов на пользователя / Fresh scan (текст сайта)
            self.log(f"api {addr}: user limit")
            return T.too_fast(str(e)), (T.PREMIUM_UPSELL if e.upsell else None), addr
        except Busy:
            self.log(f"api {addr}: site busy")
            return T.SITE_BUSY, None, addr
        except ApiError as e:
            self.log(f"api {addr}: {e}")
            return T.UNREACHABLE, None, addr
        if r.get("error") or not isinstance(r.get("result"), dict):
            self.log(f"scan {addr}: {r.get('error')}")
            return T.scan_error(addr, r.get("error")), None, addr
        res = r["result"]
        token = res.get("token") or addr
        ticker = (res.get("header") or {}).get("ticker")
        if ticker:
            if len(self.tickers) >= TICKERS_MAX:
                self.tickers.clear()
            self.tickers[token] = ticker
        chain = res.get("chain") or T.chain_of(token)
        watch = watch and res.get("band") not in (T.TOO_ESTABLISHED, T.TOO_ACTIVE) and self.alerts_on()
        return T.verdict(res), T.report_button(token, trade.url(chain, token, self.trade_urls), watch), token

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
        self.call("setMyCommands", commands=COMMANDS + (WATCH_COMMANDS if self.alerts_on() else [])
                  + (PREMIUM_COMMANDS if self.premium_on() else [])
                  + ([PICK_COMMAND] if self.feature("premium_import") else [])
                  + ([DEV_COMMAND] if self.feature("premium_devcheck") else [])
                  + ([TRENDING_COMMAND] if self.feature("premium_trending") else [])
                  + ([DIGEST_COMMAND] if self.feature("premium_digest") else []))
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


def admin_ids_env():
    """PREMIUM_ADMIN_ID: Telegram ID через запятую; неверные — пропускаются."""
    out = set()
    for x in os.environ.get("PREMIUM_ADMIN_ID", "").split(","):
        try:
            out.add(int(x.strip()))
        except ValueError:
            pass
    return out


def main():
    env.load_dotenv()
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not token:
        print("TG_BOT_TOKEN is not set", file=sys.stderr)
        sys.exit(1)
    api = CrawlScan(os.environ.get("CRAWLSCAN_API", "").strip() or "https://crawlscan.fun",
                    alerts_secret=os.environ.get("ALERTS_API_SECRET", "").strip())
    try:
        Bot(Telegram(token), api).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
