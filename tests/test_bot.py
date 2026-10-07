"""Тесты Telegram-бота: подставные Telegram и API сайта, никакой сети."""
import html, os, re, sys, tempfile, threading, time, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import main as bm, text as T   # noqa: E402
from bot.api import ApiError, Rejected  # noqa: E402
from bot.tg import TelegramError        # noqa: E402

RH = "0x19dCb63C4d2F29A6f077F094a4f858fC790145e1"
SOL = "So11111111111111111111111111111111111111112"
PUMP = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"


class FakeTG:
    def __init__(self):
        self.calls, self.uploads, self.n, self.lock = [], [], 100, threading.Lock()

    def redact(self, s):
        return str(s)

    def call(self, method, **params):
        with self.lock:
            self.calls.append((method, params))
            if method in ("sendMessage", "sendPhoto"):
                self.n += 1
                msg = {"message_id": self.n}
                if method == "sendPhoto":
                    msg["photo"] = [{"file_id": "small"}, {"file_id": "BANNER_ID"}]
                return msg
        return True

    def upload(self, method, field, path, **params):
        with self.lock:
            self.uploads.append((method, field, path))
        return self.call(method, **params)

    def of(self, method):
        with self.lock:
            return [p for m, p in self.calls if m == method]

    def texts(self):
        return [p["text"] for p in self.of("sendMessage") + self.of("editMessageText")]


def result(**kw):
    r = {"token": RH.lower(), "chain": "robinhood", "header": {"name": "CrawlScan", "ticker": "CRAWL"},
         "holders_total": 700, "holders": [{}] * 20, "operators": [{"wallets": ["a", "b"]}] + [{"wallets": ["c"]}] * 15,
         "score": 79, "band": "OK", "gates": [],
         "metrics": {"impact": 0.311, "virgin": 0.15, "transfer": 0, "sniper": 0.0004, "operator": 0.04}}
    r.update(kw)
    return r


def rewards_status(**kw):
    """Ответ /api/rewards/status сайта (как rewards_service.status_json)."""
    st = {"enabled": True, "chain": "robinhood", "decimals": 18, "now": "2026-10-07T16:48:00+00:00",
          "next_draw": "2026-10-07T22:00:00+00:00", "next_burn": "2026-10-07T22:00:00+00:00",
          "last_burn": {"tx": "0x47f0" + "a" * 60, "amount_tokens": 500000.0, "time": 1791323882},
          "burned_by_dev": {"amount": 1500001 * 10 ** 18, "amount_tokens": 1500001.0, "count": 3},
          "total_burned": {"amount": 1500001 * 10 ** 18, "amount_tokens": 1500001.0, "minted": 10 ** 27},
          "last_draw": {"day": "2026-10-06", "status": "done", "winner": "0x2dc4382d0959791e45c7beb0e13c610a4a824e58",
                        "chance": 0.003856, "payout_status": "pending", "payout_tokens": None, "payout_tx": None}}
    st.update(kw)
    return st


class FakeAPI:
    base = "https://crawlscan.test"

    def __init__(self, res=None, error=None, reject=None, down=False, pending=0, rewards=None):
        self.res, self.error, self.reject, self.down, self.pending = res or result(), error, reject, down, pending
        self.rewards = rewards if rewards is not None else rewards_status()
        self.scans = []

    def rewards_status(self):
        if self.down:
            raise ApiError("connection refused")
        return self.rewards

    def scan(self, token):
        if self.down:
            raise ApiError("connection refused")
        if self.reject:
            raise Rejected(self.reject)
        self.scans.append(token)
        return "job1"

    def result(self, job):
        if self.pending:
            self.pending -= 1
            return {"done": False}
        if self.error:
            return {"done": True, "error": self.error}
        return {"done": True, "result": self.res}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def private(text, user=1):
    return {"message": {"message_id": 5, "chat": {"id": user, "type": "private"}, "from": {"id": user}, "text": text}}


def group(text, user=1):
    return {"message": {"message_id": 7, "chat": {"id": -100, "type": "supergroup"}, "from": {"id": user},
                        "text": text}}


