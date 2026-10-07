"""Тесты HTTP-сервера: настоящий ThreadingHTTPServer на свободном порту, движок на подставном адаптере."""
import json, os, tempfile, threading, time, unittest, urllib.error, urllib.request
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
        # лента пишет в DRAW_DB_PATH: в тестах — временный файл, не ./data/draw.db
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DRAW_DB_PATH": os.path.join(self.tmp.name, "t.db")})
        self.env.__enter__()

    def tearDown(self):
        with server._stores_lock:
            for st in server._recent_stores.values():
                st.close()
            server._recent_stores.clear()
        self.env.__exit__(None, None, None)
        self.fakes.__exit__(None, None, None)
        self.tmp.cleanup()

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
            self.assertEqual((code, d["solana"]), (200, on))

    def test_config_trade_templates(self):
        import trade
        with mock.patch.dict(os.environ, {"TRADE_URL_ROBINHOOD": "", "TRADE_URL_SOLANA": ""}):
            _, d, _ = self.request("/api/config")
        self.assertEqual(d["trade"], trade.DEFAULTS)
        env = {"TRADE_URL_ROBINHOOD": "https://x.example/{address}?c=rh", "TRADE_URL_SOLANA": "javascript:alert(1)//{address}"}
        with mock.patch.dict(os.environ, env):
            _, d, _ = self.request("/api/config")
        self.assertEqual(d["trade"], {"robinhood": "https://x.example/{address}?c=rh", "solana": trade.DEFAULTS["solana"]})
        self.assertEqual(trade.url("robinhood", "0xab", d["trade"]), "https://x.example/0xab?c=rh")
        self.assertIsNone(trade.url("base", "0xab"))

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



class TestPageCache(unittest.TestCase):
    """index.html: Cache-Control no-cache + ETag / Last-Modified, неизменная страница — 304; иконки — как раньше."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.page = os.path.join(self.tmp.name, "index.html")
        self.write(b"<html>v1</html>", 1_800_000_000)
        p = mock.patch.object(server, "ROOT", self.tmp.name)
        p.start()
        self.addCleanup(p.stop)

    def write(self, body, mtime):
        with open(self.page, "wb") as f:
            f.write(body)
        os.utime(self.page, (mtime, mtime))

    def get(self, path="/", **headers):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request("GET", path, headers=headers)
            r = c.getresponse()
            return r.status, r.read(), {k.lower(): v for k, v in r.getheaders()}
        finally:
            c.close()

    def test_headers(self):
        code, body, h = self.get("/")
        self.assertEqual((code, body), (200, b"<html>v1</html>"))
        self.assertEqual(h["cache-control"], "no-cache")
        self.assertRegex(h["etag"], r'^"[0-9a-f]{20}"$')
        self.assertEqual(h["last-modified"], "Fri, 15 Jan 2027 08:00:00 GMT")
        self.assertEqual(h["content-type"], "text/html; charset=utf-8")
        self.assertEqual(self.get("/index.html")[2]["etag"], h["etag"])
        self.assertEqual(self.get("/?ca=0x" + "ab" * 20)[2]["etag"], h["etag"])   # ссылка на скан — та же страница

    def test_304(self):
        etag, lm = self.get()[2]["etag"], self.get()[2]["last-modified"]
        for hdr in ({"If-None-Match": etag}, {"If-None-Match": "W/" + etag},      # Cloudflare при сжатии: W/
                    {"If-None-Match": '"other", ' + etag}, {"If-None-Match": "*"}, {"If-Modified-Since": lm}):
            code, body, h = self.get(**hdr)
            self.assertEqual((code, body), (304, b""), hdr)
            self.assertEqual((h["etag"], h["cache-control"]), (etag, "no-cache"), hdr)
        for hdr in ({"If-None-Match": '"other"'}, {"If-Modified-Since": "Thu, 01 Jan 2026 00:00:00 GMT"},
                    {"If-Modified-Since": "garbage"},
                    {"If-None-Match": '"other"', "If-Modified-Since": lm}):           # If-None-Match важнее
            self.assertEqual(self.get(**hdr)[0], 200, hdr)

    def test_new_deploy_new_etag(self):
        old = self.get()[2]["etag"]
        self.write(b"<html>v2 with early buyers</html>", 1_800_000_600)
        code, body, h = self.get(**{"If-None-Match": old})
        self.assertEqual((code, body), (200, b"<html>v2 with early buyers</html>"))
        self.assertNotEqual(h["etag"], old)

    def test_missing_page(self):
        os.remove(self.page)
        self.assertEqual(self.get()[:2], (200, b"<h1>rh-crawler</h1>"))

    def test_icons_cached_as_before(self):
        with mock.patch.object(server, "ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
            code, _, h = self.get("/favicon.svg")
        self.assertEqual((code, h["cache-control"]), (200, "public, max-age=86400"))
        self.assertNotIn("etag", h)


if __name__ == "__main__":
    unittest.main()
