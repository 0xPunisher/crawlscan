"""CrawlScan Telegram bot: a separate service, a thin client of the site API (the bot never talks to the blockchain itself).

  python bot/main.py

env: TG_BOT_TOKEN (required, from .env via env.py; never printed), CRAWLSCAN_API (default
https://crawlscan.fun), TRADE_URL_ROBINHOOD / TRADE_URL_SOLANA: Trade on Axiom button templates (trade.py),
ALERTS_API_SECRET: the site subscriptions API secret (absent → no alerts in the bot; never printed). Long polling getUpdates (timeout=30): only one
bot instance can run at a time, a second one gets 409 Conflict.

DMs: token address (or /scan <address>) → scan; /start, /help, /rewards. Groups: only /scan <address>
and /rewards (and /scan@botname, /rewards@botname). Alerts (DMs only, if the site's /api/config → alerts and
ALERTS_API_SECRET is set): /watch <address>, /watchlist, /unwatch <address>, [Watch] button under the verdict, [Unwatch] under
a notification (notifications are sent by the site); otherwise "coming soon". /rewards: rewards and burns status from the site (/api/rewards/status). Scan: immediate "crawling…" reply, then that same message is edited into the verdict.
Limits: 1 scan per user per USER_COOLDOWN seconds, at most WORKERS scans at once (the rest wait in the queue).
"""
import math, os, queue, sys, threading, time, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import env                                          # noqa: E402
import trade                                        # noqa: E402
from bot import text as T                           # noqa: E402
from bot.api import AlertsOff, ApiError, Busy, CrawlScan, Rejected   # noqa: E402
from bot.tg import Telegram, TelegramError          # noqa: E402

WORKERS = 3            # concurrent scans for the whole bot
MAX_QUEUE = 30         # waiting in the queue; more → "too many scans"
USER_COOLDOWN = 20     # seconds between scans by the same user
SCAN_TIMEOUT = 60      # seconds per scan (from start, excluding queue wait)
BANNER = os.path.join(ROOT, "bot", "assets", "banner.png")   # /start image
POLL_EVERY = 1.5       # seconds between /api/result requests
LONG_POLL = 30         # getUpdates timeout
COMMANDS = [{"command": "start", "description": "What this bot does"},
            {"command": "scan", "description": "Scan a token: /scan <address>"},
            {"command": "help", "description": "How to read a verdict"},
            {"command": "rewards", "description": "Next holder draw, last winner, burns"}]
WATCH_COMMANDS = [{"command": "watch", "description": "Get alerts for a token: /watch <address>"},
                  {"command": "watchlist", "description": "Tokens you watch"},
                  {"command": "unwatch", "description": "Stop alerts: /unwatch <address>"}]
ALERTS_CHECK_TTL = 60  # seconds: how long to remember whether alerts are enabled on the site
AWAIT_WATCH = 300      # seconds: after [➕ New] the next address from the chat is a subscription, not a scan
TICKERS_MAX = 2000     # tickers from scans kept in memory (for the subscription list)

_log_lock = threading.Lock()


def log(msg):
    with _log_lock:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def parse_command(text):
    """"/scan@bot 0x..." → ("scan", "bot", "0x..."); not a command → (None, None, text)."""
    if not text.startswith("/"):
        return None, None, text
    head, _, arg = text.partition(" ")
    cmd, _, target = head[1:].partition("@")
    return cmd.lower(), target or None, arg.strip()


