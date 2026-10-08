"""Стабильность под нагрузкой: SQLite (WAL, busy_timeout, общий замок), 503 busy (очередь, память), потолок истории
Pons, лимит _SCAN по переводам, постраничный getLogs, бот и сайт на busy. Без сети."""
import io, json, os, sqlite3, tempfile, threading, time, unittest, urllib.error, urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer

import fakes
from fakes import ch
import alerts_recheck as ar
import db
import engine
import memguard
import server
import test_alerts_recheck as tar
from alerts_store import AlertsStore
from recent_store import RecentStore
from rewards_store import RewardsStore
from bot import api as bot_api, text as T
from test_bot import FakeAPI, make, private, drain, RH


class TestSqlite(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "d.db")
        self.stores = [RecentStore(self.path), AlertsStore(self.path), RewardsStore(self.path)]

    def tearDown(self):
        for s in self.stores:
            s.close()
        self.tmp.cleanup()

    def test_wal_timeout_shared_lock(self):
        rs, al, rw = self.stores
        self.assertEqual(rs.db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(al.db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        self.assertIs(rs._lock, al._lock)
        self.assertIs(al._lock, rw._lock)

    def test_concurrent_writers_no_lock_errors(self):
        rs, al, rw = self.stores
        errors = []

        def run(fn):
            try:
                for i in range(60):
                    fn(i)
            except Exception as e:
                errors.append(e)

        res = lambda i: {"token": f"0x{i:040x}", "band": "SAFE", "score": 80, "header": {"ticker": "T"}}
        jobs = [lambda i: rs.record(res(i)),
                lambda i: al.watch(i, f"0x{i:040x}", "robinhood", now=int(time.time())),
                lambda i: rw.set_meta(f"k{i}", {"v": i})]
        ts = [threading.Thread(target=run, args=(f,)) for f in jobs for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(rs.recent(50)), 50)

    def test_other_process_writer_waits_not_fails(self):
        """Чужое соединение держит запись 0.5 с: хранилище ждёт (busy_timeout), а не падает с database is locked."""
        other = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        other.execute("BEGIN IMMEDIATE")
        threading.Timer(0.5, lambda: other.execute("COMMIT")).start()
        t0 = time.time()
        self.stores[0].record({"token": "0x" + "1" * 40, "band": "SAFE", "score": 1})
        self.assertGreaterEqual(time.time() - t0, 0.4)
        other.close()


class TestScanCache(unittest.TestCase):

    def setUp(self):
        self.p = [mock.patch.dict(ch._SCAN, clear=True), mock.patch.object(ch, "SCAN_CACHE_MAX_TRANSFERS", 10)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in reversed(self.p):
            p.stop()

    def test_lru_by_transfers(self):
        launch = {"block": 1}
        for t in ("a", "b", "c"):
            ch.remember_scan(t, launch, [{}] * 4, 1)
        self.assertEqual(list(ch._SCAN), ["b", "c"])          # 12 > 10: самый давний вытеснен
        ch.remember_scan("b", launch, [{}] * 4, 1)            # повтор — в конец
        ch.remember_scan("d", launch, [{}] * 4, 1)
        self.assertEqual(list(ch._SCAN), ["b", "d"])

    def test_too_large_not_stored(self):
        ch.remember_scan("a", {"block": 1}, [{}] * 3, 1)
        ch.remember_scan("big", {"block": 1}, [{}] * 11, 1)
        self.assertEqual(list(ch._SCAN), ["a"])
        self.assertIsNone(ch.scan_history("big"))

    def test_expired_dropped_and_drop_caches(self):
        with mock.patch.object(ch.time, "time", return_value=1000.0):
            ch.remember_scan("old", {"block": 1}, [{}], 1)
        ch.remember_scan("new", {"block": 1}, [{}], 1)
        self.assertEqual(list(ch._SCAN), ["new"])
        ch.drop_caches()
        self.assertEqual(ch._SCAN, {})


def _log(block, i=0):
    topic = lambda a: "0x" + "0" * 24 + a[2:]
    return {"address": fakes.TOKEN, "topics": [ch.TRANSFER_TOPIC, topic(fakes.CURVE), topic(fakes.WALLETS[block % 3])],
            "data": hex(10 + block), "transactionHash": f"0x{block:064x}", "blockNumber": hex(block), "logIndex": hex(i)}


class TestPagedLogs(unittest.TestCase):
    """getLogs окнами: один лог на блок 0..19, RPC обрезает ответ на LOG_CAP = 4 (как публичный RPC)."""

    def setUp(self):
        self.calls = []

        def rpc(method, params):
            flt = params[0]
            lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
            self.calls.append((lo, hi))
            return [_log(b) for b in range(lo, min(hi, 19) + 1)][:4]

        self.p = [mock.patch.object(ch, "rpc", side_effect=rpc), mock.patch.object(ch, "LOG_CAP", 4),
                  mock.patch.object(ch, "block_number", return_value=19)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in reversed(self.p):
            p.stop()

    def test_pages_in_order_complete(self):
        pages = list(ch.iter_logs(0, 19))
        self.assertTrue(all(len(p) < 4 for p in pages))
        self.assertEqual([int(lg["blockNumber"], 16) for p in pages for lg in p], list(range(20)))
        self.assertEqual(len(ch.get_logs(0, 19)), 20)
        trs = ch.get_token_transfers(fakes.TOKEN, 0)
        self.assertEqual([t["block"] for t in trs], list(range(20)))
        self.assertIs(trs[0]["frm"], trs[1]["frm"])              # адрес — одна строка на всю историю

    def test_cap_stops_reading(self):
        list(ch.iter_logs(0, 19))
        full = len(self.calls)
        self.calls.clear()
        with ch._log_cap(5), self.assertRaises(ch.HistoryTooLarge):
            ch.get_token_transfers(fakes.TOKEN, 0)
        self.assertLess(len(self.calls), full)                  # дальше потолка не читаем
        self.assertEqual(len(ch.get_token_transfers(fakes.TOKEN, 0)), 20)   # потолок только внутри _log_cap


class TestHistoryCap(unittest.TestCase):

    def test_engine_too_large(self):
        with fakes.patched(), mock.patch.object(ch, "SCAN_MAX_LOGS", 5):
            with self.assertRaises(engine.ScanError) as cm:
                engine.scan(fakes.TOKEN)
        self.assertEqual(str(cm.exception), "token history too large")

    def test_under_cap_same(self):
        with fakes.patched():
            a = engine.scan(fakes.TOKEN)
        with fakes.patched(), mock.patch.object(ch, "SCAN_MAX_LOGS", len(fakes.transfers())):
            b = engine.scan(fakes.TOKEN)
        self.assertEqual((a["score"], a["band"]), (b["score"], b["band"]))


class TestMemguard(unittest.TestCase):

    def setUp(self):
        memguard._state["relieved"] = 0.0

    def test_soft_limit(self):
        with mock.patch.dict(os.environ, {"MEMORY_SOFT_LIMIT_MB": ""}):
            with mock.patch.object(memguard, "cgroup_limit_mb", return_value=None):
                self.assertEqual(memguard.soft_limit_mb(), 1536)
            with mock.patch.object(memguard, "cgroup_limit_mb", return_value=1024):
                self.assertEqual(memguard.soft_limit_mb(), 716)
            with mock.patch.object(memguard, "cgroup_limit_mb", return_value=8192):
                self.assertEqual(memguard.soft_limit_mb(), 1536)
        with mock.patch.dict(os.environ, {"MEMORY_SOFT_LIMIT_MB": "900"}):
            self.assertEqual(memguard.soft_limit_mb(), 900)

    def test_over(self):
        dropped = []
        with mock.patch.dict(os.environ, {"MEMORY_SOFT_LIMIT_MB": "100"}), \
                mock.patch.object(memguard, "_droppers", [lambda: dropped.append(1)]):
            with mock.patch.object(memguard, "rss_mb", return_value=50):
                self.assertFalse(memguard.over(live=2))
            self.assertEqual(dropped, [])
            with mock.patch.object(memguard, "rss_mb", return_value=500):
                self.assertTrue(memguard.over(live=2))
                self.assertEqual(dropped, [1])
                self.assertTrue(memguard.over(live=2))
                self.assertEqual(dropped, [1])                  # не чаще RELIEVE_EVERY
                self.assertFalse(memguard.over(live=0))         # без сканов отказ память не освободит
            rss = iter([500, 60])                               # сброс кэшей помог
            memguard._state["relieved"] = 0.0
            with mock.patch.object(memguard, "rss_mb", side_effect=lambda: next(rss)):
                self.assertFalse(memguard.over(live=2))
        with mock.patch.dict(os.environ, {"MEMORY_SOFT_LIMIT_MB": "0"}), \
                mock.patch.object(memguard, "rss_mb", return_value=10 ** 6):
            self.assertFalse(memguard.over(live=5))
        with mock.patch.object(memguard, "rss_mb", return_value=None):
            self.assertFalse(memguard.over(live=5))

    def test_server_registers_adapter_cache_drop(self):
        self.assertIn(ch.drop_caches, memguard._droppers)


class TestServerBusy(unittest.TestCase):

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
        memguard._state["relieved"] = 0.0
        self.fakes = fakes.patched()
        self.fakes.__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DRAW_DB_PATH": os.path.join(self.tmp.name, "t.db"),
                                                "MEMORY_SOFT_LIMIT_MB": "0"})
        self.env.__enter__()

    def tearDown(self):
        with server._stores_lock:
            for st in server._recent_stores.values():
                st.close()
            server._recent_stores.clear()
        self.env.__exit__(None, None, None)
        self.fakes.__exit__(None, None, None)
        self.tmp.cleanup()

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read()), dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read()), dict(e.headers)

    def scan_done(self, token=fakes.TOKEN):
        code, d, _ = self.request("/api/scan", {"token": token})
        self.assertEqual(code, 200)
        for _ in range(200):
            _, r, _ = self.request(f"/api/result?job={d['job']}")
            if r["done"]:
                return d["job"], r
            time.sleep(0.05)
        self.fail("скан не завершился")

    def full(self):
        return mock.patch.object(server, "_ACTIVE", [server.MAX_CONCURRENT + server.SCAN_QUEUE_MAX])

    def test_queue_full_503_busy(self):
        with self.full():
            code, d, h = self.request("/api/scan", {"token": fakes.TOKEN})
        self.assertEqual(code, 503)
        self.assertEqual(d, {"error": "busy", "message": "Scanner is busy, try again in a few seconds"})
        self.assertEqual(h.get("retry-after") or h.get("Retry-After"), "5")
        self.assertEqual(server.BY_TOKEN, {})

    def test_cache_and_join_work_when_full(self):
        jid, r = self.scan_done()
        self.assertIn("result", r)
        with self.full():
            code, d, _ = self.request("/api/scan", {"token": fakes.TOKEN})      # кэш 10 минут
            self.assertEqual((code, d), (200, {"job": jid}))
            with server._lock:                                                 # уже идущий скан того же токена
                server.JOBS["run1"] = {"token": fakes.OTHER, "chain": "robinhood", "events": [], "done": False,
                                       "result": None, "error": None, "ts": time.time()}
                server.BY_TOKEN[fakes.OTHER] = "run1"
            code, d, _ = self.request("/api/scan", {"token": fakes.OTHER})
            self.assertEqual((code, d), (200, {"job": "run1"}))

    def test_active_counter_returns_to_zero(self):
        self.scan_done()
        time.sleep(0.1)
        self.assertEqual(server._ACTIVE[0], 0)

    def test_memory_busy(self):
        with mock.patch.dict(os.environ, {"MEMORY_SOFT_LIMIT_MB": "100"}), \
                mock.patch.object(memguard, "rss_mb", return_value=900):
            with mock.patch.object(server, "_ACTIVE", [1]):
                code, d, _ = self.request("/api/scan", {"token": fakes.TOKEN})
            self.assertEqual((code, d["error"]), (503, "busy"))
            code, _, _ = self.request("/api/scan", {"token": fakes.TOKEN})      # сканов нет — пускаем
            self.assertEqual(code, 200)

    def test_rewards_db_failure_is_503_and_scan_lives(self):
        locked = sqlite3.OperationalError("database is locked")
        with mock.patch.dict(os.environ, {"REWARDS_ENABLED": "true"}), \
                mock.patch.object(server, "rewards_store", side_effect=locked):
            code, d, _ = self.request("/api/rewards/status")
            self.assertEqual((code, d), (503, {"error": "temporarily unavailable"}))
            code, _, _ = self.request("/api/rewards/history?kind=burns")
            self.assertEqual(code, 503)
        _, r = self.scan_done()
        self.assertIn("result", r)
        self.assertEqual(self.request("/health")[0], 200)

    def test_recent_db_failure_does_not_break_scan(self):
        with mock.patch.object(server, "recent_store", side_effect=sqlite3.OperationalError("database is locked")):
            _, r = self.scan_done()
            self.assertIn("result", r)
            code, d, _ = self.request("/api/recent")
        self.assertEqual((code, d), (200, {"items": []}))