def make(api=None, tg=None, **kw):
    tg, clock = tg or FakeTG(), Clock()
    bot = bm.Bot(tg, api or FakeAPI(), username="CrawlScanBot", clock=clock, sleep=clock.sleep,
                 log=lambda m: None, **{"banner": None, "spawn": lambda f: f()} | kw)
    return bot, tg, clock


def drain(bot):
    """Выполнить сканы из очереди в текущем потоке."""
    while not bot.jobs.empty():
        bot.run_scan(*bot.jobs.get())


class TestAddress(unittest.TestCase):

    def test_chains(self):
        self.assertEqual(T.find_address(RH), ("robinhood", RH))
        self.assertEqual(T.find_address(f"  {RH.lower()}\n"), ("robinhood", RH.lower()))
        self.assertEqual(T.find_address(SOL), ("solana", SOL))
        self.assertEqual(T.find_address(PUMP), ("solana", PUMP))

    def test_inside_link(self):
        self.assertEqual(T.find_address(f"https://pump.fun/coin/{PUMP}"), ("solana", PUMP))
        self.assertEqual(T.find_address(f"look at https://crawlscan.fun/?ca={RH}&x=1"), ("robinhood", RH))

    def test_not_address(self):
        for s in ["", "hello", "0x1234", RH + "00", "0x" + "g" * 40, "1" * 44, "I0O" + "a" * 40,
                  "this is just a long sentence without any address"]:
            self.assertIsNone(T.find_address(s), s)

    def test_same_as_site(self):
        from chains import solana as sol
        for s in [SOL, PUMP, "1" * 32, "11111111111111111111111111111111", "z" * 44, "2" * 33]:
            self.assertEqual(T.chain_of(s) == "solana", sol.is_address(s), s)


