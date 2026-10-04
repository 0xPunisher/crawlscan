"""Тесты detect.py на синтетических данных (unittest, без сети)."""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import detect as d

SUPPLY = 10_000             # 1% сапплая = 100
ZERO = "0x" + "0" * 40
CURVE = "0xcurve"
ROUTER = "0xrouter"         # сторонний бот-роутер (контракт)
BUNDLER = "0xbundler"       # контракт, раздавший токен одной транзакцией
HUB = "0xhub"               # биржа: ≥ 100 исходящих ETH-переводов
DEPLOYER = "0xdeployer"
LAUNCH_BLOCK, LAUNCH_TS = 100, 1_000_000
ETH = 10 ** 18


class Scenario:
    """Собирает переводы токена и данные по кошелькам и прогоняет весь detect."""

    def __init__(self):
        self.transfers = [{"frm": ZERO, "to": CURVE, "amount": SUPPLY, "tx": "0xmint", "block": LAUNCH_BLOCK}]
        self.data, self.inflows, self.outgoing = {}, {}, {}
        self.contracts = {CURVE, ROUTER, BUNDLER}
        self.excluded = {ZERO, CURVE}
        self.n = 0

    def wallet(self, amount, kind="buy", via=CURVE, tx=None, block=None, dt=600,
               eth_in=None, distinct=4, inflows=None, addr=None):
        """Кошелёк с первым входом: токен amount пришёл от via (если via не кривая —
        via сначала покупает на кривой в той же транзакции)."""
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

    def normal(self, k, share=100):
        """k обычных старых кошельков: разные блоки, суммы, фандеры."""
        return [self.wallet(share + 7 * i) for i in range(k)]

    def run(self):
        self.base = d.supply_base(self.transfers, SUPPLY, self.excluded)
        self.holders = d.top_holders(self.base)
        hs = [a for a, _, _ in self.holders]
        self.signals = d.wallet_signals({w: self.data[w] for w in hs}, LAUNCH_TS, DEPLOYER)
        self.packs = d.find_packs(self.signals)
        self.links = d.find_links(hs, self.transfers, self.signals, self.inflows, self.outgoing,
                                  self.contracts, self.excluded, self.packs)
        self.ops = d.operators(self.holders, self.links, self.packs,
                               self.base["circulating"] / SUPPLY)
        self.score = d.score(self.holders, self.signals, self.ops, self.base)
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
        # 90% сапплая в кривой; кошелёк держит 2% сапплая = 20% оборота
        s = Scenario()
        whale = s.wallet(200)
        for i in range(19):
            s.wallet(43 if i < 2 else 42)  # 19 кошельков на 800 → оборот 1000 = 10% сапплая
        s.run()
        circ = s.base["circulating"]
        self.assertEqual(circ, 1000)
        self.assertEqual(s.base["holders_total"], 20)
        top = s.ops[0]
        self.assertEqual(top["wallets"], [whale])
        self.assertAlmostEqual(top["share"], 0.20)                # 20% оборота
        self.assertAlmostEqual(top["share_supply"], 0.02)         # и от сапплая
        self.assertAlmostEqual(s.holders[0][2], 0.20)
        self.assertAlmostEqual(s.score["parts"]["operator"], 8.75, delta=0.1)  # из 35: 35*(25-20)/(25-5)
        self.assertIn("20.0% of float (2.0% of supply)", s.score["headline"])


if __name__ == "__main__":
    unittest.main()
