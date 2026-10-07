"""«Too established» (проверка GeckoTerminal до скана) и защита резерва (reserve_ok). Без сети."""
import os, time, unittest
from contextlib import ExitStack
from unittest import mock

import fakes
from fakes import ch, engine, market
from chains import robinhood as rh
from chains import solana as sol
import detect as d
import recent_store
from test_rug import TestRugProjection, signals
import test_solana as ts

REAL_FETCH = market.fetch_market
BIG_OLD = {"name": "Fartcoin", "ticker": "Fartcoin", "price_usd": 0.17, "mcap_usd": 168e6, "fdv_usd": 168e6,
           "liquidity_usd": 7.6e6, "vol24h_usd": 2.3e6, "age_days": 718.8}


def scan(gt, env=None):
    events = []
    with fakes.patched(), mock.patch.object(market, "fetch_market", return_value=gt), \
            mock.patch.dict(os.environ, env or {}):
        res = engine.scan(fakes.TOKEN, events.append)
        launched = ch.get_launch.called
    return res, events, launched


class TestThreshold(unittest.TestCase):

    def test_all_three_required(self):
        L = d.ESTABLISHED
        edge = {"age_days": L["min_age_days"], "liquidity_usd": L["min_liquidity_usd"], "mcap_usd": L["min_mcap_usd"]}
        self.assertTrue(d.too_established(edge))                                  # ровно на порогах — срабатывает
        for k in edge:
            self.assertFalse(d.too_established(edge | {k: edge[k] * 0.99}), k)   # любой ниже порога — нет
            self.assertFalse(d.too_established({x: v for x, v in edge.items() if x != k}), k)  # нет числа — нет
        self.assertFalse(d.too_established({}))
        self.assertFalse(d.too_established(None))

    def test_defaults(self):
        self.assertEqual(d.ESTABLISHED, {"min_age_days": 30.0, "min_liquidity_usd": 750_000.0,
                                         "min_mcap_usd": 10_000_000.0})

    def test_env_limits(self):
        env = {"ESTABLISHED_MIN_AGE_DAYS": "365", "ESTABLISHED_MIN_LIQUIDITY_USD": "abc",
               "ESTABLISHED_MIN_MCAP_USD": "-5"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(engine.established_limits(), d.ESTABLISHED | {"min_age_days": 365.0})


class TestEngine(unittest.TestCase):

    def test_established_skips_scan(self):
        res, events, launched = scan(BIG_OLD)
        self.assertFalse(launched)                                    # ни одного запроса к адаптеру
        self.assertEqual((res["band"], res["score"], res["rpc_requests"]), ("TOO_ESTABLISHED", None, 0))
        self.assertEqual(res["headline"], "This token is too established for CrawlScan.")
        self.assertTrue(res["reason"].startswith("CrawlScan is built for fresh memecoins."))
        self.assertIsNone(res["rug"])
        self.assertEqual(res["holders"], [])
        self.assertEqual(res["header"]["ticker"], "Fartcoin")
        self.assertEqual(res["established"]["age_days"], 718.8)
        self.assertEqual([(e["type"], e["band"], e["score"]) for e in events], [("done", "TOO_ESTABLISHED", None)])

    def test_fresh_or_small_scans_normally(self):
        for gt in ({}, BIG_OLD | {"age_days": 5.0}, BIG_OLD | {"liquidity_usd": 650_000.0},
                   BIG_OLD | {"mcap_usd": 6e6}, BIG_OLD | {"age_days": None}):
            res, _, launched = scan(gt)
            self.assertTrue(launched, gt)
            self.assertIn(res["band"], ("CLEAN", "OK", "RISKY", "DANGER"), gt)

    def test_env_raises_threshold(self):
        res, _, launched = scan(BIG_OLD, {"ESTABLISHED_MIN_LIQUIDITY_USD": "1e9"})
        self.assertTrue(launched)
        self.assertNotEqual(res["band"], "TOO_ESTABLISHED")

    def test_gt_down_or_slow_scans_normally(self):
        with fakes.patched(), mock.patch.object(market, "fetch_market", REAL_FETCH), \
                mock.patch.object(engine, "MARKET_LATE", 0.3), \
                mock.patch.object(market.urllib.request, "urlopen", side_effect=OSError("geckoterminal down")):
            res = engine.scan(fakes.TOKEN)                               # настоящий fetch_market: сеть GT упала → {}
            time.sleep(0.4)                                              # фоновый запрос GT кончился под моком
        self.assertNotEqual(res["band"], "TOO_ESTABLISHED")
        self.assertIsNone(res["header"]["liquidity_usd"])

        def slow(token, network="robinhood", **kw):
            time.sleep(0.5)
            return BIG_OLD
        with fakes.patched(), mock.patch.object(market, "fetch_market", side_effect=slow), \
                mock.patch.object(engine, "MARKET_WAIT", 0.1):
            res = engine.scan(fakes.TOKEN)
        self.assertNotEqual(res["band"], "TOO_ESTABLISHED")           # не дождались GT — обычный скан
        self.assertIsNone(res["header"]["liquidity_usd"])

    def test_bot_text_matches_engine(self):
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from bot import text as T
        self.assertEqual((T.ESTABLISHED_HEADLINE, T.ESTABLISHED_TEXT, T.TOO_ESTABLISHED),
                         (d.ESTABLISHED_HEADLINE, d.ESTABLISHED_TEXT, d.TOO_ESTABLISHED))

    def test_recent_feed_entry(self):
        res, _, _ = scan(BIG_OLD)
        e = recent_store.entry(res, ts=1)
        self.assertEqual((e["band"], e["score"], e["rug"], e["ticker"]), ("TOO_ESTABLISHED", None, False, "Fartcoin"))


def gt_response(age_days=718.8, liq=7.6e6, mcap=168e6):
    """Ответ GT /tokens/{ca}?include=top_pools для настоящего fetch_market."""
    born = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - age_days * 86400))
    return {"data": {"attributes": {"name": "Fartcoin", "symbol": "Fartcoin", "price_usd": "0.17",
                                    "market_cap_usd": str(mcap), "fdv_usd": str(mcap),
                                    "total_reserve_in_usd": str(liq), "volume_usd": {"h24": "2300000"}}},
            "included": [{"type": "pool", "attributes": {"pool_created_at": born}}]}