class TestCommands(unittest.TestCase):

    def check_start(self, msg, field):
        self.assertEqual(msg["parse_mode"], "HTML")
        self.assertIn(f"CA: <code>{T.OFFICIAL_CA}</code>", msg[field])
        self.assertTrue(msg[field].startswith("<b>CrawlScan</b> shows how many real people"))
        self.assertIn("<b>CrawlScan has its own token, and it rewards its holders.</b>", msg[field])
        self.assertNotIn("🕷", msg[field])
        buttons = [b for row in msg["reply_markup"]["inline_keyboard"] for b in row]
        by = {b["text"]: b for b in buttons}
        self.assertEqual(by["Scan a token"]["callback_data"], "scan")
        self.assertEqual(by["Help"]["callback_data"], "help")
        self.assertEqual(by["Website"]["url"], "https://crawlscan.fun")
        self.assertEqual(by["Buy $CrawlScan"]["url"],
                         "https://www.ponsfamily.com/launchpad/0x19dCb63C4d2F29A6f077F094a4f858fC790145e1")

    def test_caption_fits(self):
        visible = html.unescape(re.sub(r"<[^>]+>", "", T.START))
        self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, T.CAPTION_MAX)
        self.assertLessEqual(len(T.START), T.CAPTION_MAX)          # и с тегами — с запасом

    def test_banner_shipped(self):
        self.assertTrue(os.path.isfile(bm.BANNER))

    def test_start_photo_then_file_id(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            bot, tg, _ = make(banner=f.name)
            bot.handle_update(private("/start"))
            bot.handle_update(private("/start", user=2))
            self.assertEqual(tg.uploads, [("sendPhoto", "photo", f.name)])   # файл загружен один раз
        first, second = tg.of("sendPhoto")
        self.check_start(first, "caption")
        self.assertNotIn("photo", first)
        self.assertEqual(second["photo"], "BANNER_ID")                         # дальше по file_id
        self.assertEqual(second["chat_id"], 2)
        self.check_start(second, "caption")
        self.assertEqual(tg.of("sendMessage"), [])

    def test_start_without_banner(self):
        bot, tg, _ = make(banner="/nonexistent/banner.png")
        bot.handle_update(private("/start"))
        self.assertEqual(tg.of("sendPhoto"), [])
        (msg,) = tg.of("sendMessage")
        self.check_start(msg, "text")

    def test_start_photo_failed(self):
        class NoPhotoTG(FakeTG):
            def upload(self, method, field, path, **params):
                raise TelegramError(400, "Bad Request: IMAGE_PROCESS_FAILED")

        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            bot, tg, _ = make(tg=NoPhotoTG(), banner=f.name)
            bot.handle_update(private("/start"))
        (msg,) = tg.of("sendMessage")
        self.check_start(msg, "text")
        self.assertIsNone(bot.banner_id)

    def test_callbacks(self):
        bot, tg, _ = make()
        bot.handle_update({"callback_query": {"id": "q1", "data": "scan", "message": {"chat": {"id": 1}}}})
        bot.handle_update({"callback_query": {"id": "q2", "data": "help", "message": {"chat": {"id": 1}}}})
        self.assertEqual([p["callback_query_id"] for p in tg.of("answerCallbackQuery")], ["q1", "q2"])
        self.assertEqual(tg.texts(), ["Send me a token address from Robinhood Chain or Solana", T.HELP])

    def test_help_and_hint(self):
        bot, tg, _ = make()
        bot.handle_update(private("/help"))
        bot.handle_update(private("gm"))
        bot.handle_update(private("/scan"))
        self.assertEqual(tg.texts(), [T.HELP, T.HINT, T.SCAN_USAGE])
        self.assertTrue(bot.jobs.empty())

    def test_private_address_and_scan_command(self):
        bot, tg, _ = make()
        bot.handle_update(private(RH, user=1))
        bot.handle_update(private(f"/scan {SOL}", user=2))
        self.assertEqual([j[2] for j in list(bot.jobs.queue)], [RH, SOL])
        self.assertEqual(tg.texts(), [T.crawling(RH), T.crawling(SOL)])
        self.assertIn("0x19dC…45e1", tg.texts()[0])

    def test_group_only_scan(self):
        bot, tg, _ = make()
        for t in [RH, "/start", "/help", "hello", f"/scan@OtherBot {RH}", "/scan"]:
            bot.handle_update(group(t, user=3 if t == "/scan" else 1))
        self.assertEqual(tg.texts(), [T.SCAN_USAGE])         # только /scan без адреса — подсказка
        self.assertTrue(bot.jobs.empty())
        bot.handle_update(group(f"/scan {RH}", user=1))
        bot.handle_update(group(f"/scan@crawlscanbot {PUMP}", user=2))
        self.assertEqual([j[2] for j in list(bot.jobs.queue)], [RH, PUMP])
        self.assertEqual(tg.of("sendMessage")[-1]["reply_parameters"]["message_id"], 7)


class TestVerdict(unittest.TestCase):

    def scan(self, api):
        bot, tg, _ = make(api)
        bot.handle_update(private(RH))
        drain(bot)
        return tg.of("editMessageText")[-1]

    def test_verdict(self):
        res = result(gates=["biggest operator could move price −62% if sold (≥ 50%; 9.0% of float)",
                            "soft: liquidity too thin ($512)"], band="DANGER", score=31,
                     header={"name": "<b>Evil</b> & co", "ticker": "EV<IL"},
                     metrics={"impact": 0.62, "virgin": 0.35, "transfer": 0.042, "sniper": 0.031})
        msg = self.scan(FakeAPI(res))
        t = msg["text"]
        self.assertEqual(msg["parse_mode"], "HTML")
        self.assertIn("<b>$EV&lt;IL</b> · &lt;b&gt;Evil&lt;/b&gt; &amp; co · Robinhood Chain", t)
        self.assertIn("🔴 <b>Score 31/100 · DANGER</b>", t)
        self.assertIn("20 top holders → 16 operators", t)
        self.assertIn("Biggest operator (2 wallets) could move price −62% if sold", t)
        self.assertIn("• 35% virgin wallets in top", t)
        self.assertIn("• 4.2% of float received by transfer", t)
        self.assertIn("• snipers still hold 3.1% of float", t)
        self.assertIn("<b>Why:</b>\n• biggest operator could move price −62% if sold (≥ 50%; 9.0% of float)", t)
        self.assertIn("• liquidity too thin ($512)", t)
        self.assertNotIn("soft:", t)
        self.assertEqual(msg["reply_markup"]["inline_keyboard"],
                         [[{"text": "Full report", "url": f"https://crawlscan.fun/?ca={RH.lower()}"},
                           {"text": "Trade on Axiom",
                            "url": f"https://axiom.trade/t/{RH.lower()}/@crawlscan?chain=robinhood"}]])

    def test_trade_button_templates(self):
        res = result(chain="solana", token=PUMP)
        msg = self.scan(FakeAPI(res))
        self.assertEqual(msg["reply_markup"]["inline_keyboard"][0][1],
                         {"text": "Trade on Axiom", "url": f"https://axiom.trade/t/{PUMP}/@crawlscan"})
        bot, tg, _ = make(FakeAPI(res), trade_urls={"robinhood": "https://r/{address}", "solana": "https://s/{address}?x=1"})
        bot.handle_update(private(PUMP))
        drain(bot)
        self.assertEqual(tg.of("editMessageText")[-1]["reply_markup"]["inline_keyboard"][0][1]["url"],
                         f"https://s/{PUMP}?x=1")

    def test_probably_rug(self):
        rug = {"drop": 0.867, "level_factor": 0.133, "level_usd": 1e-4, "share": 0.187, "share_supply": 0.15,
               "parts": [{"kind": "linked", "wallets": ["a"] * 6, "share": 0.115, "share_supply": 0.09},
                         {"kind": "transfer", "wallets": ["b"], "share": 0.012, "share_supply": 0.01},
                         {"kind": "virgin", "wallets": ["c", "d"], "share": 0.06, "share_supply": 0.05}],
               "wallets": []}
        t = self.scan(FakeAPI(result(band="DANGER", score=45, rug=rug)))["text"]
        self.assertIn("⚠️ <b>Probably rug: −87% if suspicious holders sell</b>\n"
                      "• linked wallets: 6 wallets, 11.5% of float\n"
                      "• fresh wallets: 2 wallets, 6.0% of float", t)       # две главные причины по доле
        self.assertNotIn("received by transfer", t)
        self.assertNotIn("Probably rug", self.scan(FakeAPI(result(rug=None)))["text"])

    def test_small_impact_and_zero_signals(self):
        t = self.scan(FakeAPI(result(metrics={"impact": 0.004, "virgin": 0, "transfer": 0, "sniper": 0.00001},
                                     operators=[{"wallets": ["a"]}] * 20)))["text"]
        self.assertIn("Biggest operator could move price <1% if sold", t)
        self.assertIn("🟡 <b>Score 79/100 · OK</b>", t)
        for word in ["virgin", "transfer", "snipers", "Why"]:
            self.assertNotIn(word, t)

    def test_too_early(self):
        res = result(band="TOO_EARLY_OR_LATE", score=None, metrics={}, gates=[], holders_total=6,
                     holders=[{}] * 6, chain="solana", token=PUMP)
        msg = self.scan(FakeAPI(res))
        self.assertIn("Too early or too late", msg["text"])
        self.assertIn("Only 6 holders", msg["text"])
        self.assertIn("· Solana", msg["text"])
        self.assertNotIn("Score", msg["text"])
        self.assertIn(PUMP, msg["reply_markup"]["inline_keyboard"][0][0]["url"])

    def test_errors(self):
        cases = [(FakeAPI(reject="not a token address"), "is not a token address"),
                 (FakeAPI(reject="Solana support is coming soon"), "Solana support is coming soon"),
                 (FakeAPI(error="not a Pons V2 token"), "not a Pons V2 token"),
                 (FakeAPI(error="scan failed: rpc <boom>"), T.FAILED),
                 (FakeAPI(down=True), T.UNREACHABLE)]
        for api, want in cases:
            msg = self.scan(api)
            self.assertIn(want, msg["text"])
            self.assertNotIn("boom", msg["text"])
            self.assertIsNone(msg.get("reply_markup"))

    def test_timeout(self):
        api = FakeAPI(pending=10 ** 6)
        bot, tg, clock = make(api)
        bot.handle_update(private(RH))
        t0 = clock.t
        drain(bot)
        self.assertEqual(tg.of("editMessageText")[-1]["text"], T.TIMEOUT)
        self.assertGreaterEqual(clock.t - t0, 60)
        self.assertLess(clock.t - t0, 63)


class TestLimits(unittest.TestCase):

    def test_user_cooldown(self):
        bot, tg, clock = make()
        bot.handle_update(private(RH))
        clock.t += 5
        bot.handle_update(private(SOL))
        self.assertEqual(tg.texts()[-1], "⏳ please wait 15 s")
        bot.handle_update(private(SOL, user=2))             # другой пользователь — без ожидания
        clock.t += 15
        bot.handle_update(private(SOL))
        self.assertEqual(len(bot.jobs.queue), 3)

    def test_bad_address_not_counted(self):
        bot, tg, _ = make()
        bot.handle_update(private("/scan nope"))
        bot.handle_update(private(RH))
        self.assertEqual(len(bot.jobs.queue), 1)

    def test_three_at_once_rest_queued(self):
        gate, running, peak = threading.Event(), [0], [0]
        lock = threading.Lock()

        class SlowAPI(FakeAPI):
            def scan(self, token):
                with lock:
                    running[0] += 1
                    peak[0] = max(peak[0], running[0])
                gate.wait(5)
                with lock:
                    running[0] -= 1
                return "job"

        tg = FakeTG()
        bot = bm.Bot(tg, SlowAPI(), username="CrawlScanBot", log=lambda m: None, poll_every=0.01)
        bot.start_workers()
        addrs = ["0x" + f"{i:040x}" for i in range(5)]
        for i, a in enumerate(addrs):
            bot.handle_update(private(a, user=i))
            time.sleep(0.05)                               # рабочий поток успевает взять скан
        sent = [p["text"] for p in tg.of("sendMessage")]
        self.assertEqual(sent[:3], [T.crawling(a) for a in addrs[:3]])
        self.assertEqual(sent[3:], [T.queued(a) for a in addrs[3:]])
        with lock:
            self.assertEqual(running[0], 3)
        gate.set()
        bot.jobs.join()
        self.assertEqual(peak[0], 3)
        edits = [p["text"] for p in tg.of("editMessageText")]
        self.assertEqual(sum(t.startswith("🕷 crawling") for t in edits), 2)   # из очереди → crawling
        self.assertEqual(sum("Score 79/100" in t for t in edits), 5)


class TestRewards(unittest.TestCase):

    def reply(self, api, update=None):
        bot, tg, _ = make(api)
        bot.handle_update(update or private("/rewards"))
        return tg.of("sendMessage")

    def test_pending_payout(self):
        [m] = self.reply(FakeAPI())
        self.assertEqual(m["parse_mode"], "HTML")
        self.assertEqual(m["reply_markup"], {"inline_keyboard": [[{"text": "Rewards on website",
                                                                    "url": "https://crawlscan.fun"}]]})
        txt = m["text"]
        self.assertIn("🎲 Next draw: <b>22:00 UTC</b> · in 5h 12m", txt)
        self.assertIn("🔥 Next burn: <b>22:00 UTC</b> · in 5h 12m", txt)
        self.assertIn("<code>0x2dc4…4e58</code> · chance 0.39%", txt)
        self.assertIn("⏳ Payout pending", txt)
        self.assertIn("🔥 <b>Burned</b>: 1,500,001 $CrawlScan (0.15% of supply)", txt)
        self.assertIn("Last burn: 500,000 $CrawlScan · 2026-10-06 21:58 UTC · "
                      '<a href="https://robinhoodchain.blockscout.com/tx/0x47f0', txt)

    def test_paid(self):
        d = dict(rewards_status()["last_draw"], payout_status="paid", payout_tokens=12345.6, payout_tx="0xbeef")
        txt = self.reply(FakeAPI(rewards=rewards_status(last_draw=d)))[0]["text"]
        self.assertIn('✅ Paid: 12,346 $CrawlScan · <a href="https://robinhoodchain.blockscout.com/tx/0xbeef">tx</a>', txt)
        self.assertNotIn("pending", txt)

    def test_no_draw_no_winner_no_burns(self):
        txt = self.reply(FakeAPI(rewards=rewards_status(last_draw=None, last_burn=None, total_burned=None,
                                                         burned_by_dev={"amount": 0})))[0]["text"]
        self.assertIn("No draws yet", txt)
        self.assertIn("🔥 <b>Burned</b>: nothing yet", txt)
        self.assertNotIn("Last burn", txt)
        d = {"day": "2026-10-06", "status": "no_eligible", "winner": None}
        txt = self.reply(FakeAPI(rewards=rewards_status(last_draw=d)))[0]["text"]
        self.assertIn("no eligible holders, the prize carries over", txt)

    def test_disabled_and_unreachable(self):
        [m] = self.reply(FakeAPI(rewards={"enabled": False}))
        self.assertEqual(m["text"], T.REWARDS_OFF)
        [m] = self.reply(FakeAPI(down=True))
        self.assertEqual((m["text"], m["reply_markup"]), (T.UNREACHABLE, None))
        [m] = self.reply(FakeAPI(rewards={"enabled": True, "last_draw": "garbage"}))
        self.assertEqual(m["text"], T.UNREACHABLE)              # кривой ответ сайта — не падение

    def test_group_reply_and_other_bot(self):
        [m] = self.reply(FakeAPI(), group("/rewards@CrawlScanBot"))
        self.assertEqual(m["reply_parameters"]["message_id"], 7)
        self.assertEqual(self.reply(FakeAPI(), group("/rewards@OtherBot")), [])

    def test_until(self):
        self.assertEqual([T.until(t, 0) for t in (-5, 0, 30, 60, 3599, 3600, 90061)],
                         ["now", "now", "in <1m", "in 1m", "in 59m", "in 1h 0m", "in 25h 1m"])

    def test_menu_commands(self):
        bot, tg, _ = make()

        def call(method, **p):          # первый getUpdates останавливает run()
            if method == "getUpdates":
                raise KeyboardInterrupt
            return FakeTG.call(tg, method, **p)

        tg.call, bot.start_workers = call, lambda: None
        with self.assertRaises(KeyboardInterrupt):
            bot.run()
        [cmds] = [p["commands"] for m, p in tg.calls if m == "setMyCommands"]
        self.assertEqual([c["command"] for c in cmds], ["start", "scan", "help", "rewards"])


class TestResilience(unittest.TestCase):

    def test_telegram_errors_do_not_raise(self):
        class BadTG(FakeTG):
            def call(self, method, **params):
                raise TelegramError(0, "network down")

        bot = bm.Bot(BadTG(), FakeAPI(), username="CrawlScanBot", log=lambda m: None)
        bot.handle_update(private("/start"))
        bot.handle_update(private(RH))                     # сообщение не ушло — скан не ставится
        self.assertTrue(bot.jobs.empty())

    def test_poll_survives_bad_update(self):
        bot, tg, _ = make()
        updates = [{"update_id": 10, "message": None}, {"update_id": 11, **private("/help")}]
        tg.call = lambda method, **p: updates if method == "getUpdates" else FakeTG.call(tg, method, **p)
        self.assertEqual(bot.poll_once(None), 12)
        self.assertEqual(tg.texts(), [T.HELP])

    def test_parse_command(self):
        self.assertEqual(bm.parse_command("/scan@CrawlScanBot  0xab "), ("scan", "CrawlScanBot", "0xab"))
        self.assertEqual(bm.parse_command("/START"), ("start", None, ""))
        self.assertEqual(bm.parse_command("hi"), (None, None, "hi"))


if __name__ == "__main__":
    unittest.main()
