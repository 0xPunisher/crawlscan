"""Тесты alerts A4: плановые перепроверки (alerts_recheck), приоритет живых сканов (chains.priority),
авто-отписка при TOO_ESTABLISHED, истечение подписок, перепроверки не в ленте. Без сети."""
import os, tempfile, threading, time, unittest
from unittest import mock

import fakes
import alerts
import alerts_recheck as ar
import server
from alerts_store import AlertsStore
from chains import priority

T1, T2, T3, T4, T5 = ("0x" + c * 40 for c in "12345")
NOW = 1_800_000_000.0


def result(token, band="OK", score=70, ticker="AAA", unread=()):
    return {"token": token, "chain": "robinhood", "band": band, "score": score, "rug": None,
            "header": {"ticker": ticker}, "operators": [], "holders": [], "unread": list(unread),
            "rpc_requests": 10}


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class TestRechecker(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = AlertsStore(os.path.join(self.tmp.name, "r.db"))
        self.clock = Clock()
        self.scanned, self.logs, self.msgs, self.after = [], [], [], []
        self.live, self.rl, self.ylds = [0], [0], [0]
        self.results = {}

        def scan(token):
            self.scanned.append(token)
            r = self.results.get(token, result(token))
            if callable(r):
                return r()
            return r

        def after(res):
            self.after.append(res["token"])
            self.st.record(alerts.snapshot(res, ts=int(self.clock())))

        self.rc = ar.Rechecker(lambda: self.st, scan, after, lambda c, t, m=None: self.msgs.append((c, t, m)),
                               live_count=lambda: self.live[0], rate_limited=lambda: self.rl[0],
                               yields=lambda: self.ylds[0], clock=self.clock, sleep=self.clock.sleep,
                               log=self.logs.append, cfg={"recheck_min": 15, "max_per_hour": 60})

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def watch(self, token, chat=1, at=None):
        self.st.watch(chat, token, "robinhood", now=int(at if at is not None else self.clock()))

    def snap(self, token, age_min):
        self.st.record(alerts.snapshot(result(token), ts=int(self.clock() - age_min * 60)))

    def test_interval(self):
        self.watch(T1)
        self.snap(T1, 14)
        self.rc.tick()
        self.assertEqual(self.scanned, [])                       # моложе 15 минут — рано
        self.clock.t += 60
        self.rc.tick()
        self.assertEqual(self.scanned, [T1])
        self.assertTrue(any(l.startswith(f"alerts: recheck {T1} (age 15m, queue 1, used 1/60 this hour)")
                            for l in self.logs), self.logs)
        for _ in range(14):                                      # снимок свежий — 15 минут тишины
            self.clock.t += 60
            self.rc.tick()
        self.assertEqual(self.scanned, [T1])
        self.clock.t += 60
        self.rc.tick()
        self.assertEqual(self.scanned, [T1, T1])

    def test_no_snapshot_first_and_oldest_first(self):
        for t in (T1, T2, T3):
            self.watch(t)
        self.snap(T1, 20)
        self.snap(T2, 90)                                        # T3 без снимка — первым
        self.rc.tick()
        self.assertEqual(self.scanned, [T3, T2, T1])
        self.assertIn(f"alerts: recheck {T3} (age new, queue 3, used 1/60 this hour)", self.logs)

    def test_only_watched_tokens(self):
        self.snap(T1, 60)                                        # снимок есть, подписки нет
        self.watch(T2, at=self.clock() - 8 * 86400)              # подписка истекла
        self.rc.tick()
        self.assertEqual(self.scanned, [])

    def test_hourly_cap_sliding(self):
        self.rc.max_per_hour = 3
        for i, t in enumerate((T1, T2, T3, T4, T5)):
            self.watch(t, chat=i)                                # у чата лимит 3 — разные чаты
        self.rc.tick()
        self.assertEqual(len(self.scanned), 3)
        self.assertIn("alerts: recheck cap reached (3/3 this hour), 2 waiting", self.logs)
        self.clock.t += 30 * 60
        self.rc.tick()
        self.assertEqual(len(self.scanned), 3)                   # окно ещё не сдвинулось
        self.clock.t += 30 * 60
        self.rc.tick()
        self.assertEqual(self.scanned[3:5], [T4, T5])            # через час — дальше по очереди

    def test_skip_while_live_scan(self):
        self.watch(T1); self.watch(T2)
        self.live[0] = 1
        self.rc.tick()
        self.assertEqual(self.scanned, [])
        self.assertIn("alerts: recheck skipped: 1 live scan, 2 waiting", self.logs)
        self.live[0] = 0
        self.rc.tick()
        self.assertEqual(self.scanned, [T1, T2])

    def test_live_scan_starts_between_rechecks(self):
        self.watch(T1); self.watch(T2)

        def first():
            self.live[0] = 1                                     # пока шла перепроверка T1, пришёл живой скан
            return result(T1)
        self.results[T1] = first
        self.rc.tick()
        self.assertEqual(self.scanned, [T1])                     # T2 не начат
        self.assertEqual(self.after, [T1])

    def test_pause_on_rate_limit(self):
        self.watch(T1); self.watch(T2)

        def limited():
            self.rl[0] += 1                                      # RPC ответил 429 во время перепроверки
            return result(T1, band="DANGER", score=10)
        self.results[T1] = limited
        self.rc.tick()
        self.assertEqual(self.scanned, [T1])
        self.assertEqual(self.after, [])                         # результат не используется
        self.assertTrue(any("pausing rechecks for 10 min" in l for l in self.logs))
        for _ in range(9):
            self.clock.t += 60
            self.rc.tick()
        self.assertEqual(self.scanned, [T1])
        self.clock.t += 60
        self.rc.tick()
        self.assertEqual(self.scanned, [T1, T2])                 # T1 попробуем после min_age

    def test_pause_on_throughput_error(self):
        self.watch(T1)

        def boom():
            raise RuntimeError("Your app has exceeded its throughput limit")
        self.results[T1] = boom
        self.rc.tick()
        self.assertGreater(self.rc.paused_until, self.clock())
        self.assertTrue(any("pausing rechecks" in l and "throughput" in l for l in self.logs))

    def test_failed_scan_retried_after_interval(self):
        self.watch(T1)

        def bad():
            raise RuntimeError("not a Pons V2 token")
        self.results[T1] = bad
        self.rc.tick()
        self.assertEqual(self.rc.paused_until, 0.0)
        self.assertTrue(any(l == f"alerts: recheck {T1} failed: RuntimeError: not a Pons V2 token" for l in self.logs))
        for _ in range(14):
            self.clock.t += 60
            self.rc.tick()
        self.assertEqual(self.scanned, [T1])
        self.clock.t += 60
        self.rc.tick()
        self.assertEqual(self.scanned, [T1, T1])

    def test_discard_incomplete_after_yield(self):
        self.watch(T1)

        def starved():
            self.ylds[0] += 1                                    # запросы ждали живой скан
            return result(T1, unread=["0xw1", "0xw2"])
        self.results[T1] = starved
        self.rc.tick()
        self.assertEqual(self.after, [])
        self.assertIn(f"alerts: recheck {T1} discarded: yielded to live scans, 2 wallets unread", self.logs)
        self.results.pop(T1)
        self.clock.t += 60
        self.rc.tick()                                           # через минуту — ещё раз
        self.assertEqual(self.after, [T1])

    def test_yield_without_unread_is_used(self):
        self.watch(T1)

        def waited():
            self.ylds[0] += 1
            return result(T1)
        self.results[T1] = waited
        self.rc.tick()
        self.assertEqual(self.after, [T1])

    def test_expiry_message(self):
        self.st.record(alerts.snapshot(result(T1, ticker="CRAWL"), ts=1))
        self.watch(T1, chat=7, at=self.clock() - 7 * 86400)      # истекла ровно сейчас
        self.watch(T2, chat=7)
        self.rc.tick()
        (chat, text, markup), = self.msgs
        self.assertEqual(chat, 7)
        self.assertEqual(text, f"🔕 Stopped watching $CRAWL after 7 days. Tap Watch to renew.\n<code>{T1}</code>")
        self.assertEqual(markup, {"inline_keyboard": [[{"text": "🔔 Watch", "callback_data": f"watch:{T1}"}]]})
        self.assertEqual([w["token"] for w in self.st.watches(7, now=self.clock())], [T2])
        self.rc.tick()
        self.assertEqual(len(self.msgs), 1)                      # один раз

    def test_expiry_without_notify(self):
        self.rc.notify = None                                    # без TG_BOT_TOKEN — просто удаляем
        self.watch(T1, at=self.clock() - 8 * 86400)
        self.rc.tick()
        self.assertEqual(self.st.db.execute("SELECT COUNT(*) FROM alert_watches").fetchone()[0], 0)

    def test_config_env(self):
        with mock.patch.dict(os.environ, {"ALERTS_RECHECK_MIN": "5", "ALERTS_MAX_RECHECKS_PER_HOUR": "12"}):
            self.assertEqual(ar.config(), {"recheck_min": 5, "max_per_hour": 12})
        with mock.patch.dict(os.environ, {"ALERTS_RECHECK_MIN": "x", "ALERTS_MAX_RECHECKS_PER_HOUR": "0"}):
            self.assertEqual(ar.config(), {"recheck_min": 15, "max_per_hour": 60})


class TestPriority(unittest.TestCase):

    def test_live_thread_never_waits(self):
        with priority.live():
            t0 = time.time()
            priority.wait_turn()
            self.assertLess(time.time() - t0, 0.05)

    def test_background_waits_for_live(self):
        gate, done = threading.Event(), []
        y0 = priority.YIELDS[0]

        def bg():
            gate.wait(5)
            priority.wait_turn()
            done.append(time.time())

        th = threading.Thread(target=bg, name=f"{priority.BG}-test")
        cm = priority.live()
        cm.__enter__()
        th.start()
        gate.set()
        time.sleep(0.2)
        self.assertEqual(done, [])                               # ждёт, пока идёт живой скан
        self.assertEqual(priority.live_count(), 1)
        t_end = time.time()
        cm.__exit__(None, None, None)
        th.join(5)
        self.assertEqual(len(done), 1)
        self.assertGreaterEqual(done[0], t_end)
        self.assertEqual(priority.YIELDS[0], y0 + 1)

    def test_background_no_wait_without_live(self):
        out = []
        th = threading.Thread(target=lambda: (priority.wait_turn(), out.append(1)), name=f"{priority.BG}-x")
        th.start(); th.join(2)
        self.assertEqual(out, [1])

    def test_adapters_wait_turn(self):
        from chains import robinhood as ch, solana as sol
        for a in (ch, sol):
            with mock.patch.object(priority, "wait_turn") as wt:
                a._rate_limit()
            wt.assert_called_once()

    def test_engine_pool_inherits_background(self):
        names = []

        def history(wallet, before_block, token, window=None, cap=4):
            names.append(threading.current_thread().name)
            return cap

        with fakes.patched(history_fn=history):                  # живой скан — обычные потоки
            server.engine.scan(fakes.TOKEN)
        self.assertTrue(names and not any(n.startswith(priority.BG) for n in names), names)
        names.clear()
        with fakes.patched(history_fn=history):                  # новый контекст — кэш истории пуст
            th = threading.Thread(target=lambda: server.recheck_scan(fakes.TOKEN), name=f"{priority.BG}-alerts")
            th.start(); th.join(30)
        self.assertTrue(names and all(n.startswith(priority.BG) for n in names), names)


class TestServerPath(unittest.TestCase):
    """record_snapshot: TOO_ESTABLISHED → авто-отписка; перепроверка через сервер — не в ленте; live-счётчик."""

    def setUp(self):
        with server._lock:
            server.JOBS.clear(); server.BY_TOKEN.clear(); server.RECENT.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DRAW_DB_PATH": os.path.join(self.tmp.name, "s.db"),
                                                "ALERTS_ENABLED": "true"})
        self.env.__enter__()
        self.msgs = []
        self.nm = mock.patch.object(server, "notify_message", side_effect=lambda c, t, m=None: self.msgs.append((c, t)))
        self.nm.__enter__()

    def tearDown(self):
        self.nm.__exit__(None, None, None)
        with server._stores_lock:
            for st in list(server._recent_stores.values()) + list(server._alerts_stores.values()):
                st.close()
            server._recent_stores.clear(); server._alerts_stores.clear()
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_too_established_unwatches_all(self):
        st = server.alerts_store()
        server.record_snapshot(result(T1, band="DANGER", score=10) | {"rug": {"drop": 0.7}})
        for chat in (1, 2):
            st.watch(chat, T1, "robinhood")
        st.watch(1, T2, "robinhood")
        with mock.patch.object(server, "notifier") as n:
            server.record_snapshot(result(T1, band="TOO_ESTABLISHED", score=None, ticker="FART"))
        n.return_value.push.assert_not_called()                  # «rug is gone» и т.п. не шлём
        self.assertEqual(sorted(c for c, _ in self.msgs), [1, 2])
        self.assertEqual(self.msgs[0][1], f"🏛 <b>$FART</b> · Robinhood Chain\n<code>{T1}</code>\n\n"
                                          "This token became too established for CrawlScan, stopped watching it.")
        self.assertEqual(st.watchers(T1), [])
        self.assertEqual(st.watchers(T2), [1])

    def test_recheck_not_in_feed(self):
        st = server.alerts_store()
        st.watch(1, fakes.TOKEN, "robinhood")
        with fakes.patched():
            rc = ar.Rechecker(server.alerts_store, server.recheck_scan, server.record_snapshot, None,
                              log=lambda m: None, cfg={"recheck_min": 15, "max_per_hour": 60})
            rc.tick()
        _, cur = st.get(fakes.TOKEN)
        self.assertIsNotNone(cur)                                # снимок записан
        self.assertEqual(server.get_recent(12), {"items": []})   # в ленте нет
        self.assertEqual(server.JOBS, {})                        # и не задача пользователя

    def test_live_scan_counted(self):
        seen = []
        with fakes.patched(), mock.patch.object(server.engine, "scan",
                                                side_effect=lambda t, emit: seen.append(priority.live_count()) or result(t)):
            jid = server.start_scan(fakes.TOKEN)
            t0 = time.time()
            while not server.JOBS[jid]["done"] and time.time() - t0 < 5:
                time.sleep(0.02)
        self.assertEqual(seen, [1])
        time.sleep(0.1)
        self.assertEqual(priority.live_count(), 0)


if __name__ == "__main__":
    unittest.main()
