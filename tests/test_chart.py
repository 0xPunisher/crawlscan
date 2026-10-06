"""Тесты чарта: market.fetch_chart на подставном GeckoTerminal и GET /api/chart (без сети)."""
import json, os, threading, time, unittest, urllib.error, urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer

import fakes
from fakes import market
import server
from chains import solana as sol

NOW = 1_800_000_000
HOUR = 3600
SOL_TOKEN = sol.b58encode(bytes([7]) * 32)


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def pool(addr, dex, liq, age_h, base, price=1.0, quote_price=2.0, network="robinhood"):
    return {"attributes": {"address": addr, "reserve_in_usd": str(liq), "pool_created_at": iso(NOW - age_h * HOUR),
                           "base_token_price_usd": str(price), "quote_token_price_usd": str(quote_price)},
            "relationships": {"dex": {"data": {"id": dex}},
                              "base_token": {"data": {"id": f"{network}_{base}"}}}}


def ohlcv(*ts):
    """GT отдаёт новые первыми."""
    return {"data": {"attributes": {"ohlcv_list": [[t, 1, 2, 0.5, 1.5, 10] for t in sorted(ts, reverse=True)]}}}


def fake_gt(routes):
    """_gt по префиксу пути; значение-исключение — бросить его."""
    calls = []

    def gt(path, budget=3.0):
        calls.append(path)
        for prefix, resp in routes.items():
            if path.startswith(prefix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"неожиданный запрос {path}")
    gt.calls = calls
    return gt


