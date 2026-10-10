"""Логирование времени в боте: апдейт → первый ответ, каждый запрос к сайту, медленные вызовы Telegram (SLOW ≥ 3 с).
Без сети: подставной Telegram и локальный HTTP-сервер вместо сайта."""
import json, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import fakes  # noqa: F401
from bot import main as bm
from bot.api import ApiError, CrawlScan

U = 4242
ADDR = "0x" + "ab" * 20


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TG:
    """Telegram: каждый вызов «длится» delays[метод] секунд (сдвигает часы)."""

    def __init__(self, clock, delays=None):
        self.clock, self.delays, self.calls = clock, delays or {}, []

    def redact(self, s):
        return str(s)

    def call(self, method, **p):
        self.calls.append(method)
        self.clock.t += self.delays.get(method, 0.1)
        if method == "getUpdates":
            return self.updates.pop(0) if getattr(self, "updates", None) else []
        return {"message_id": 1}


class API:
    """Сайт для бота: config и статус премиума; каждый запрос «длится» delay секунд."""
    base, alerts_secret = "https://x", "s"

    def __init__(self, clock, delay=0.2):
        self.clock, self.delay = clock, delay

    def config(self):
        self.clock.t += self.delay
        return {"alerts": True, "premium": True}

    def premium_status(self, user_id):
        self.clock.t += self.delay
        return {"linked": False, "min_tokens": 500_000}

    def scan(self, token, **kw):
        return "job"

    def result(self, job):
        self.clock.t += 1.5
        self.n = getattr(self, "n", 0) + 1
        if self.n < 3:
            return {"done": False}
        return {"done": True, "result": {"token": ADDR, "chain": "robinhood", "header": {"ticker": "T"},
                                         "holders": [{}] * 20, "operators": [], "score": 70, "band": "OK",
                                         "gates": [], "metrics": {"impact": 0.1}}}


def callback(data, uid=7):
    return {"update_id": uid, "callback_query": {"id": "q", "data": data, "from": {"id": U},
                                                 "message": {"message_id": 3, "chat": {"id": U, "type": "private"}}}}


def message(text, uid=8):
    return {"update_id": uid, "message": {"message_id": 1, "chat": {"id": U, "type": "private"}, "from": {"id": U},
                                          "text": text}}