class TestGtCache(unittest.TestCase):
    """Кэш GT (market._gt) и вердикта too established (market.established_*). Без сети."""

    def setUp(self):
        market.clear_cache()
        self.calls = []

    def tearDown(self):
        market.clear_cache()

    def gt(self, resp):
        def fn(path, budget=market.GT_BUDGET):
            self.calls.append(path)
            if isinstance(resp, Exception):
                raise resp
            return resp
        return fn

    def urlopen(self, body):
        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return __import__("json").dumps(body).encode()

        def fn(req, timeout):
            self.calls.append(req.full_url)
            return R()
        return fn

    def test_repeat_request_cached(self):
        with mock.patch.object(market.urllib.request, "urlopen", side_effect=self.urlopen(gt_response())):
            a = market.fetch_market(fakes.TOKEN)
            b = market.fetch_market(fakes.TOKEN)
        self.assertEqual(a, b)
        self.assertEqual(len(self.calls), 1)                          # повтор — из кэша
        with mock.patch.object(market.urllib.request, "urlopen", side_effect=self.urlopen(gt_response())), \
                mock.patch.object(market, "GT_CACHE_TTL", 0):
            market.fetch_market(fakes.TOKEN)
        self.assertEqual(len(self.calls), 2)                          # кэш устарел — снова в GT

    def test_failure_not_cached_and_logged(self):
        def boom(req, timeout):
            self.calls.append(req.full_url)
            raise market.urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        with mock.patch.object(market.urllib.request, "urlopen", side_effect=boom), \
                mock.patch("builtins.print") as pr:
            self.assertEqual(market.fetch_market(fakes.TOKEN, budget=0.8), {})
        line = " ".join(str(a) for c in pr.call_args_list for a in c.args)
        self.assertIn("gt: fail /robinhood/tokens/", line)
        self.assertIn("http 429", line)
        n = len(self.calls)
        with mock.patch.object(market.urllib.request, "urlopen", side_effect=self.urlopen(gt_response())):
            self.assertTrue(market.fetch_market(fakes.TOKEN))
        self.assertEqual(len(self.calls), n + 1)                      # сбой не кэшируется

    def test_timeout_logged(self):
        def slow(req, timeout):
            raise market.urllib.error.URLError(TimeoutError("timed out"))
        with mock.patch.object(market.urllib.request, "urlopen", side_effect=slow), \
                mock.patch("builtins.print") as pr:
            market.fetch_market(fakes.TOKEN, budget=0.6)
        self.assertIn("timeout", " ".join(str(a) for c in pr.call_args_list for a in c.args))

    def test_established_verdict_cached_for_a_day(self):
        with fakes.patched(), mock.patch.object(market, "fetch_market", REAL_FETCH), \
                mock.patch.object(market, "_gt", side_effect=self.gt(gt_response())):
            first = engine.scan(fakes.TOKEN)
            market._CACHE.clear()                                     # ответ GT забыт, вердикт — нет
            second = engine.scan(fakes.TOKEN)
            launched = ch.get_launch.called
        self.assertEqual((first["band"], second["band"]), ("TOO_ESTABLISHED", "TOO_ESTABLISHED"))
        self.assertEqual(len(self.calls), 1)                          # повторный скан GT не запрашивал
        self.assertFalse(launched)
        self.assertEqual(second["established"]["age_days"], first["established"]["age_days"])
        hit = market._EST[market._key(fakes.TOKEN, "robinhood")]
        market._EST[market._key(fakes.TOKEN, "robinhood")] = (hit[0] - market.ESTABLISHED_TTL - 1, hit[1])
        self.assertIsNone(market.established_get(fakes.TOKEN))        # сутки прошли — вердикт устарел
        market._CACHE.clear()


