"""Тесты ленты «Recently scanned»: хранилище, запись после скана, /api/recent. Без сети."""
import json, os, tempfile, threading, time, unittest, urllib.error, urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer

import fakes
import server
from recent_store import RecentStore, entry

SOL = "fjKUqPWK9m331Y5TZZNFismtqoP2MGWAMHEkB62pump"


def res(token="0x" + "11" * 20, chain="robinhood", score=42, band="RISKY", rug=None, ticker="AAA", name="Alpha"):
    return {"token": token, "chain": chain, "header": {"ticker": ticker, "name": name},
            "score": score, "band": band, "rug": rug}


class TestStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RecentStore(os.path.join(self.tmp.name, "r.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_record_and_order(self):
        self.store.record(res("0x" + "11" * 20), ts=100)
        self.store.record(res(SOL, chain="solana", score=80, band="CLEAN", ticker="SOLX"), ts=200)
        self.store.record(res("0x" + "33" * 20, band="DANGER", score=10, rug={"drop": 0.6}), ts=150)
        items = self.store.recent(12)
        self.assertEqual([x["token"] for x in items], [SOL, "0x" + "33" * 20, "0x" + "11" * 20])
        self.assertEqual(items[0], {"token": SOL, "chain": "solana", "ticker": "SOLX", "name": "Alpha",
                                    "score": 80, "band": "CLEAN", "rug": False, "ts": 200})
        self.assertTrue(items[1]["rug"])
        self.assertEqual(len(self.store.recent(2)), 2)

    def test_one_row_per_token(self):
        self.store.record(res(score=42, band="RISKY"), ts=100)
        self.store.record(res("0x" + "22" * 20), ts=150)
        self.store.record(res(score=12, band="DANGER", rug={"drop": 0.5}), ts=200)   # тот же токен — обновление
        items = self.store.recent(12)
        self.assertEqual(len(items), 2)
        self.assertEqual((items[0]["token"], items[0]["score"], items[0]["band"], items[0]["rug"], items[0]["ts"]),
                         ("0x" + "11" * 20, 12, "DANGER", True, 200))

    def test_too_early_and_missing_header(self):
        e = entry({"token": "0x" + "44" * 20, "chain": "robinhood", "header": None, "score": None,
                   "band": "TOO_EARLY_OR_LATE", "rug": None}, ts=5)
        self.assertEqual((e["score"], e["ticker"], e["name"], e["rug"]), (None, None, None, False))

    def test_no_verdict_not_saved(self):
        for bad in (None, {}, {"token": "0x" + "55" * 20}, {"band": "OK"}, "error"):
            self.assertIsNone(self.store.record(bad))
        self.assertEqual(self.store.recent(), [])


class TestServerFeed(unittest.TestCase):

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
            server.JOBS.clear(); server.BY_TOKEN.clear(); server.RECENT.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DRAW_DB_PATH": os.path.join(self.tmp.name, "f.db")})
        self.env.__enter__()
        self.fakes = fakes.patched()
        self.fakes.__enter__()

    def tearDown(self):
        self.fakes.__exit__(None, None, None)
        with server._stores_lock:
            for st in server._recent_stores.values():
                st.close()
            server._recent_stores.clear()
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, json.loads(r.read()), dict(r.headers)

    def scan(self, token):
        req = urllib.request.Request(self.base + "/api/scan", data=json.dumps({"token": token}).encode(),
                                     method="POST", headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                job = json.loads(r.read())["job"]
        except urllib.error.HTTPError as e:
            with e:
                return e.code
        t0 = time.time()
        while time.time() - t0 < 10:
            _, d, _ = self.get(f"/api/result?job={job}")
            if d["done"]:
                time.sleep(0.05)       # запись в ленту — сразу после done
                return d
            time.sleep(0.05)
        self.fail("скан не завершился")

    def test_scan_is_recorded(self):
        d = self.scan(fakes.TOKEN)
        self.assertIn("result", d)
        code, feed, headers = self.get("/api/recent?limit=12")
        self.assertEqual(code, 200)
        self.assertEqual([x["token"] for x in feed["items"]], [fakes.TOKEN])
        x = feed["items"][0]
        self.assertEqual((x["chain"], x["score"], x["band"], x["rug"]),
                         ("robinhood", d["result"]["score"], d["result"]["band"], bool(d["result"]["rug"])))
        self.assertIn("max-age=20", headers["cache-control"])

    def test_errors_not_recorded(self):
        self.assertEqual(self.scan("not an address"), 400)
        d = self.scan(fakes.OTHER)                       # not a Pons V2 token — ошибка скана
        self.assertIn("error", d)
        _, feed, _ = self.get("/api/recent")
        self.assertEqual(feed, {"items": []})

    def test_db_failure_does_not_break_scan(self):
        with mock.patch.object(server, "recent_store", side_effect=OSError("disk full")):
            d = self.scan(fakes.TOKEN)
            self.assertIn("result", d)
            self.assertNotIn("error", d)
            _, feed, _ = self.get("/api/recent")
        self.assertEqual(feed, {"items": []})            # чтение тоже не падает

    def test_cache_and_limit(self):
        st = server.recent_store()
        for i in range(5):
            st.record(res("0x" + f"{i:02d}" * 20), ts=100 + i)
        _, feed, _ = self.get("/api/recent?limit=3")
        self.assertEqual([x["ts"] for x in feed["items"]], [104, 103, 102])
        st.record(res("0x" + "99" * 20), ts=200)         # мимо record_recent: кэш не сброшен
        _, again, _ = self.get("/api/recent?limit=3")
        self.assertEqual(again, feed)
        with mock.patch.object(server.time, "time", return_value=time.time() + server.RECENT_TTL + 1):
            _, fresh, _ = self.get("/api/recent?limit=3")
        self.assertEqual(fresh["items"][0]["ts"], 200)
        for bad in ("abc", "0", "1000"):
            code, d, _ = self.get(f"/api/recent?limit={bad}")
            self.assertEqual(code, 200)
            self.assertLessEqual(len(d["items"]), 50)


if __name__ == "__main__":
    unittest.main()
