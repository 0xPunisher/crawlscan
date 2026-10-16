"""Tests for detect.py on synthetic data (unittest, no network)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import detect as d

SUPPLY = 10_000             # 1% of supply = 100
ZERO = "0x" + "0" * 40
CURVE = "0xcurve"
POOL = "0xpool"             # pool after migration (liquidity)
LOCKER = "0xlocker"         # infrastructure, not liquidity
ROUTER = "0xrouter"         # third-party bot router (contract)
BUNDLER = "0xbundler"       # contract that distributed the token in one transaction
HUB = "0xhub"               # exchange: ≥ 100 outgoing ETH transfers
DEPLOYER = "0xdeployer"
LAUNCH_BLOCK, LAUNCH_TS = 100, 1_000_000
ETH = 10 ** 18


class Scenario:
    """Builds token transfers and per-wallet data and runs the whole detect."""

    def __init__(self, supply=SUPPLY):
        self.supply = supply
        self.transfers = [{"frm": ZERO, "to": CURVE, "amount": supply, "tx": "0xmint", "block": LAUNCH_BLOCK}]
        self.data, self.inflows, self.outgoing = {}, {}, {}
        self.contracts = {CURVE, ROUTER, BUNDLER, POOL, LOCKER}
        self.excluded = {ZERO, CURVE, POOL, LOCKER}
        self.n = 0

    def wallet(self, amount, kind="buy", via=CURVE, tx=None, block=None, dt=600,
               eth_in=None, distinct=4, inflows=None, addr=None):
        """Wallet with a first entry: token amount came from via (if via is not the curve,
        via first buys on the curve in the same transaction)."""
        self.n += 1
        w = addr or f"0xw{self.n:02d}"
        tx = tx or f"0xtx{self.n:02d}"
        block = block or LAUNCH_BLOCK + 10 * self.n
        if via != CURVE:
            self.transfers.append({"frm": CURVE, "to": via, "amount": amount, "tx": tx, "block": block})
        self.transfers.append({"frm": via, "to": w, "amount": amount, "tx": tx, "block": block})
        inflows = inflows if inflows is not None else [
            {"from": f"0xf{self.n:02d}a", "block": 1, "amount": ETH},
            {"from": f"0xf{self.n:02d}b", "block": 2, "amount": ETH}]
        self.inflows[w] = inflows
        for f in inflows:
            self.outgoing.setdefault(f["from"], 3)
        self.data[w] = {"kind": kind, "tx": tx, "block": block, "via": via,
                        "eth_in": eth_in if eth_in is not None else (ETH // 10 if kind == "buy" else None),
                        "entry_ts": LAUNCH_TS + dt, "distinct_tokens": distinct,
                        "inflows": inflows, "sold": False}
        return w

    def move(self, to, amount):
        """Tokens from the curve into infrastructure (migration to a pool, locker)."""
        self.transfers.append({"frm": CURVE, "to": to, "amount": amount, "tx": f"0xmove{to}", "block": 9999})

    def normal(self, k, share=100):
        """k ordinary old wallets: different blocks, amounts, funders."""
        return [self.wallet(share + 7 * i) for i in range(k)]

    def run(self, liquidity_usd=None):
        self.base = d.supply_base(self.transfers, self.supply, self.excluded)
        bal = {}
        for t in self.transfers:
            bal[t["frm"]] = bal.get(t["frm"], 0) - t["amount"]
            bal[t["to"]] = bal.get(t["to"], 0) + t["amount"]
        self.reserve = bal.get(CURVE, 0) + bal.get(POOL, 0)   # liquidity: curve + pool
        self.holders = d.top_holders(self.base)
        hs = [a for a, _, _ in self.holders]
        self.signals = d.wallet_signals({w: self.data[w] for w in hs}, LAUNCH_TS, DEPLOYER)
        self.packs = d.find_packs(self.signals)
        self.links = d.find_links(hs, self.transfers, self.signals, self.inflows, self.outgoing,
                                  self.contracts, self.excluded, self.packs)
        self.ops = d.operators(self.holders, self.links, self.packs,
                               self.base["circulating"] / self.supply)
        self.score = d.score(self.holders, self.signals, self.ops, self.base, self.reserve, liquidity_usd)
        return self


class TestDetect(unittest.TestCase):

    def test_a_clean_token(self):
        s = Scenario()
        for i in range(20):
            s.wallet(100 + 10 * i, eth_in=ETH // 20 + i * ETH // 100)
        s.run()
        self.assertEqual(len(s.holders), 20)
        self.assertEqual(s.links, [])
        self.assertEqual(len(s.ops), 20)
        self.assertIn(s.score["band"], ("CLEAN", "OK"))
        self.assertEqual(s.score["gates"], [])

    def test_b_bundle_one_tx(self):
        s = Scenario()
        bundle = [s.wallet(500, kind="transfer", via=BUNDLER, tx="0xbundle", block=101, dt=2, distinct=0)
                  for _ in range(8)]
        s.normal(12)
        s.run()
        top = s.ops[0]
        self.assertEqual(sorted(top["wallets"]), sorted(bundle))
        self.assertEqual(top["level"], "proven")
        self.assertAlmostEqual(top["share"], 4000 / s.base["circulating"])
        self.assertAlmostEqual(top["share_supply"], 0.40)
        self.assertTrue(all(l["kind"] == "same_tx" for l in s.links))
        self.assertEqual(s.score["band"], "DANGER")
        self.assertTrue(s.score["gates"])

    def test_c_pack(self):
        s = Scenario()
        amounts = [0.046, 0.050, 0.055, 0.058, 0.060, 0.062]
        pack = [s.wallet(200, via=ROUTER, block=500, eth_in=int(a * ETH), distinct=0,
                         inflows=[{"from": f"0xex{i}", "block": 1, "amount": ETH}]) for i, a in enumerate(amounts)]
        s.normal(14)
        s.run()
        self.assertEqual(len(s.packs), 1)
        self.assertEqual(sorted(s.packs[0]["wallets"]), sorted(pack))
        op = next(o for o in s.ops if len(o["wallets"]) > 1)
        self.assertEqual(sorted(op["wallets"]), sorted(pack))
        self.assertEqual(op["level"], "pack")
        share = 1200 / s.base["circulating"]
        self.assertAlmostEqual(op["share"], share)
        self.assertAlmostEqual(op["share_supply"], 0.12)
        self.assertAlmostEqual(op["weighted"], share * 0.6)
        self.assertTrue(all(l["level"] == "pack" for l in s.links))
        self.assertEqual(s.score["band"], "RISKY")
        self.assertLessEqual(s.score["score"], 59)
        self.assertTrue(any(g.startswith("soft:") for g in s.score["gates"]))

    def test_d_hub_funder_not_glued(self):
        s = Scenario()
        hubbed = [s.wallet(150, inflows=[{"from": HUB, "block": 5, "amount": ETH}]) for _ in range(5)]
        s.outgoing[HUB] = 150
        s.normal(15)
        s.run()
        self.assertEqual(s.links, [])
        for w in hubbed:
            self.assertIn([w], [o["wallets"] for o in s.ops])

    def test_e_bot_router_not_glued(self):
        s = Scenario()
        for i in range(6):
            s.wallet(200, via=ROUTER, block=300 + 50 * i)
        s.normal(14)
        s.run()
        self.assertEqual(s.links, [])
        self.assertEqual(len(s.ops), 20)
        self.assertTrue(all(len(o["wallets"]) == 1 for o in s.ops))

    def test_f_too_few_holders(self):
        s = Scenario()
        s.normal(7)
        s.run()
        self.assertEqual(len(s.holders), 7)
        self.assertIsNone(s.score["score"])
        self.assertEqual(s.score["band"], "TOO_EARLY_OR_LATE")
        self.assertTrue(s.score["headline"].startswith("7 wallets → 7 operators"))

    def test_g_pre_migration_circulating(self):
        # 90% of supply in the curve; the wallet holds 2% of supply = 20% of circulating
        s = Scenario()
        whale = s.wallet(200)
        for i in range(19):
            s.wallet(43 if i < 2 else 42)  # 19 wallets with 800 → circulating 1000 = 10% of supply
        s.run()
        circ = s.base["circulating"]
        self.assertEqual(circ, 1000)
        self.assertEqual(s.base["holders_total"], 20)
        top = s.ops[0]
        self.assertEqual(top["wallets"], [whale])
        self.assertAlmostEqual(top["share"], 0.20)                # 20% of circulating
        self.assertAlmostEqual(top["share_supply"], 0.02)         # and of supply
        self.assertAlmostEqual(s.holders[0][2], 0.20)
        # the curve holds 9000: selling 200 drops the price by 1 - (9000/9200)^2 ≈ 4.3% → full 35 points
        self.assertAlmostEqual(s.score["metrics"]["impact"], 1 - (9000 / 9200) ** 2)
        self.assertEqual(s.score["parts"]["operator"], 35)
        self.assertIn("20.0% of float (2.0% of supply), could move price −4% if sold", s.score["headline"])

    def test_h_dump_impact(self):
        self.assertEqual(d.dump_impact(0, 100), 0.0)
        self.assertEqual(d.dump_impact(10, 0), 1.0)                       # nowhere to sell
        self.assertAlmostEqual(d.dump_impact(100, 100), 0.75)             # 1 - (1/2)^2
        self.assertAlmostEqual(d.dump_impact(2000, 3000), 1 - 0.6 ** 2)

    def test_i_migrated_small_wallet_not_danger(self):
        # migrated: 99.9% of supply in the pool; a wallet with 0.1% of supply = ~72% of the tiny circulating
        s = Scenario(supply=1_000_000)
        whale = s.wallet(1000)
        for i in range(19):
            s.wallet(20)
        s.move(POOL, 1_000_000 - 1000 - 19 * 20)
        s.run()
        self.assertEqual(s.ops[0]["wallets"], [whale])
        self.assertGreater(s.ops[0]["share"], 0.30)                       # this used to be a stop rule
        self.assertAlmostEqual(s.ops[0]["share_supply"], 0.001)
        self.assertLess(s.score["metrics"]["impact"], 0.01)
        self.assertEqual(s.score["parts"]["operator"], 35)
        self.assertNotEqual(s.score["band"], "DANGER")
        self.assertFalse([g for g in s.score["gates"] if "operator" in g])
        self.assertNotIn("could move price", s.score["headline"])            # < 1% and no groups — no phrase

    def test_j_single_wallet_impact_is_soft_risky(self):
        # one independent wallet holds 20% of supply, 30% in liquidity: 1 - (0.3/0.5)^2 = 64% ≥ 50%,
        # but no links — a thin pool, not an operator: soft rule, RISKY, reason "one holder … (thin liquidity)"
        s = Scenario()
        whale = s.wallet(2000)
        s.normal(19, share=50)                                            # 19 wallets: 50..176
        rest = SUPPLY - 2000 - sum(50 + 7 * i for i in range(19))
        s.move(POOL, 3000)
        s.move(LOCKER, rest - 3000)
        s.run()
        self.assertEqual(s.reserve, 3000)
        self.assertEqual(s.ops[0]["wallets"], [whale])
        self.assertAlmostEqual(s.score["metrics"]["impact"], 1 - 0.6 ** 2)
        self.assertEqual(s.score["parts"]["operator"], 0)                 # ≥ 60% → 0 points (the part — as before)
        self.assertEqual(s.score["band"], "RISKY")
        self.assertLessEqual(s.score["score"], d.SOFT_GATE_SCORE)
        self.assertIn("soft: one holder could move price −64% (thin liquidity)", s.score["gates"])
        self.assertFalse([g for g in s.score["gates"] if not g.startswith("soft:")])   # no hard rules

    def lone_whale(self, **kw):
        """Like test_j: one wallet with 20% of supply in a thin pool (64% drop), wallet properties — kw."""
        s = Scenario()
        whale = s.wallet(2000, **kw)
        s.normal(19, share=50)
        rest = SUPPLY - 2000 - sum(50 + 7 * i for i in range(19))
        s.move(POOL, 3000)
        s.move(LOCKER, rest - 3000)
        s.run()
        self.assertEqual(s.ops[0]["wallets"], [whale])
        self.assertAlmostEqual(s.score["metrics"]["impact"], 1 - 0.6 ** 2)
        return s

    def assert_hard_impact(self, s):
        self.assertEqual(s.score["band"], "DANGER")
        self.assertTrue(any(g.startswith("biggest operator could move price −64% if sold") for g in s.score["gates"]))
        self.assertFalse([g for g in s.score["gates"] if "one holder" in g])

    def test_j_lone_deployer_stays_danger(self):
        self.assert_hard_impact(self.lone_whale(addr=DEPLOYER))

    def test_j_lone_virgin_stays_danger(self):
        s = self.lone_whale(distinct=0)
        self.assertTrue(s.signals[s.ops[0]["wallets"][0]]["virgin"])
        self.assert_hard_impact(s)

    def test_j_lone_transfer_stays_danger(self):
        self.assert_hard_impact(self.lone_whale(kind="transfer", via=BUNDLER))

    def assert_unknown_soft(self, s):
        self.assertEqual(s.score["band"], "RISKY")
        self.assertIn("soft: biggest holder couldn't be fully read; could move price −64% (thin liquidity)",
                      s.score["gates"])
        self.assertFalse([g for g in s.score["gates"] if not g.startswith("soft:")])

    def test_j_lone_unread_history_is_soft(self):
        # history not read within budget — not evidence: soft rule, reason "couldn't be fully read"
        self.assert_unknown_soft(self.lone_whale(distinct=None))

    def test_j_lone_entry_not_found_is_soft(self):
        self.assert_unknown_soft(self.lone_whale(kind=None))

    def test_j_lone_unread_deployer_stays_danger(self):
        # the deployer is evidence even without read history
        self.assert_hard_impact(self.lone_whale(addr=DEPLOYER, distinct=None))

    def test_virgin_share_over_read_wallets(self):
        # 20 holders: 4 unread, of 16 read 4 virgin → 25% (not 4/20 = 20%)
        s = Scenario()
        for _ in range(4):
            s.wallet(100, distinct=None)
        for _ in range(4):
            s.wallet(100, distinct=0)
        s.normal(12)
        s.run()
        m = s.score["metrics"]
        self.assertAlmostEqual(m["unread"], 4 / 20)
        self.assertAlmostEqual(m["virgin"], 4 / 16)
        self.assertEqual(s.score["notes"], [])

    def test_virgin_neutral_when_most_unread(self):
        # 15 of 20 unread: virgin share unknown — neutral half weight, no rule, a note
        s = Scenario()
        for _ in range(15):
            s.wallet(100, distinct=None)
        for _ in range(5):
            s.wallet(100, distinct=0)                                     # all read ones are virgin
        s.run()
        self.assertIsNone(s.score["metrics"]["virgin"])
        self.assertEqual(s.score["parts"]["virgin"], d.WEIGHTS["virgin"] / 2)
        self.assertFalse([g for g in s.score["gates"] if "virgin" in g])  # 5/5 virgin, but the rule is silent
        self.assertEqual(s.score["notes"], ["virgin: 15 of 20 top wallets unread — scored neutral"])

    def test_virgin_gate_needs_five_read(self):
        # 4 read virgin + 3 unread (≤ 50%): fewer than 5 read — no virgin rule
        s = Scenario()
        for _ in range(4):
            s.wallet(100, distinct=0)
        for _ in range(3):
            s.wallet(100, distinct=None)
        s.normal(3)
        s.run()
        self.assertAlmostEqual(s.score["metrics"]["virgin"], 4 / 7)
        self.assertFalse([g for g in s.score["gates"] if "virgin" in g])

    def test_j_lone_short_history_buyer_is_soft(self):
        # history is short (1–3 coins) but exists, and it bought itself — independent: soft rule
        s = self.lone_whale(distinct=2)
        self.assertEqual(s.score["band"], "RISKY")
        self.assertIn("soft: one holder could move price −64% (thin liquidity)", s.score["gates"])

    def test_j2_linked_operator_impact_is_danger(self):
        # the same 20% of supply, but in two wallets that bought in one transaction (proven link) → DANGER
        s = Scenario()
        pair = [s.wallet(1000, kind="buy", via=BUNDLER, tx="0xsame", block=101) for _ in range(2)]
        s.normal(18, share=50)
        rest = SUPPLY - 2000 - sum(50 + 7 * i for i in range(18))
        s.move(POOL, 3000)
        s.move(LOCKER, rest - 3000)
        s.run()
        self.assertEqual(sorted(s.ops[0]["wallets"]), sorted(pair))
        self.assertAlmostEqual(s.score["metrics"]["impact"], 1 - 0.6 ** 2)
        self.assertEqual(s.score["band"], "DANGER")
        self.assertTrue(any(g.startswith("biggest operator could move price −64% if sold") for g in s.score["gates"]))


    def test_k_impact_below_1pct_with_group(self):
        # a group of 3 wallets in one transaction, but 99.9% in the pool → "<1%"
        s = Scenario(supply=1_000_000)
        for _ in range(3):
            s.wallet(100, kind="transfer", via=BUNDLER, tx="0xbundle", block=101)
        for i in range(17):
            s.wallet(20 + i)
        s.move(POOL, 990_000)
        s.run()
        self.assertGreater(len(s.ops[0]["wallets"]), 1)
        self.assertLess(s.score["metrics"]["impact"], 0.01)
        self.assertIn("could move price <1% if sold", s.score["headline"])

    def test_l_thin_liquidity_soft_rule(self):
        def clean():
            s = Scenario()
            for i in range(20):
                s.wallet(100 + 10 * i, eth_in=ETH // 20 + i * ETH // 100)
            return s
        ok = clean().run(liquidity_usd=None)                               # unknown — rule not applied
        self.assertIn(ok.score["band"], ("CLEAN", "OK"))
        self.assertFalse([g for g in ok.score["gates"] if "liquidity" in g])
        self.assertFalse([g for g in clean().run(liquidity_usd=1000).score["gates"] if "liquidity" in g])
        thin = clean().run(liquidity_usd=114.2)
        self.assertIn("soft: liquidity too thin ($114)", thin.score["gates"])
        self.assertEqual(thin.score["band"], "RISKY")                      # no better than RISKY
        self.assertEqual(thin.score["score"], min(ok.score["score"], d.SOFT_GATE_SCORE))
        self.assertEqual(thin.score["parts"], ok.score["parts"])           # score parts unchanged


if __name__ == "__main__":
    unittest.main()