class TestLateAndLimited(unittest.TestCase):
    """Поздний ответ GT и подстраховка для старого токена без данных рынка."""

    def scan(self, fetch, launch_age_days, wait=0.1):
        ts0 = time.time() - launch_age_days * 86400
        with fakes.patched(), mock.patch.object(market, "fetch_market", side_effect=fetch), \
                mock.patch.object(engine, "MARKET_WAIT", wait), \
                mock.patch.object(ch, "block_timestamps",
                                  side_effect=lambda bl: {b: int(ts0) + (b - fakes.LAUNCH_BLOCK) for b in bl}):
            return engine.scan(fakes.TOKEN), ch.get_launch.called

    def test_late_gt_gives_too_established(self):
        def slow(token, network="robinhood", **kw):
            time.sleep(0.3)
            return BIG_OLD

        def slow_history(*a, **k):
            time.sleep(0.4)                                           # скан дольше, чем ждёт GT
            return fakes.history(*a, **k)
        with fakes.patched(history_fn=slow_history), mock.patch.object(market, "fetch_market", side_effect=slow), \
                mock.patch.object(engine, "MARKET_WAIT", 0.1):
            res = engine.scan(fakes.TOKEN)
            launched = ch.get_launch.called
            est = market.established_get(fakes.TOKEN)
        self.assertTrue(launched)                                     # скан начался без GT
        self.assertEqual((res["band"], res["score"], res["rug"]), ("TOO_ESTABLISHED", None, None))
        self.assertEqual(est["mcap_usd"], BIG_OLD["mcap_usd"])        # поздний вердикт тоже в кэше

    def test_late_gt_fresh_token_used_for_header(self):
        fresh = BIG_OLD | {"age_days": 0.5, "liquidity_usd": 500.0}

        def slow(token, network="robinhood", **kw):
            time.sleep(0.2)
            return fresh

        def slow_history(*a, **k):
            time.sleep(0.4)
            return fakes.history(*a, **k)
        with fakes.patched(history_fn=slow_history), mock.patch.object(market, "fetch_market", side_effect=slow), \
                mock.patch.object(engine, "MARKET_WAIT", 0.05):
            res = engine.scan(fakes.TOKEN)
        self.assertNotEqual(res["band"], "TOO_ESTABLISHED")
        self.assertEqual(res["header"]["liquidity_usd"], 500.0)       # поздний ответ — в шапке и в правилах
        self.assertTrue([g for g in res["gates"] if "liquidity too thin" in g])

    def gated(self):
        """Сценарий, где без подстраховки срабатывают жёсткие правила impact и transfer."""
        real_facts = ch.token_facts

        def facts(token, launch):
            f = real_facts(token, launch)
            return f | {"reserve": 1, "reserve_ok": True}            # ничтожный резерв: impact ≈ 100%
        def classify(token, trs, wallets):
            out = fakes.classify_entries(token, trs, wallets)
            for w in fakes.WALLETS[:5]:
                out[w] = out[w] | {"kind": "transfer", "eth_in": None}  # 5 крупнейших получили переводом
            return out
        return facts, classify

    def scan_gated(self, fetch, age_days):
        facts, classify = self.gated()
        ts0 = time.time() - age_days * 86400
        with fakes.patched(), mock.patch.object(market, "fetch_market", side_effect=fetch), \
                mock.patch.object(engine, "MARKET_WAIT", 0.1), \
                mock.patch.object(ch, "classify_entries", side_effect=classify), \
                mock.patch.object(ch, "token_facts", side_effect=facts, create=True), \
                mock.patch.object(ch, "block_timestamps",
                                  side_effect=lambda bl: {b: int(ts0) + (b - fakes.LAUNCH_BLOCK) for b in bl}):
            return engine.scan(fakes.TOKEN)

    def test_gated_scenario_is_danger_with_market(self):
        res = self.scan_gated(lambda *a, **k: {"liquidity_usd": 5e4}, 400)
        self.assertEqual(res["band"], "DANGER")                       # с данными рынка — как раньше
        self.assertFalse(res["limited"])
        self.assertTrue([g for g in res["gates"] if "could move price" in g])
        self.assertTrue([g for g in res["gates"] if "received by transfer" in g])

    def test_gt_down_old_token_limited(self):
        res = self.scan_gated(lambda *a, **k: {}, 400)
        self.assertTrue(res["limited"])
        self.assertFalse([g for g in res["gates"] if "could move price" in g or "received by transfer" in g])
        self.assertIsNone(res["rug"])
        self.assertTrue(res["headline"].endswith("market data unavailable, older token: holder signals are limited"))
        self.assertIn(res["band"], ("CLEAN", "OK", "RISKY"))           # не ниже RISKY
        self.assertIsNotNone(res["score"])

    def test_gt_down_young_token_unchanged(self):
        down = self.scan_gated(lambda *a, **k: {}, 2)
        self.assertFalse(down["limited"])
        self.assertEqual(down["band"], "DANGER")                      # молодой токен — правила как раньше
        self.assertTrue([g for g in down["gates"] if "could move price" in g])
        self.assertTrue([g for g in down["gates"] if "received by transfer" in g])
        self.assertIsNotNone(down["rug"])
        self.assertNotIn("market data unavailable", down["headline"])
        # тот же молодой токен на обычном скане (fakes) — ровно прежний скор
        res, _ = self.scan(lambda *a, **k: {}, 2)
        base, _ = self.scan(lambda *a, **k: {}, 0.04)
        self.assertEqual((res["score"], res["band"], res["gates"], res["headline"]),
                         (base["score"], base["band"], base["gates"], base["headline"]))

    def test_limited_score_pure(self):
        s = TestRugProjection.build(self)
        sig = signals(s)
        plain = d.score(s.holders, sig, s.ops, s.base, s.reserve)
        lim = d.score(s.holders, sig, s.ops, s.base, s.reserve, limited=True)
        self.assertEqual(plain["parts"], lim["parts"])                # части скора те же
        self.assertEqual(lim["headline"], plain["headline"] + d.LIMITED_NOTE)
        self.assertFalse([g for g in lim["gates"] if "could move price" in g or "received by transfer" in g])

    def test_limited_band_not_below_risky(self):
        s = TestRugProjection.build(self)
        sig = signals(s)
        for v in sig.values():
            v["virgin"] = True                                        # жёсткое правило virgin → DANGER
        plain = d.score(s.holders, sig, s.ops, s.base, s.reserve)
        lim = d.score(s.holders, sig, s.ops, s.base, s.reserve, limited=True)
        self.assertEqual(plain["band"], "DANGER")
        self.assertEqual(lim["band"], "RISKY")