class TestUpdateTiming(unittest.TestCase):

    def make(self, delays=None, api_delay=0.2, spawn=lambda f: f()):
        self.clock, self.logs = Clock(), []
        tg = TG(self.clock, delays)
        bot = bm.Bot(tg, API(self.clock, api_delay), username="b", spawn=spawn, sleep=lambda s: None,
                     log=self.logs.append, clock=self.clock, banner=None)
        return bot, tg

    def test_reply_time_of_a_button(self):
        bot, tg = self.make()
        bot.handle_update(callback("premium"))
        # answerCallbackQuery 0.1 + /api/config 0.2 + /api/premium/status 0.2 + sendMessage 0.1
        self.assertIn("update 7 callback:premium reply 0.60s", self.logs)
        self.assertFalse([l for l in self.logs if l.startswith("SLOW")])

    def test_slow_telegram_blocks_and_is_marked(self):
        bot, tg = self.make(delays={"answerCallbackQuery": 20.0})
        bot.handle_update(callback("premium"))
        self.assertIn("SLOW telegram answerCallbackQuery 20.00s", self.logs)
        self.assertIn("SLOW update 7 callback:premium reply 20.50s", self.logs)

    def test_slow_site_request_is_marked(self):
        bot, tg = self.make(api_delay=4.0)
        bot.handle_update(callback("premium"))
        self.assertIn("SLOW update 7 callback:premium reply 8.20s", self.logs)

    def test_only_first_reply_counts_and_kind_has_no_user_text(self):
        bot, tg = self.make()
        bot.handle_update(message(f"/scan@b {ADDR}"))
        bot.handle_update(message("hello secret words", uid=9))
        bot.handle_update(message(f"look {ADDR}", uid=10))
        kinds = [l.split(" reply")[0] for l in self.logs if l.startswith("update ")]
        self.assertEqual(kinds, ["update 8 /scan", "update 9 text", "update 10 address"])
        self.assertFalse([l for l in self.logs if "secret" in l])
        self.assertEqual(sum(1 for l in self.logs if l.startswith("update 8 ")), 1)        # crawling, вердикт — одна

    def test_waiting_in_batch_counts(self):
        """Апдейты одной пачки getUpdates: время второго — от получения пачки, включая обработку первого."""
        bot, tg = self.make(delays={"answerCallbackQuery": 5.0})
        tg.updates = [[callback("help", uid=1), callback("help", uid=2)]]
        bot.poll_once(None)
        # /help: answerCallbackQuery 5.0 + /api/config 0.2 (флаги Flap/Bankr, потом из кэша) + sendMessage 0.1
        self.assertIn("SLOW update 1 callback:help reply 5.30s", self.logs)
        self.assertIn("SLOW update 2 callback:help reply 10.40s", self.logs)

    def test_threads_keep_the_update(self):
        done = threading.Event()
        bot, tg = self.make(spawn=lambda f: threading.Thread(target=lambda: (f(), done.set())).start())
        bot.handle_update(callback("premium"))
        self.assertTrue(done.wait(5))
        self.assertIn("update 7 callback:premium reply 0.60s", self.logs)

    def test_scan_summary(self):
        bot, tg = self.make()
        bot.run_scan(U, 5, ADDR, False, True, U)
        self.assertIn(f"scan {ADDR} done in 4.5s, 3 polls", self.logs)


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 404 if self.path.startswith("/missing") else 200
        b = json.dumps({"ok": True}).encode()
        self.send_response(code)
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


class TestApiTiming(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_each_request_is_timed(self):
        ticks = iter([0.0, 0.25, 10.0, 13.5, 20.0, 20.1, 30.0, 30.05, 40.0, 44.0])
        api = CrawlScan(self.base, clock=lambda: next(ticks))
        seen = []
        api.on_request = lambda *a: seen.append(a)
        api.config()
        api.result("j1")
        api.premium_status(12345)
        api.result("j2")
        with self.assertRaises(ApiError):
            api._req("/missing?x=1")
        self.assertEqual([(m, p, c, round(d, 2)) for m, p, c, d in seen],
                         [("GET", "/api/config", 200, 0.25), ("GET", "/api/result", 200, 3.5),
                          ("GET", "/api/premium/status", 200, 0.1), ("GET", "/api/result", 200, 0.05),
                          ("GET", "/missing", 404, 4.0)])
        self.assertNotIn("12345", str(seen))                                                 # query (user_id) не пишется

    def test_network_error_is_timed(self):
        api = CrawlScan("http://127.0.0.1:1")
        seen = []
        api.on_request = lambda *a: seen.append(a)
        with self.assertRaises(ApiError):
            api.config()
        self.assertEqual(seen[0][:3], ("GET", "/api/config", "error"))

    def test_bot_lines(self):
        logs = []
        api = CrawlScan(self.base)
        bot = bm.Bot(TG(Clock()), api, username="b", log=logs.append)
        self.assertEqual(api.on_request, bot.api_timing)                                     # как в production
        bot.api_timing("GET", "/api/config", 200, 0.24)
        bot.api_timing("GET", "/api/result", 200, 0.3)                                      # опрос скана — без строки
        bot.api_timing("GET", "/api/result", 200, 3.2)
        bot.api_timing("POST", "/api/premium/status", "error", 15.0)
        self.assertEqual(logs, ["api GET /api/config 200 0.24s", "SLOW api GET /api/result 200 3.20s",
                                "SLOW api POST /api/premium/status error 15.00s"])


if __name__ == "__main__":
    unittest.main()