class TestFetchChart(unittest.TestCase):
    T = fakes.TOKEN

    def chart(self, routes, token=None, network="robinhood"):
        gt = fake_gt(routes)
        with mock.patch.object(market, "_gt", side_effect=gt):
            return market.fetch_chart(token or self.T, network, now=NOW), gt.calls

    def test_most_liquid_pool_and_candles(self):
        c, calls = self.chart({
            f"/robinhood/tokens/{self.T}/pools": {"data": [pool("0xsmall", "uniswap-v4-robinhood", 10, 20, self.T),
                                                          pool("0xbig", "pons-v2-dex", 5000, 20, self.T, price=0.5)]},
            "/robinhood/pools/0xbig/ohlcv/minute?aggregate=15": ohlcv(NOW - 2 * HOUR, NOW - HOUR),
        })
        self.assertEqual((c["pool"], c["dex"], c["timeframe"], c["price_usd"]), ("0xbig", "pons-v2-dex", "15m", 0.5))
        self.assertEqual([x[0] for x in c["candles"]], [NOW - 2 * HOUR, NOW - HOUR])   # от старых к новым
        self.assertIsInstance(c["candles"][0][0], int)
        self.assertEqual(len(c["candles"][0]), 6)
        self.assertFalse(c["stitched"])
        self.assertIn(f"token={self.T}", calls[-1])
        self.assertIn("limit=1000", calls[-1])

    def test_price_when_token_is_quote(self):
        c, _ = self.chart({f"/robinhood/tokens/{self.T}/pools": {"data": [pool("0xp", "x", 1, 20, "0xweth")]},
                           "/robinhood/pools/0xp/": ohlcv(NOW)})
        self.assertEqual(c["price_usd"], 2.0)

    def test_timeframe_by_age(self):
        for age, tf in ((1, "1m"), (3, "5m"), (24, "15m"), (100, "1h"), (500, "4h")):
            c, calls = self.chart({f"/robinhood/tokens/{self.T}/pools": {"data": [pool("0xp", "x", 1, age, self.T)]},
                                   "/robinhood/pools/0xp/": ohlcv(NOW)})
            self.assertEqual(c["timeframe"], tf, age)
        self.assertEqual(market.timeframe(None)[2], "15m")
        self.assertEqual(market.timeframe(5.99)[2], "5m")
        self.assertEqual(market.timeframe(6)[2], "15m")

    def test_solana_stitches_curve_history(self):
        t = SOL_TOKEN
        c, calls = self.chart({
            f"/solana/tokens/{t}/pools": {"data": [
                pool("swap", "pumpswap", 9000, 20, t, network="solana"),
                pool("curve", "pump-fun", 0, 30, t, network="solana")]},
            "/solana/pools/swap/": ohlcv(NOW - 20 * HOUR, NOW - HOUR),
            "/solana/pools/curve/": ohlcv(NOW - 30 * HOUR, NOW - 25 * HOUR, NOW - 20 * HOUR),
        }, token=t, network="solana")
        self.assertEqual(c["pool"], "swap")
        self.assertTrue(c["stitched"])
        self.assertEqual([x[0] for x in c["candles"]], [NOW - 30 * HOUR, NOW - 25 * HOUR, NOW - 20 * HOUR, NOW - HOUR])
        self.assertEqual(c["age_h"], 30)                                          # возраст — от кривой
        self.assertEqual(c["timeframe"], "15m")

    def test_curve_pool_main_no_stitch(self):
        t = SOL_TOKEN
        c, calls = self.chart({f"/solana/tokens/{t}/pools": {"data": [pool("curve", "pump-fun", 50, 1, t, network="solana")]},
                               "/solana/pools/curve/": ohlcv(NOW)}, token=t, network="solana")
        self.assertEqual(c["dex"], "pump-fun")
        self.assertFalse(c["stitched"])
        self.assertEqual(len([x for x in calls if "ohlcv" in x]), 1)

    def test_gt_errors(self):
        c, _ = self.chart({f"/robinhood/tokens/{self.T}/pools": RuntimeError("down")})
        self.assertEqual(c, {})
        c, _ = self.chart({f"/robinhood/tokens/{self.T}/pools": {"data": []}})
        self.assertEqual(c, {})
        c, _ = self.chart({f"/robinhood/tokens/{self.T}/pools": {"data": [pool("0xp", "x", 1, 20, self.T)]},
                           "/robinhood/pools/0xp/": RuntimeError("429")})
        self.assertEqual((c["pool"], c["candles"]), ("0xp", []))                   # пул и цена есть, свечей нет

    def test_budget_on_429(self):
        def rate_limited(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        with mock.patch.object(market.urllib.request, "urlopen", side_effect=rate_limited), \
                mock.patch.object(market, "CHART_BUDGET", 0.6):
            t0 = time.time()
            self.assertEqual(market.fetch_chart(self.T), {})
            self.assertLess(time.time() - t0, 1.0)


class TestChartApi(unittest.TestCase):

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
            server.CHARTS.clear()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_bad_address(self):
        with mock.patch.object(market, "fetch_chart") as fc:
            for bad in ("", "0x123", "hello"):
                code, body = self.get(f"/api/chart?token={bad}")
                self.assertEqual((code, body), (400, {"error": "not a token address"}), bad)
            code, _ = self.get("/api/chart")
            self.assertEqual(code, 400)
            self.assertFalse(fc.called)

    def test_solana_disabled(self):
        with mock.patch.dict(os.environ, {"SOLANA_ENABLED": "false"}):
            code, body = self.get(f"/api/chart?token={SOL_TOKEN}")
        self.assertEqual((code, body), (400, {"error": "Solana support is coming soon"}))

    def test_chart_and_cache(self):
        chart = {"pool": "0xp", "dex": "pons-v2-dex", "timeframe": "15m", "candles": [[1, 1, 2, 0.5, 1.5, 3]],
                 "price_usd": 0.1, "age_h": 3.0, "stitched": False}
        with mock.patch.object(market, "fetch_chart", return_value=chart) as fc:
            code, body = self.get(f"/api/chart?token={fakes.TOKEN.upper().replace('0X', '0x')}")
            self.assertEqual(code, 200)
            self.assertEqual(body, {"token": fakes.TOKEN, "chain": "robinhood", "pool": "0xp", "dex": "pons-v2-dex",
                                    "timeframe": "15m", "candles": [[1, 1, 2, 0.5, 1.5, 3]], "price_usd": 0.1})
            fc.assert_called_once_with(fakes.TOKEN, "robinhood")
            self.assertEqual(self.get(f"/api/chart?token={fakes.TOKEN}"), (200, body))
            self.assertEqual(fc.call_count, 1)                                     # второй раз — из кэша

    def test_gt_failure_not_fatal(self):
        with mock.patch.object(market, "fetch_chart", side_effect=RuntimeError("boom")) as fc:
            code, body = self.get(f"/api/chart?token={fakes.TOKEN}")
            self.assertEqual(code, 200)
            self.assertEqual(body["candles"], [])
            self.assertIsNone(body["pool"])
            self.get(f"/api/chart?token={fakes.TOKEN}")
            self.assertEqual(fc.call_count, 1)                                     # пустой чарт тоже в кэше (минуту)
        with server._lock:                                                        # кэш пустого чарта истёк
            ts, data = server.CHARTS[fakes.TOKEN]
            server.CHARTS[fakes.TOKEN] = (ts - server.CHART_EMPTY_TTL - 1, data)
        with mock.patch.object(market, "fetch_chart", return_value={"candles": [[1, 1, 1, 1, 1, 1]]}) as fc:
            code, body = self.get(f"/api/chart?token={fakes.TOKEN}")
            self.assertEqual(len(body["candles"]), 1)
            self.assertEqual(fc.call_count, 1)
        self.assertEqual(self.get("/health"), (200, {"ok": True}))


if __name__ == "__main__":
    unittest.main()
