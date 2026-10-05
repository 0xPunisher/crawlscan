"""Тесты HTTP-сервера: настоящий ThreadingHTTPServer на свободном порту, движок на подставном адаптере."""
import json, os, threading, time, unittest, urllib.error, urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer

import fakes
from fakes import ch
import server


class TestServer(unittest.TestCase):

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
        self.fakes = fakes.patched()
        self.fakes.__enter__()

    def tearDown(self):
        self.fakes.__exit__(None, None, None)

    def request(self, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read()), dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read()), dict(e.headers)

    def wait_done(self, job, timeout=10):
        t0 = time.time()
        while time.time() - t0 < timeout:
            code, d, _ = self.request(f"/api/events?job={job}&after=0")
            self.assertEqual(code, 200)
            if d["done"]:
                return d["events"]
            time.sleep(0.05)
        self.fail("скан не завершился")

    def test_health(self):
        code, d, _ = self.request("/health")
        self.assertEqual((code, d), (200, {"ok": True}))

    def test_config_solana_flag(self):
        for value, on in (("true", True), ("false", False)):
            with mock.patch.dict(os.environ, {"SOLANA_ENABLED": value}):
                code, d, _ = self.request("/api/config")
            self.assertEqual((code, d), (200, {"solana": on}))

    def test_solana_coming_soon(self):
        sol_ca = "fjKUqPWK9m331Y5TZZNFismtqoP2MGWAMHEkB62pump"
        with mock.patch.dict(os.environ, {"SOLANA_ENABLED": "false"}):
            code, d, _ = self.request("/api/scan", {"token": sol_ca})
        self.assertEqual((code, d), (400, {"error": "Solana support is coming soon"}))
        self.assertFalse(ch.get_launch.called)

    def test_scan_bad_address(self):
        for bad in ("0x123", "", "not an address"):
            code, d, _ = self.request("/api/scan", {"token": bad})
            self.assertEqual((code, d), (400, {"error": "not a token address"}))
        code, d, _ = self.request("/api/scan", raw=b"{not json")
        self.assertEqual((code, d), (400, {"error": "bad json"}))
        self.assertFalse(ch.get_launch.called)

    def test_scan_events_and_result(self):
        code, d, _ = self.request("/api/scan", {"token": fakes.TOKEN})
        self.assertEqual(code, 200)
        job = d["job"]
        events = self.wait_done(job)
        self.assertEqual(events[0]["type"], "stage")
        self.assertEqual(events[-1]["type"], "done")
        # after=N отдаёт события с i >= N
        code, part, _ = self.request(f"/api/events?job={job}&after=5")
        self.assertEqual([e["i"] for e in part["events"]], list(range(5, len(events))))
        code, r, _ = self.request(f"/api/result?job={job}")
        self.assertEqual(code, 200)
        self.assertTrue(r["done"])
        self.assertEqual(r["result"]["token"], fakes.TOKEN)
        self.assertEqual(r["result"]["score"], events[-1]["score"])

    def test_repeat_scan_uses_cache(self):
        _, d1, _ = self.request("/api/scan", {"token": fakes.TOKEN})
        events = self.wait_done(d1["job"])
        _, d2, _ = self.request("/api/scan", {"token": fakes.TOKEN.upper().replace("0X", "0x")})
        self.assertEqual(d2["job"], d1["job"])                           # тот же скан из кэша
        code, again, _ = self.request(f"/api/events?job={d2['job']}&after=0")
        self.assertTrue(again["done"])
        self.assertEqual(again["events"], events)                        # события сразу, целиком
        self.assertEqual(ch.get_launch.call_count, 1)                    # адаптер второй раз не вызывался

    def test_not_pons_token_error_event(self):
        _, d, _ = self.request("/api/scan", {"token": fakes.OTHER})
        events = self.wait_done(d["job"])
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["detail"], "not a Pons V2 token")
        _, r, _ = self.request(f"/api/result?job={d['job']}")
        self.assertEqual(r, {"done": True, "error": "not a Pons V2 token"})

    def test_unknown_job(self):
        for path in ("/api/events?job=nope", "/api/result?job=nope"):
            code, d, _ = self.request(path)
            self.assertEqual((code, d), (404, {"error": "unknown job"}))

    def test_api_responses_not_cached(self):
        _, _, h = self.request("/health")
        self.assertEqual(h.get("cache-control"), "no-store")


if __name__ == "__main__":
    unittest.main()
