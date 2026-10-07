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
                mock.patch.object(market.urllib.request, "urlopen", side_effect=OSError("geckoterminal down")):
            res = engine.scan(fakes.TOKEN)                               # настоящий fetch_market: сеть GT упала → {}
        self.assertNotEqual(res["band"], "TOO_ESTABLISHED")
        self.assertIsNone(res["header"]["liquidity_usd"])

        def slow(token, network="robinhood"):
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