def ds_response(age_days=550.0, liq=7.6e6, mcap=168e6, token=fakes.TOKEN, chain="robinhood"):
    """Ответ DexScreener /tokens/v1/{chain}/{token}: пары токена (base), пара, где он quote, и чужая сеть."""
    born = int((time.time() - age_days * 86400) * 1000)
    pair = lambda liq_, created, base=True, chain_=chain: {
        "chainId": chain_, "dexId": "x", "pairAddress": f"p{created}",
        "baseToken": {"address": token if base else "other", "name": "Zerebro", "symbol": "ZEREBRO"},
        "quoteToken": {"address": "other" if base else token, "name": "Zerebro", "symbol": "ZEREBRO"},
        "priceUsd": "0.02" if base else "150", "liquidity": {"usd": liq_}, "marketCap": mcap, "fdv": mcap * 1.1,
        "pairCreatedAt": created, "volume": {"h24": 1000.0}}
    return [pair(liq * 0.6, born + 86400_000), pair(liq * 0.3, born), pair(liq * 0.1, born + 5, base=False),
            pair(1e12, born - 10 ** 12, chain_="ethereum")]


class Net:
    """Подставной urlopen: GT и DexScreener по префиксу URL; значение — тело ответа, исключение или код HTTP."""

    def __init__(self, gt=429, ds=None):
        self.routes, self.calls = {market.GT: gt, market.DS: ds}, []

    def __call__(self, req, timeout):
        self.calls.append(req.full_url)
        for prefix, resp in self.routes.items():
            if req.full_url.startswith(prefix):
                break
        else:
            raise AssertionError(req.full_url)
        if isinstance(resp, int):
            raise market.urllib.error.HTTPError(req.full_url, resp, "x", {}, None)
        if isinstance(resp, Exception):
            raise resp
        body = __import__("json").dumps(resp).encode()

        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return body
        return R()

    def n(self, prefix):
        return len([c for c in self.calls if c.startswith(prefix)])


