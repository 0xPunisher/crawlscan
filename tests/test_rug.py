"""Тесты проекции «probably rug» (detect.rug_projection, сигнал launch_bundle) и её места в engine.scan.
Без сети: detect — синтетика из test_detect, engine — подставные адаптеры из fakes / test_solana."""
import unittest
from unittest import mock

import fakes
from fakes import ch, engine, market
from chains import solana as sol
import detect as d
from test_detect import Scenario, BUNDLER, CURVE, LAUNCH_BLOCK, LAUNCH_TS, DEPLOYER
import test_solana as ts


def signals(s, window=0):
    """Сигналы сценария с блоком запуска (Scenario.run считает их без него)."""
    hs = [a for a, _, _ in s.holders]
    return d.wallet_signals({w: s.data[w] for w in hs}, LAUNCH_TS, DEPLOYER, LAUNCH_BLOCK, window)


class TestRugProjection(unittest.TestCase):

    def build(self):
        s = Scenario()
        self.linked = [s.wallet(500, kind="transfer", via=BUNDLER, tx="0xbundle", block=101, dt=2, distinct=0)
                       for _ in range(3)]                                         # связаны и заодно transfer/virgin/снайперы
        self.transfer = s.wallet(400, kind="transfer", via="0xsolo")             # перевод от одиночного раздатчика
        self.virgin = s.wallet(300, distinct=0)
        self.sniper = s.wallet(250, dt=10)                                        # снайпер, не продавал
        self.sold = s.wallet(240, dt=10)
        s.data[self.sold]["sold"] = True                                          # снайпер, но продал
        self.launch = s.wallet(230, block=LAUNCH_BLOCK, dt=0)                     # вход в блоке запуска
        self.unread = s.wallet(220, distinct=None)                                # историю не прочитали
        self.short = s.wallet(210, distinct=2)                                    # короткая история не в счёт
        s.normal(12)
        return s.run()

    def test_union_without_double_count(self):
        s = self.build()
        self.assertEqual(s.score["band"], "DANGER")
        sig = signals(s)
        rug = d.rug_projection(s.holders, sig, s.ops, s.base, s.reserve, s.score["band"])
        kinds = {p["kind"]: p["wallets"] for p in rug["parts"]}
        self.assertEqual([p["kind"] for p in rug["parts"]], ["linked", "transfer", "virgin", "bundle"])
        self.assertEqual(sorted(kinds["linked"]), sorted(self.linked))           # приоритет: связанные первыми
        self.assertEqual(kinds["transfer"], [self.transfer])
        self.assertEqual(kinds["virgin"], [self.virgin])
        self.assertEqual(sorted(kinds["bundle"]), sorted([self.sniper, self.launch]))
        flat = [w for p in rug["parts"] for w in p["wallets"]]
        self.assertEqual(len(flat), len(set(flat)))                               # каждый кошелёк один раз
        self.assertEqual(sorted(flat), sorted(rug["wallets"]))
        for w in (self.sold, self.unread, self.short):
            self.assertNotIn(w, flat)
        share = {a: sh for a, _, sh in s.holders}
        self.assertAlmostEqual(rug["share"], sum(share[w] for w in flat))
        self.assertAlmostEqual(rug["share"], sum(p["share"] for p in rug["parts"]))  # доли складываются в итог
        ratio = s.base["circulating"] / s.supply
        self.assertAlmostEqual(rug["share_supply"], rug["share"] * ratio)
        q = sum(v for a, v, _ in s.holders if a in flat)
        self.assertAlmostEqual(rug["drop"], d.dump_impact(q, s.reserve))
        self.assertAlmostEqual(rug["level_factor"], 1 - rug["drop"])
        self.assertGreaterEqual(rug["drop"], d.RUG_MIN_DROP)

    def test_snipers_off_keeps_launch_bundle_only(self):
        s = self.build()
        rug = d.rug_projection(s.holders, signals(s), s.ops, s.base, s.reserve, "DANGER", snipers=False)
        bundle = next(p for p in rug["parts"] if p["kind"] == "bundle")
        self.assertEqual(bundle["wallets"], [self.launch])

    def test_only_danger(self):
        s = self.build()
        sig = signals(s)
        for band in ("CLEAN", "OK", "RISKY", d.TOO_EARLY, None):
            self.assertIsNone(d.rug_projection(s.holders, sig, s.ops, s.base, s.reserve, band), band)

    def test_threshold(self):
        s = self.build()
        sig = signals(s)
        rug = d.rug_projection(s.holders, sig, s.ops, s.base, s.reserve, "DANGER")
        q = sum(v for a, v, _ in s.holders if a in rug["wallets"])
        # 1 − (R/(R+q))² = 0.40 при R ≈ 3.4365·q
        above = d.rug_projection(s.holders, sig, s.ops, s.base, 3.4 * q, "DANGER")
        self.assertGreaterEqual(above["drop"], 0.40)
        self.assertIsNone(d.rug_projection(s.holders, sig, s.ops, s.base, 3.5 * q, "DANGER"))
        self.assertEqual(d.RUG_MIN_DROP, 0.40)

    def test_reserve_zero(self):
        s = self.build()
        rug = d.rug_projection(s.holders, signals(s), s.ops, s.base, 0, "DANGER")
        self.assertEqual(rug["drop"], 1.0)                                        # продать некуда
        self.assertEqual(rug["level_factor"], 0.0)

    def test_no_suspicious_supply(self):
        s = Scenario()
        s.normal(20)
        s.run()
        self.assertIsNone(d.rug_projection(s.holders, signals(s), s.ops, s.base, 1, "DANGER"))

    def test_launch_bundle_signal(self):
        def entry(block):
            return {"kind": "buy" if block is not None else None, "tx": "t", "block": block, "via": CURVE,
                    "eth_in": None, "entry_ts": None, "distinct_tokens": 4, "inflows": [], "sold": False}
        blocks = {"a": 1000, "b": 1002, "c": 1003, "e": 999, "n": None}
        data = {w: entry(b) for w, b in blocks.items()}
        sig = d.wallet_signals(data, LAUNCH_TS, DEPLOYER, 1000, 2)
        self.assertEqual({w for w, s in sig.items() if s["launch_bundle"]}, {"a", "b"})
        sig = d.wallet_signals(data, LAUNCH_TS, DEPLOYER, 1000, 0)
        self.assertEqual({w for w, s in sig.items() if s["launch_bundle"]}, {"a"})
        sig = d.wallet_signals(data, LAUNCH_TS, DEPLOYER)                          # блок запуска не передан
        self.assertFalse(any(s["launch_bundle"] for s in sig.values()))


