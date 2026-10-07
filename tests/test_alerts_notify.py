"""Тесты alerts A3: уведомления подписчикам после живого скана (alerts_notify, server.record_snapshot). Без сети."""
import contextlib, io, json, os, tempfile, threading, time, unittest, urllib.error, urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer

import fakes
import alerts
import alerts_notify as an
import server
from alerts_store import AlertsStore
from bot.tg import TelegramError

T = "0x" + "11" * 20
T2 = "0x" + "22" * 20
A, B = "0x" + "a" * 40, "0x" + "b" * 40
NOW = 1_800_000_000.0


def snap(band="OK", score=70, rug=None, token=T, ticker="AAA", ts=100):
    res = {"token": token, "chain": "robinhood", "band": band, "score": score, "rug": rug,
           "header": {"ticker": ticker}, "operators": [{"wallets": [A], "share_supply": 0.05}],
           "holders": [{"wallet": A, "share_supply": 0.05}, {"wallet": B, "share_supply": 0.02}]}
    return alerts.snapshot(res, ts=ts)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Sender:
    def __init__(self, clock=None, fail=None):
        self.sent, self.clock, self.fail = [], clock, fail or {}

    def __call__(self, chat_id, text, markup):
        err = self.fail.get(chat_id)
        if err:
            e = err.pop(0) if isinstance(err, list) else err
            if isinstance(err, list) and not err:
                del self.fail[chat_id]
            raise e
        self.sent.append({"chat": chat_id, "text": text, "markup": markup, "t": self.clock() if self.clock else None})