class TestDexScreenerFallback(unittest.TestCase):
    """GT не отвечает (429 / ошибка / GT_FORCE_FAIL) — рынок с DexScreener. Без сети."""

    def setUp(self):
        market.clear_cache()
        st = ExitStack()
        self.addCleanup(st.close)
        self.addCleanup(market.clear_cache)
        st.enter_context(mock.patch.object(market, "GT_FIRST", 0.3))   # быстрее переходим на DexScreener
        st.enter_context(mock.patch.object(market, "DS_BUDGET", 0.4))
        self.print = st.enter_context(mock.patch("builtins.print"))

    def logs(self):
        return " ".join(str(a) for c in self.print.call_args_list for a in c.args)

    def scan(self, net, age_days=1 / 24, env=None, gated=False):
        ts0 = time.time() - age_days * 86400
        with ExitStack() as st:
            st.enter_context(fakes.patched())
            st.enter_context(mock.patch.object(market, "fetch_market", REAL_FETCH))
            st.enter_context(mock.patch.object(market.urllib.request, "urlopen", side_effect=net))
            st.enter_context(mock.patch.object(engine, "MARKET_LATE", 1.5))
            st.enter_context(mock.patch.dict(os.environ, env or {}))
            st.enter_context(mock.patch.object(ch, "block_timestamps", side_effect=lambda bl: {
                b: int(ts0) + (b - fakes.LAUNCH_BLOCK) for b in bl}))
            if gated:
                facts, classify = TestLateAndLimited.gated(self)
                st.enter_context(mock.patch.object(ch, "classify_entries", side_effect=classify))
                st.enter_context(mock.patch.object(ch, "token_facts", side_effect=facts, create=True))
            res = engine.scan(fakes.TOKEN)
            return res, ch.get_launch.called

    def test_gt_429_dexscreener_too_established(self):
        net = Net(gt=429, ds=ds_response())
        res, launched = self.scan(net, age_days=550)
        self.assertFalse(launched)                                    # вердикт до первого RPC
        self.assertEqual((res["band"], res["market_source"], res["rpc_requests"]), ("TOO_ESTABLISHED", "dexscreener", 0))
        self.assertEqual(res["established"]["liquidity_usd"], 7.6e6)  # сумма пар токена, чужая сеть не входит
        self.assertEqual(res["established"]["mcap_usd"], 168e6)       # marketCap самой ликвидной base-пары
        self.assertAlmostEqual(res["established"]["age_days"], 550.0, places=0)  # по самой ранней паре
        self.assertEqual((res["header"]["ticker"], res["header"]["price_usd"], res["header"]["vol24h_usd"]),
                         ("ZEREBRO", 0.02, 3000.0))
        self.assertIn("gt http 429 -> dexscreener", self.logs())
        self.assertEqual(market.established_get(fakes.TOKEN)["source"], "dexscreener")   # вердикт в кэше на сутки

    def test_gt_429_dexscreener_down_limited(self):
        for ds in (503, OSError("down"), []):                        # DexScreener: ошибка, сеть, токена не знает
            market.clear_cache()
            net = Net(gt=429, ds=ds)
            res, launched = self.scan(net, age_days=400, gated=True)
            self.assertTrue(launched, ds)
            self.assertTrue(res["limited"], ds)                       # прежняя подстраховка
            self.assertIsNone(res["market_source"])
            self.assertIsNone(res["rug"])
            self.assertIn(res["band"], ("CLEAN", "OK", "RISKY"))
            self.assertGreater(net.n(market.GT), 1)                    # GT ещё раз на остаток бюджета
        self.assertIn("-> dexscreener failed", self.logs())

    def test_young_token_via_dexscreener_scans(self):
        net = Net(gt=OSError("gt down"), ds=ds_response(age_days=0.5, liq=40_000.0, mcap=200_000.0))
        res, launched = self.scan(net, age_days=0.5)
        self.assertTrue(launched)
        self.assertIn(res["band"], ("CLEAN", "OK", "RISKY", "DANGER"))
        self.assertEqual(res["market_source"], "dexscreener")
        self.assertFalse(res["limited"])
        self.assertEqual(res["header"]["liquidity_usd"], 40_000.0)    # шапка и правила — по DexScreener
        self.assertIn("gt OSError -> dexscreener", self.logs())

    def test_gt_ok_no_dexscreener(self):
        net = Net(gt=gt_response(), ds=AssertionError("DexScreener не нужен"))
        res, _ = self.scan(net)
        self.assertEqual((res["band"], res["market_source"]), ("TOO_ESTABLISHED", "gt"))
        self.assertEqual(net.n(market.DS), 0)

    def test_force_fail(self):
        net = Net(gt=gt_response(), ds=ds_response())
        with mock.patch.object(market.urllib.request, "urlopen", side_effect=net):
            self.assertEqual(market.fetch_market(fakes.TOKEN)["source"], "gt")      # по умолчанию выключено
            with mock.patch.dict(os.environ, {"GT_FORCE_FAIL": "1"}):
                m = market.fetch_market(fakes.TOKEN)                              # кэш GT не мешает
                self.assertRaises(market.urllib.error.HTTPError, market._gt, "/robinhood/x", 0.3)
        self.assertEqual(m["source"], "dexscreener")
        self.assertEqual(net.n(market.GT), 1)                          # при GT_FORCE_FAIL в GT не ходили
        self.assertIn("http 429", self.logs())
        self.assertIn("(GT_FORCE_FAIL)", self.logs())
        res, launched = self.scan(Net(gt=gt_response(), ds=ds_response()), age_days=550, env={"GT_FORCE_FAIL": "1"})
        self.assertEqual((res["band"], res["market_source"]), ("TOO_ESTABLISHED", "dexscreener"))

    def test_market_cache_shared(self):
        net = Net(gt=429, ds=ds_response())
        with mock.patch.object(market.urllib.request, "urlopen", side_effect=net):
            a = market.fetch_market(fakes.TOKEN)
            n = len(net.calls)
            b = market.fetch_market(fakes.TOKEN)
        self.assertEqual(a, b)
        self.assertEqual(len(net.calls), n)                            # повтор — из кэша, ни GT, ни DexScreener

    def test_parse_dexscreener(self):
        sol = ts.SOL_TOKEN
        with mock.patch.object(market, "_ds", return_value=ds_response(token=sol, chain="solana")) as ds:
            m = market._ds_market(sol, "solana")
        self.assertEqual(ds.call_args.args[0], f"{market.DS}/solana/{sol}")
        self.assertEqual((m["price_usd"], m["mcap_usd"], m["liquidity_usd"]), (0.02, 168e6, 7.6e6))
        quote_only = [p for p in ds_response() if p["quoteToken"]["address"] == fakes.TOKEN]
        quote_only[0] = quote_only[0] | {"marketCap": None}
        with mock.patch.object(market, "_ds", return_value=quote_only):
            m = market._ds_market(fakes.TOKEN, "robinhood")
        self.assertEqual((m["price_usd"], m["mcap_usd"], m["ticker"]), (None, None, "ZEREBRO"))  # цена пары — не наша
        no_mcap = [p | {"marketCap": None} for p in ds_response()]
        with mock.patch.object(market, "_ds", return_value=no_mcap):
            self.assertAlmostEqual(market._ds_market(fakes.TOKEN, "robinhood")["mcap_usd"], 168e6 * 1.1)  # иначе FDV
        with mock.patch.object(market, "_ds", return_value=[]):
            self.assertRaises(LookupError, market._ds_market, fakes.TOKEN, "robinhood")