class Bot:
    def __init__(self, tg, api, username="", workers=WORKERS, cooldown=USER_COOLDOWN, scan_timeout=SCAN_TIMEOUT,
                 poll_every=POLL_EVERY, clock=time.monotonic, sleep=time.sleep, log=log, banner=BANNER, spawn=None, trade_urls=None):
        self.tg, self.api, self.username = tg, api, username
        self.workers, self.cooldown, self.scan_timeout, self.poll_every = workers, cooldown, scan_timeout, poll_every
        self.clock, self.sleep, self.log = clock, sleep, log
        self.jobs = queue.Queue()
        self.busy = 0                # scans in progress
        self.last_scan = {}          # user_id -> clock() of the last accepted scan
        self.lock = threading.Lock()
        self.banner = banner
        self.banner_id = None        # banner file_id after the first upload: later sends skip the file
        # site requests outside the polling loop (/rewards): by default a separate thread
        self.trade_urls = trade_urls or trade.templates()   # {chain: template} for [Trade on Axiom]
        self.spawn = spawn or (lambda f: threading.Thread(target=f, daemon=True).start())
        self.alerts_seen = None      # (clock(), whether alerts are enabled on the site)
        self.flap_seen = None        # (clock(), (Flap, Bankr) enabled on the site: launchpads line in /help)
        self.awaiting = {}           # chat_id -> clock() until which we wait for an address to subscribe ([➕ New])
        self.tickers = {}            # token address -> ticker from the latest scan (the site does not store subscription tickers)

    # --- Telegram ---------------------------------------------------------------------------------

    def call(self, method, upload=None, **params):
        """Bot API call; errors are logged, the bot keeps running. → result or None.
        upload=(field, path): with a file (multipart)."""
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
        """Banner with the greeting in the caption; by file_id if the banner was already uploaded.
        No file or sendPhoto failed → the same greeting as a plain message.
        Alerts enabled → [🔔 Watchlist] button."""
        buttons = T.start_buttons(self.alerts_on())
        params = dict(chat_id=chat_id, caption=T.START, parse_mode="HTML", reply_markup=buttons)
        msg = None
        if self.banner_id:
            msg = self.call("sendPhoto", photo=self.banner_id, **params)
            if msg is None:
                self.banner_id = None    # file_id rejected: upload the file again next time
        elif self.banner and os.path.isfile(self.banner):
            msg = self.call("sendPhoto", upload=("photo", self.banner), **params)
            sizes = msg.get("photo") if isinstance(msg, dict) else None
            if sizes:
                self.banner_id = sizes[-1]["file_id"]   # largest size
        if msg is None:
            return self.send(chat_id, T.START, buttons)
        return msg.get("message_id") if isinstance(msg, dict) else None

    def edit(self, chat_id, message_id, html, markup=None):
        self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=html, parse_mode="HTML",
                  reply_markup=markup, link_preview_options={"is_disabled": True})

    # --- updates -----------------------------------------------------------------------------------

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
            return                               # command for another bot
        chat_id, mid = chat.get("id"), m.get("message_id")
        if chat.get("type") == "private":
            if cmd is None and self.awaiting_watch(chat_id):       # after [➕ New]: the address is a subscription, not a scan
                found = T.find_address(txt)
                if not found:
                    return self.send(chat_id, T.ASK_WATCH_AGAIN)
                self.awaiting.pop(chat_id, None)
                return self.alerts_command(chat_id, "new", found[1])
            if cmd == "start":   # alerts check (Watchlist button) may hit the site: not in the polling loop
                return self.spawn(lambda: self.send_start(chat_id))
            if cmd == "help":   # Flap flag may hit the site: not in the polling loop
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
            # [Watch] under the verdict, [Unwatch] under a site notification (alerts_notify), [Remove] in the subscription list
            found = T.find_address(addr)
            if found:
                mid = (cq.get("message") or {}).get("message_id") if cmd == "rm" else None
                self.alerts_command(chat_id, "remove" if cmd == "rm" else cmd, found[1], edit=mid)
        elif data == "watchlist" and private:
            self.alerts_command(chat_id, "watchlist", "")
        elif data == "new" and private:
            self.awaiting[chat_id] = self.clock() + AWAIT_WATCH    # right away: the address may arrive before the site responds
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
        """Site /api/rewards/status → (html, buttons). Site unreachable → UNREACHABLE without buttons."""
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

    # --- alerts: subscriptions (the site stores them, the bot only asks) ---------------------------------

    def alerts_on(self):
        """Alerts are enabled: the bot has the secret and the site answers /api/config → alerts. Remembered for ALERTS_CHECK_TTL seconds;
        site unreachable → disabled (also remembered)."""
        if not getattr(self.api, "alerts_secret", ""):
            return False
        now = self.clock()
        seen = self.alerts_seen
        if seen and now - seen[0] < ALERTS_CHECK_TTL:
            return seen[1]
        try:
            on = bool(self.api.config().get("alerts"))
        except (ApiError, Rejected, AttributeError) as e:
            self.log(f"api config: {e}")
            on = False
        self.alerts_seen = (now, on)
        return on

    def launchpads(self):
        """(Flap, Bankr) enabled on the site (/api/config → flap, bankr). Remembered for ALERTS_CHECK_TTL seconds;
        site unreachable → no."""
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
        """Whether we are waiting for an address to subscribe from the chat ([➕ New], AWAIT_WATCH seconds); expired → reset."""
        until = self.awaiting.get(chat_id)
        if until is None:
            return False
        if self.clock() >= until:
            self.awaiting.pop(chat_id, None)
            return False
        return True

    def alerts_command(self, chat_id, cmd, arg, edit=None):
        """Subscriptions: site request in a separate thread. edit is a message_id: update that message ([Remove]).
        cmd: watch / unwatch / watchlist (commands and buttons), new (address after [➕ New]), ask ([➕ New]),
        remove ([Remove] in the list)."""
        def run():
            html, markup = self.alerts_text(chat_id, cmd, arg)
            if edit:
                self.edit(chat_id, edit, html, markup)
            else:
                self.send(chat_id, html, markup)
        self.spawn(run)

    def alerts_text(self, chat_id, cmd, arg, now=None):
        """→ (html, buttons)."""
        if not self.alerts_on():
            self.awaiting.pop(chat_id, None)
            return T.ALERTS_SOON, None
        now = time.time() if now is None else now
        found = T.find_address(arg) if arg else None
        if cmd in ("watch", "unwatch", "new", "remove") and not found:
            return (T.UNWATCH_USAGE if cmd == "unwatch" else T.WATCH_USAGE), None
        try:
            if cmd == "watchlist":
                return T.watchlist_view(self.api.watch_list(chat_id), now, self.tickers)
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
                return T.watchlist_view(self.api.watch_list(chat_id), now, self.tickers)
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

    # --- scans -------------------------------------------------------------------------------------

    def request_scan(self, chat_id, user_id, addr, reply_to, private=False):
        """Limits and queue: immediate crawling/queued reply, the scan runs in a worker thread."""
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
        self.jobs.put((chat_id, mid, addr, waiting, private))

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

    def run_scan(self, chat_id, mid, addr, was_queued, private=False):
        if was_queued:
            self.edit(chat_id, mid, T.crawling(addr))
        html, markup = self.scan_text(addr, watch=private)
        self.edit(chat_id, mid, html, markup)

    def scan_text(self, addr, watch=False):
        """Scan via the site API → (html, buttons). watch = DM: [Watch] button if alerts are enabled
        and the token is not too established."""
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
        except Busy:
            self.log(f"api {addr}: site busy")
            return T.SITE_BUSY, None
        except ApiError as e:
            self.log(f"api {addr}: {e}")
            return T.UNREACHABLE, None
        if r.get("error") or not isinstance(r.get("result"), dict):
            self.log(f"scan {addr}: {r.get('error')}")
            return T.scan_error(addr, r.get("error")), None
        res = r["result"]
        token = res.get("token") or addr
        ticker = (res.get("header") or {}).get("ticker")
        if ticker:
            if len(self.tickers) >= TICKERS_MAX:
                self.tickers.clear()
            self.tickers[token] = ticker
        chain = res.get("chain") or T.chain_of(token)
        watch = watch and res.get("band") not in (T.TOO_ESTABLISHED, T.TOO_ACTIVE) and self.alerts_on()
        return T.verdict(res), T.report_button(token, trade.url(chain, token, self.trade_urls), watch)

    # --- polling loop ------------------------------------------------------------------------------

    def start_workers(self):
        for _ in range(self.workers):
            threading.Thread(target=self.worker, daemon=True).start()

    def poll_once(self, offset):
        """One getUpdates → new offset. A failure handling one update does not affect the rest."""
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
        self.call("setMyCommands", commands=COMMANDS + (WATCH_COMMANDS if self.alerts_on() else []))
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
    api = CrawlScan(os.environ.get("CRAWLSCAN_API", "").strip() or "https://crawlscan.fun",
                    alerts_secret=os.environ.get("ALERTS_API_SECRET", "").strip())
    try:
        Bot(Telegram(token), api).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