class TestNotifier(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = AlertsStore(os.path.join(self.tmp.name, "n.db"))
        self.clock = Clock()
        self.send = Sender(self.clock)
        self.logs = []
        self.n = an.Notifier(lambda: self.st, self.send, clock=self.clock, sleep=self.clock.sleep, log=self.logs.append)

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def watch(self, chat, token=T):
        self.st.watch(chat, token, "robinhood", now=self.clock())

    def event(self, old, new):
        changes = alerts.diff(old, new)
        self.n.process(old, new, changes)
        return changes

    def test_no_subscribers_nothing_sent(self):
        self.watch(1, T2)
        self.event(snap("OK", 70), snap("DANGER", 20))
        self.assertEqual(self.send.sent, [])

    def test_message_format(self):
        self.watch(1)
        self.event(snap("OK", 70), snap("DANGER", 20, rug={"drop": 0.72}))
        (m,) = self.send.sent
        self.assertEqual(m["chat"], 1)
        self.assertEqual(m["text"], "🔔 <b>$AAA</b> · Robinhood Chain\n\n"
                                    "• Verdict worsened: OK → DANGER (score 70 → 20)\n"
                                    "• Probably rug: −72% if suspicious holders sell\n\n"
                                    "Now: 🔴 <b>DANGER</b> · score 20/100\n"
                                    "⚠️ probably rug −72%")
        rows = m["markup"]["inline_keyboard"]
        self.assertEqual([b["text"] for b in rows[0]], ["Full report", "Trade on Axiom"])
        self.assertEqual(rows[0][0]["url"], f"https://crawlscan.fun/?ca={T}")
        self.assertIn(T, rows[0][1]["url"])
        self.assertEqual(rows[1], [{"text": "🔕 Unwatch", "callback_data": f"unwatch:{T}"}])

    def test_message_escapes_and_no_ticker(self):
        text, _ = alerts.message(snap(ticker="<b>x&y"), [{"kind": "k", "text": "a < b"}])
        self.assertIn("$&lt;b&gt;x&amp;y", text)
        self.assertIn("• a &lt; b", text)
        text, _ = alerts.message(snap(ticker=None), [])
        self.assertTrue(text.startswith("🔔 <b>0x1111…1111</b> · Robinhood Chain"))

    def test_all_subscribers_get_it(self):
        for chat in (1, 2, 3):
            self.watch(chat)
        self.event(snap("OK", 70), snap("DANGER", 20))
        self.assertEqual([m["chat"] for m in self.send.sent], [1, 2, 3])

    def test_cooldown_and_summary(self):
        self.watch(1)
        s1, s2, s3, s4 = snap("CLEAN", 90), snap("OK", 70), snap("RISKY", 50), snap("DANGER", 20, rug={"drop": 0.6})
        self.event(s1, s2)                                     # сразу
        self.assertEqual(len(self.send.sent), 1)
        self.clock.t += 60
        self.event(s2, s3)                                     # внутри 15 минут — копится
        self.clock.t += 60
        self.event(s3, s4)
        self.n.flush()
        self.assertEqual(len(self.send.sent), 1)
        self.clock.t = NOW + an.COOLDOWN - 1
        self.n.flush()
        self.assertEqual(len(self.send.sent), 1)
        self.clock.t = NOW + an.COOLDOWN
        self.n.flush()
        self.assertEqual(len(self.send.sent), 2)               # одно сводное: от OK (что видел человек) до DANGER
        text = self.send.sent[1]["text"]
        self.assertIn("• Verdict worsened: OK → DANGER (score 70 → 20)", text)
        self.assertIn("Probably rug: −60%", text)
        self.assertNotIn("RISKY", text)
        self.clock.t += 60                                     # новое окно: снова ждём 15 минут от сводки
        self.event(s4, s3)
        self.assertEqual(len(self.send.sent), 2)
        self.clock.t += an.COOLDOWN
        self.n.flush()
        self.assertEqual(len(self.send.sent), 3)

    def test_cooldown_per_chat_and_token(self):
        self.watch(1); self.watch(2, T2); self.watch(1, T2)
        self.event(snap("OK", 70), snap("DANGER", 20))
        self.event(snap("OK", 70, token=T2), snap("DANGER", 20, token=T2))   # другой токен — не ждёт
        self.assertEqual(sorted((m["chat"], T2 in m["markup"]["inline_keyboard"][1][0]["callback_data"])
                                for m in self.send.sent), [(1, False), (1, True), (2, True)])

    def test_summary_back_to_start_sends_nothing(self):
        self.watch(1)
        self.event(snap("OK", 70), snap("DANGER", 20))         # человек видел DANGER
        self.clock.t += 60
        self.event(snap("DANGER", 20), snap("OK", 70))
        self.clock.t += 60
        self.event(snap("OK", 70), snap("DANGER", 20))         # за окно вернулось к тому, что он видел
        self.clock.t += an.COOLDOWN
        self.n.flush()
        self.assertEqual(len(self.send.sent), 1)

    def test_unwatched_during_cooldown(self):
        self.watch(1)
        self.event(snap("OK", 70), snap("DANGER", 20))
        self.event(snap("DANGER", 20), snap("OK", 90))
        self.st.unwatch(1, T)
        self.clock.t += an.COOLDOWN
        self.n.flush()
        self.assertEqual(len(self.send.sent), 1)

    def test_403_removes_subscriptions(self):
        self.watch(1); self.watch(1, T2); self.watch(2)
        self.send.fail[1] = TelegramError(403, "Forbidden: bot was blocked by the user")
        self.event(snap("OK", 70), snap("DANGER", 20))
        self.assertEqual([m["chat"] for m in self.send.sent], [2])
        self.assertEqual(self.st.watches(1, now=self.clock()), [])
        self.assertEqual(len(self.st.watches(2, now=self.clock())), 1)
        self.assertTrue(any("blocked the bot, removed 2 watches" in l for l in self.logs))

    def test_429_retries_after(self):
        self.watch(1)
        self.send.fail[1] = [TelegramError(429, "Too Many Requests", retry_after=3)]
        t0 = self.clock()
        self.event(snap("OK", 70), snap("DANGER", 20))
        self.assertEqual(len(self.send.sent), 1)
        self.assertGreaterEqual(self.send.sent[0]["t"] - t0, 3)

    def test_other_error_logged_not_raised(self):
        self.watch(1); self.watch(2)
        self.send.fail[1] = TelegramError(400, "Bad Request: chat not found")
        self.event(snap("OK", 70), snap("DANGER", 20))
        self.assertEqual([m["chat"] for m in self.send.sent], [2])
        self.assertEqual(len(self.st.watches(1, now=self.clock())), 1)   # не 403 — подписка остаётся

    def test_expired_not_notified_and_deleted(self):
        self.st.watch(1, T, "robinhood", now=self.clock() - 7 * 86400)    # истекла ровно сейчас
        self.watch(2)
        self.event(snap("OK", 70), snap("DANGER", 20))
        self.assertEqual([m["chat"] for m in self.send.sent], [2])
        expired = self.st.expire(now=self.clock())              # удаляет планировщик перепроверок (A4)
        self.assertEqual([r["chat_id"] for r in expired], [1])
        rows = self.st.db.execute("SELECT chat_id FROM alert_watches").fetchall()
        self.assertEqual([r[0] for r in rows], [2])

    def test_rate_limit(self):
        for chat in range(1, 46):
            self.watch(chat)
        self.event(snap("OK", 70), snap("DANGER", 20))
        ts = [m["t"] for m in self.send.sent]
        self.assertEqual(len(ts), 45)
        self.assertTrue(all(b - a >= 1 / an.RATE - 1e-6 for a, b in zip(ts, ts[1:])))
        for i in range(len(ts) - an.RATE):                     # в любом окне 1 с — не больше RATE
            self.assertGreaterEqual(ts[i + an.RATE] - ts[i], 1.0 - 1e-6)

    def test_push_never_blocks(self):
        n = an.Notifier(lambda: self.st, self.send, log=self.logs.append)
        for _ in range(an.QUEUE_MAX + 5):
            n.push(None, snap(), [])
        self.assertTrue(any("queue full" in l for l in self.logs))

    def test_thread_sends(self):
        self.watch(1)
        send = Sender()
        n = an.Notifier(lambda: self.st, send, log=self.logs.append).start()
        old, new = snap("OK", 70), snap("DANGER", 20)
        n.push(old, new, alerts.diff(old, new))
        t0 = time.time()
        while not send.sent and time.time() - t0 < 5:
            time.sleep(0.01)
        self.assertEqual(len(send.sent), 1)

    def test_telegram_sender_redacts_token(self):
        send = an.telegram("123:SECRET")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom https://api.telegram.org/bot123:SECRET/x")):
            with self.assertRaises(TelegramError) as cm:
                send(1, "hi", None)
        self.assertNotIn("SECRET", str(cm.exception))


class TestLiveScans(unittest.TestCase):
    """server: живой скан → снимок → diff → очередь → фоновый поток → Telegram (подставной)."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        with server._lock:
            server.JOBS.clear(); server.BY_TOKEN.clear()
        server._notifier.update(obj=None, warned=False)
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DRAW_DB_PATH": os.path.join(self.tmp.name, "l.db"),
                                                "ALERTS_ENABLED": "true", "TG_BOT_TOKEN": "123:TESTTOKEN"})
        self.env.__enter__()
        self.fakes = fakes.patched()
        self.fakes.__enter__()
        self.send = Sender()
        self.tg = mock.patch.object(an, "telegram", return_value=self.send)
        self.tg.__enter__()

    def tearDown(self):
        self.tg.__exit__(None, None, None)
        self.fakes.__exit__(None, None, None)
        server._notifier.update(obj=None, warned=False)
        with server._stores_lock:
            for st in list(server._recent_stores.values()) + list(server._alerts_stores.values()):
                st.close()
            server._recent_stores.clear()
            server._alerts_stores.clear()
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def scan(self):
        with server._lock:                                     # мимо 10-минутного кэша
            server.JOBS.clear(); server.BY_TOKEN.clear()
        req = urllib.request.Request(self.base + "/api/scan", data=json.dumps({"token": fakes.TOKEN}).encode(),
                                     method="POST", headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            job = json.loads(r.read())["job"]
        t0 = time.time()
        while time.time() - t0 < 10:
            with urllib.request.urlopen(f"{self.base}/api/result?job={job}", timeout=10) as r:
                d = json.loads(r.read())
            if d["done"]:
                took = time.time() - t0
                time.sleep(0.15)                               # снимок пишется сразу после done
                return d, took
            time.sleep(0.02)
        self.fail("скан не завершился")

    def wait_sent(self, n, timeout=5):
        t0 = time.time()
        while len(self.send.sent) < n and time.time() - t0 < timeout:
            time.sleep(0.02)
        time.sleep(0.1)
        return self.send.sent

    def make_prev_differ(self):
        """Последний снимок токена → тот же, но с «крупнейшим оператором» в 10 раз больше: следующий скан
        даст operator_sold."""
        st = server.alerts_store()
        _, cur = st.get(fakes.TOKEN)
        w, share = next(iter(cur["top"].items()))
        st.record(cur | {"operator": {"wallets": [w], "share_supply": share * 10}})

    def test_notification_after_scan(self):
        self.scan()                                            # первый снимок: diff не с чем — тишина
        server.alerts_store().watch(77, fakes.TOKEN, "robinhood")
        self.make_prev_differ()
        self.scan()
        (m,) = self.wait_sent(1)
        self.assertEqual(m["chat"], 77)
        self.assertIn("Biggest operator sold 90% of their holdings", m["text"])
        self.assertIn(f"unwatch:{fakes.TOKEN}", json.dumps(m["markup"]))

    def test_no_diff_no_notification(self):
        server.alerts_store().watch(77, fakes.TOKEN, "robinhood")
        self.scan(); self.scan()                               # те же данные — diff пустой
        self.assertEqual(self.wait_sent(1, timeout=0.5), [])
        self.assertIsNone(server._notifier["obj"])             # даже отправщик не понадобился

    def test_no_subscribers_no_notification(self):
        self.scan()
        self.make_prev_differ()
        self.scan()
        self.assertEqual(self.wait_sent(1, timeout=0.5), [])

    def test_no_bot_token(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"TG_BOT_TOKEN": ""}), contextlib.redirect_stdout(out):
            self.scan()
            server.alerts_store().watch(77, fakes.TOKEN, "robinhood")
            for _ in range(2):
                self.make_prev_differ()
                d, _ = self.scan()
                self.assertIn("result", d)
            time.sleep(0.2)
        self.assertEqual(self.send.sent, [])
        self.assertEqual(out.getvalue().count("TG_BOT_TOKEN is not set"), 1)   # одно предупреждение

    def test_scan_does_not_wait_for_send(self):
        gate, started = threading.Event(), threading.Event()

        def slow(chat_id, text, markup):
            started.set()
            gate.wait(10)
            self.send.sent.append({"chat": chat_id})

        self.tg.__exit__(None, None, None)
        self.tg = mock.patch.object(an, "telegram", return_value=slow)
        self.tg.__enter__()
        self.scan()
        server.alerts_store().watch(77, fakes.TOKEN, "robinhood")
        self.make_prev_differ()
        d, _ = self.scan()
        self.assertTrue(started.wait(5))                       # отправка началась и висит
        self.make_prev_differ()
        d, took = self.scan()                                  # следующий скан не ждёт зависшую отправку
        self.assertIn("result", d)
        self.assertLess(took, 5)
        self.assertEqual(self.send.sent, [])
        gate.set()
        self.assertEqual(len(self.wait_sent(1)), 1)

    def test_send_failure_does_not_break_scan(self):
        def boom(chat_id, text, markup):
            raise RuntimeError("telegram exploded")

        self.tg.__exit__(None, None, None)
        self.tg = mock.patch.object(an, "telegram", return_value=boom)
        self.tg.__enter__()
        self.scan()
        server.alerts_store().watch(77, fakes.TOKEN, "robinhood")
        for _ in range(2):
            self.make_prev_differ()
            d, _ = self.scan()
            self.assertIn("result", d)
            self.assertNotIn("error", d)


    # --- POST /api/alerts/test ------------------------------------------------------------------------

    def post_test(self, body, secret="sec"):
        req = urllib.request.Request(self.base + "/api/alerts/test", data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json", "X-Alerts-Secret": secret})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_test_alert_secret_and_switch(self):
        with mock.patch.dict(os.environ, {"ALERTS_API_SECRET": "sec"}):
            self.assertEqual(self.post_test({"chat_id": 5, "token": fakes.TOKEN}, secret="nope")[0], 403)
            with mock.patch.dict(os.environ, {"ALERTS_ENABLED": "false"}):
                self.assertEqual(self.post_test({"chat_id": 5, "token": fakes.TOKEN})[0], 404)
        self.assertEqual(self.post_test({"chat_id": 5, "token": fakes.TOKEN}, secret="")[0], 403)   # секрет не задан
        self.assertEqual(self.send.sent, [])

    def test_test_alert_from_snapshot_no_cooldown(self):
        self.scan()
        with mock.patch.dict(os.environ, {"ALERTS_API_SECRET": "sec"}):
            for _ in range(2):                                # cooldown не применяется
                code, d = self.post_test({"chat_id": 5, "token": fakes.TOKEN})
                self.assertEqual((code, d["ok"], d["source"]), (200, True, "snapshot"))
        self.assertEqual([m["chat"] for m in self.send.sent], [5, 5])   # подписка не нужна
        text = self.send.sent[0]["text"]
        first = text.split("\n")[0]
        self.assertTrue(first.startswith("🔔 <b>") and first.endswith(" (test alert)"), first)
        self.assertIn("• This is a test.", text)
        self.assertIn("Now: ", text)
        self.assertEqual(self.send.sent[0]["markup"]["inline_keyboard"][1][0]["callback_data"], f"unwatch:{fakes.TOKEN}")

    def test_test_alert_shows_real_changes(self):
        self.scan()
        self.make_prev_differ()
        self.scan()
        with mock.patch.dict(os.environ, {"ALERTS_API_SECRET": "sec"}):
            code, d = self.post_test({"chat_id": 5, "token": fakes.TOKEN})
        self.assertEqual(d["changes"], ["operator_sold"])
        self.assertIn("Biggest operator sold", self.send.sent[-1]["text"])
        self.assertNotIn("This is a test", self.send.sent[-1]["text"])

    def test_test_alert_scans_if_no_snapshot(self):
        with mock.patch.dict(os.environ, {"ALERTS_API_SECRET": "sec"}):
            code, d = self.post_test({"chat_id": 5, "token": fakes.TOKEN})
            self.assertEqual((code, d["source"]), (200, "scan"))
            self.assertEqual(self.post_test({"chat_id": 5, "token": fakes.OTHER})[0], 409)   # скан с ошибкой
            self.assertEqual(self.post_test({"chat_id": 5, "token": "nope"})[0], 400)
            self.assertEqual(self.post_test({"chat_id": "x", "token": fakes.TOKEN})[0], 400)
        self.assertIn("(test alert)", self.send.sent[0]["text"])

    def test_test_alert_telegram_errors(self):
        self.scan()
        server.alerts_store().watch(5, fakes.TOKEN, "robinhood")
        self.send.fail[5] = TelegramError(403, "Forbidden: bot was blocked by the user")
        with mock.patch.dict(os.environ, {"ALERTS_API_SECRET": "sec"}):
            code, d = self.post_test({"chat_id": 5, "token": fakes.TOKEN})
            self.assertEqual((code, d["code"]), (502, 403))
            self.assertEqual(len(server.alerts_store().watches(5)), 1)   # тест подписки не трогает
            with mock.patch.dict(os.environ, {"TG_BOT_TOKEN": ""}):
                self.assertEqual(self.post_test({"chat_id": 5, "token": fakes.TOKEN})[0], 503)


if __name__ == "__main__":
    unittest.main()