class TestReserveOk(unittest.TestCase):

    def test_score_without_reserve(self):
        s = TestRugProjection.build(self)
        sig = signals(s)
        good = d.score(s.holders, sig, s.ops, s.base, s.reserve)
        bad = d.score(s.holders, sig, s.ops, s.base, s.reserve, reserve_ok=False)
        self.assertIsNotNone(good["metrics"]["impact"])
        self.assertIsNone(bad["metrics"]["impact"])
        self.assertFalse([g for g in bad["gates"] if "could move price" in g])     # правило dump impact не применяется
        self.assertTrue(bad["headline"].endswith(", liquidity not measured"))
        self.assertEqual(bad["parts"]["operator"], d._part("operator", bad["metrics"]["operator"], "operator_share"))
        self.assertEqual(list(bad["parts"]), list(d.WEIGHTS))
        self.assertIsNone(d.rug_projection(s.holders, sig, s.ops, s.base, s.reserve, "DANGER", reserve_ok=False))
        self.assertIsNotNone(d.rug_projection(s.holders, sig, s.ops, s.base, s.reserve, "DANGER"))

    def test_zero_reserve_full_drop_only_when_trusted(self):
        self.assertEqual(d.dump_impact(10, 0), 1.0)     # кривая (резерв известен): продать некуда — 100%

    def test_reserve_seen(self):
        gt = {"fdv_usd": 1_000_000.0, "liquidity_usd": 100_000.0}            # токенная сторона $50k
        self.assertTrue(engine.reserve_seen(20, 1000, gt))                    # $20k ≥ 25% от $50k
        self.assertFalse(engine.reserve_seen(10, 1000, gt))                   # $10k < $12.5k: пулы не найдены
        self.assertTrue(engine.reserve_seen(10, 1000, {}))                    # GT нет — проверить нечем

    def test_engine_marks_unmeasured(self):
        res, _, _ = scan({"fdv_usd": 1.0, "liquidity_usd": 1e12})           # резерв ничтожен рядом с ликвидностью GT
        self.assertFalse(res["reserve_ok"])
        self.assertIsNone(res["metrics"]["impact"])
        self.assertIsNone(res["rug"])
        self.assertIn("liquidity not measured", res["headline"])
        res, _, _ = scan({})
        self.assertTrue(res["reserve_ok"])
        self.assertIsInstance(res["metrics"]["impact"], float)

    def test_robinhood_zero_reserve_not_ok(self):
        launch = {"block": 1, "curve": fakes.CURVE}
        with mock.patch.object(rh, "get_token_transfers", return_value=[]), \
                mock.patch.object(rh, "token_supply", return_value=100), \
                mock.patch.object(rh, "_post", side_effect=fakes._no_network):
            f = rh.token_facts(fakes.TOKEN, launch)
        self.assertEqual((f["reserve"], f["reserve_ok"]), (0, False))