ADAPTER_CALLS = ("get_launch", "get_token_transfers", "token_supply", "excluded_addresses", "market_addresses",
                 "classify_entries", "block_timestamps", "wallet_distinct_tokens", "is_contract", "token_meta")


class TestRugInScan(unittest.TestCase):

    def scan(self, history=lambda w, b, t, window=None, cap=4: 0, header=None, rug=True):
        # в синтетическом токене 80% сапплая в кривой: весь топ роняет цену на ~35%, порог опущен,
        # чтобы проверить проводку в engine (сам порог — TestRugProjection.test_threshold)
        with fakes.patched(history_fn=history), mock.patch.object(d, "RUG_MIN_DROP", 0.2), \
                mock.patch.object(market, "fetch_market", return_value=header or {}), \
                mock.patch.object(engine.d, "rug_projection",
                                  side_effect=None if rug else (lambda *a, **k: None),
                                  wraps=d.rug_projection if rug else None):
            events = []
            res = engine.scan(fakes.TOKEN, events.append)
            calls = {n: getattr(ch, n).call_count for n in ADAPTER_CALLS}
        return res, events, calls

    def test_danger_scan_has_rug(self):
        res, events, _ = self.scan(header={"price_usd": 2.0})                    # все девственные → DANGER
        self.assertEqual(res["band"], "DANGER")
        rug = res["rug"]
        self.assertIsNotNone(rug)
        self.assertEqual(rug["parts"][0]["kind"], "linked")
        self.assertEqual(sorted(rug["parts"][0]["wallets"]), sorted(fakes.WALLETS[:fakes.BUNDLE]))
        self.assertAlmostEqual(rug["level_usd"], 2.0 * rug["level_factor"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["rug"], rug)

    def test_no_price_no_level(self):
        res, _, _ = self.scan()
        self.assertIsNotNone(res["rug"])
        self.assertIsNone(res["rug"]["level_usd"])

    def test_not_danger_no_rug(self):
        res, events, _ = self.scan(history=fakes.history)
        self.assertNotEqual(res["band"], "DANGER")
        self.assertIsNone(res["rug"])
        self.assertIsNone(events[-1]["rug"])

    def test_score_and_requests_unchanged(self):
        with_rug, _, calls_with = self.scan()
        without, _, calls_without = self.scan(rug=False)
        self.assertIsNotNone(with_rug["rug"])
        self.assertIsNone(without["rug"])
        for k in ("score", "band", "parts", "gates", "metrics", "headline", "reason"):
            self.assertEqual(with_rug[k], without[k], k)
        self.assertEqual(calls_with, calls_without)                              # ни одного нового запроса


class TestRugSolana(unittest.TestCase):

    def test_launch_bundle_not_30s_snipers(self):
        w = ts.SOL_WALLETS
        offsets = {0: 0, 1: 2, 2: 3, 3: 10}                                       # слоты от запуска (1 слот = 1 с в фейке)
        entries = {x: ts.buy(ts.LAUNCH_SLOT + offsets.get(i, 300 + 10 * i), 10 ** 8, f"tx{i}") for i, x in enumerate(w)}
        for i in (5, 6):
            entries[w[i]] = dict(entries[w[i]], kind="transfer", eth_in=None)
        bal = {x: ts.SUPPLY // 400 * (30 - i) for i, x in enumerate(w)}
        history = lambda x, b, t, window=None, cap=4: cap
        with ts.enabled(), ts.patched_chain(sol, ts.SOL_TOKEN, w, entries, history, bal=bal):
            res = engine.scan(ts.SOL_TOKEN)
        sig = {h["wallet"]: h["signals"] for h in res["holders"]}
        self.assertEqual({x for x, s in sig.items() if s["launch_bundle"]}, {w[0], w[1]})
        self.assertTrue(sig[w[3]]["sniper"])
        self.assertEqual(res["band"], "DANGER")
        parts = {p["kind"]: p["wallets"] for p in res["rug"]["parts"]}
        self.assertEqual(sorted(parts["transfer"]), sorted([w[5], w[6]]))
        self.assertEqual(sorted(parts["bundle"]), sorted([w[0], w[1]]))         # +3 слота и снайпер 10 с — нет
        self.assertNotIn("linked", parts)


if __name__ == "__main__":
    unittest.main()