class TestRecheckMemory(unittest.TestCase):

    def test_skips_when_memory_high(self):
        tmp = tempfile.TemporaryDirectory()
        st = AlertsStore(os.path.join(tmp.name, "r.db"))
        clock, scanned, logs = tar.Clock(), [], []
        st.watch(1, tar.T1, "robinhood", now=int(clock()))
        high = [True]
        rc = ar.Rechecker(lambda: st, lambda t: scanned.append(t) or tar.result(t), lambda r: None, None,
                          live_count=lambda: 0, clock=clock, sleep=clock.sleep, log=logs.append,
                          cfg={"recheck_min": 15, "max_per_hour": 60}, memory_high=lambda: high[0])
        rc.tick()
        self.assertEqual(scanned, [])
        self.assertIn("alerts: recheck skipped: memory high, 1 waiting", logs)
        high[0] = False
        rc.tick()
        self.assertEqual(scanned, [tar.T1])
        st.close()
        tmp.cleanup()


class TestBotBusy(unittest.TestCase):

    def test_api_503_busy(self):
        body = json.dumps(server.BUSY).encode()
        err = urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(body))
        with mock.patch.object(bot_api.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(bot_api.Busy) as cm:
                bot_api.CrawlScan("https://x.test").scan(RH)
        self.assertEqual(str(cm.exception), server.BUSY["message"])

    def test_bot_shows_busy_text(self):
        class BusyAPI(FakeAPI):
            def scan(self, token):
                raise bot_api.Busy(server.BUSY["message"])

        bot, tg, _ = make(api=BusyAPI())
        bot.handle_update(private(RH))
        drain(bot)
        self.assertEqual(tg.of("editMessageText")[-1]["text"], T.SITE_BUSY)
        self.assertIn("Scanner is busy, try again in a few seconds", T.SITE_BUSY)


class TestFrontendBusy(unittest.TestCase):

    def test_build_handles_busy(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "build_frontend.py"), encoding="utf-8").read()
        self.assertIn(f"const BUSY_TEXT='{server.BUSY['message']}'", src)
        self.assertIn("d.error==='busy'", src)


if __name__ == "__main__":
    unittest.main()