class TestSolanaPools(unittest.TestCase):
    """Пулы Raydium AMM v4 / CPMM / Meteora DAMM v2: хранилища у общего authority без данных."""

    def facts(self, owner, prog, complete=True, amount=900):
        rows = [{"account": "a1", "owner": ts.SOL_CURVE, "amount": 700, "pda": True, "label": "bonding curve"},
                {"account": "a2", "owner": owner, "amount": amount, "pda": True, "label": "x"},
                {"account": "a3", "owner": ts.SOL_WALLETS[0], "amount": 50, "pda": False, "label": "wallet"}]
        launch = {"block": ts.LAUNCH_SLOT, "curve": ts.SOL_CURVE, "complete": complete, "virtual_token_reserves": 1000}
        base = {"supply": 1650, "circulating": 50, "holders_total": 1, "balances": {ts.SOL_WALLETS[0]: 50}}
        with ExitStack() as st:
            p = lambda name, **kw: st.enter_context(mock.patch.object(sol, name, **kw))
            p("_post", side_effect=fakes._no_network)
            p("supply_base", return_value=base)
            p("get_token_transfers", return_value=[])
            p("top_accounts", return_value=rows)
            st.enter_context(mock.patch.dict(sol._OWNER_PROG, {owner: prog, ts.SOL_CURVE: sol.PUMP}))
            return sol.token_facts(ts.SOL_TOKEN, launch)

    def test_vault_authorities(self):
        for auth in sol.VAULT_AUTHORITIES:
            f = self.facts(auth, sol.SYSTEM)
            self.assertEqual((f["reserve"], f["reserve_ok"]), (900, True), sol.VAULT_AUTHORITIES[auth])

    def test_authorities_match_program_seeds(self):
        seeds = {"Raydium AMM v4": ([b"amm authority"], "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"),
                 "Raydium CPMM": ([b"vault_and_lp_mint_auth_seed"], "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"),
                 "Meteora DAMM v2": ([b"pool_authority"], "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG")}
        for addr, name in sol.VAULT_AUTHORITIES.items():
            self.assertEqual(sol.find_pda(*seeds[name]), addr, name)

    def test_unknown_system_pda_not_a_pool(self):
        other = sol.b58encode(bytes([7]) * 32)
        f = self.facts(other, sol.SYSTEM)
        self.assertEqual((f["reserve"], f["reserve_ok"]), (0, False))   # пул не найден: не «ликвидности нет»

    def test_curve_always_ok(self):
        f = self.facts(sol.b58encode(bytes([7]) * 32), sol.SYSTEM, complete=False)
        self.assertEqual((f["reserve"], f["reserve_ok"]), (1000, True))


if __name__ == "__main__":
    unittest.main()
