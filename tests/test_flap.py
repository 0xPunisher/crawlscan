"""Тесты лаунчпада Flap (chains/flap.py + engine + detect): без сети, подставные ответы RPC.

Выключатель FLAP_ENABLED: выключен — всё как раньше (токен Flap отклоняется, Pons без изменений);
включён — суффикс 8888/7777 без RPC, затем одна проверка Portal; исключения, покупки через агрегатор,
налоговые ноги, резерв кривой и пары, q × (1 − sellTax), дев и запасной путь, поля результата."""
import os, unittest
from unittest import mock

import fakes
from fakes import ch, engine, ZERO
from chains import flap
import detect as d

FTOKEN = "0x" + "f1" * 18 + "7777"        # налоговый токен Flap (суффикс 7777)
NOTFLAP = "0x" + "e2" * 18 + "7777"       # тот же суффикс, но Portal о нём не знает
DEV = "0x" + "de" * 20
OTHER_DEV = "0x" + "d0" * 20
EXEC = "0xc0fab674ff7ddf8b891495ba9975b0fe1dcac735"   # исполнитель агрегатора (trader в TokenBought)
TAXP = "0x" + "7a" * 20                    # taxProcessor
PAIR = "0x" + "9a" * 20                    # пара Uniswap V2 (mainPool)
SHADOW = "0x" + "5a" * 20                  # теневая пара
WETH = ch.WETH_ADDR
LAUNCH_BLOCK = fakes.LAUNCH_BLOCK
E18 = 10 ** 18
H = 107_036_752 * E18
W = [fakes.wallet(i) for i in range(12)]


def word(x):
    if isinstance(x, str):
        x = int(x, 16)
    return f"{x:064x}"


def data(*xs):
    return "0x" + "".join(word(x) for x in xs)


def tlog(frm, to, amount, tx, token=FTOKEN, i=0):
    return {"address": token, "topics": [ch.TRANSFER_TOPIC, ch.topic_for(frm), ch.topic_for(to)],
            "data": data(amount), "transactionHash": tx, "blockNumber": hex(LAUNCH_BLOCK), "logIndex": hex(i)}


def elog(address, topic, *xs):
    return {"address": address, "topics": [topic], "data": data(*xs), "transactionHash": "0x", "blockNumber": "0x0",
            "logIndex": "0x0"}


def bought(trader, amount, eth):
    return elog(flap.PORTAL, flap.TOKEN_BOUGHT, 1, FTOKEN, trader, amount, eth, eth // 100, 1)


class Chain:
    """Подставная сеть токена Flap: состояние Portal, запуск, переводы, чеки, пара."""

    def __init__(self, status=flap.STATUS_CURVE, buy_tax=400, sell_tax=400, circ=450_000_000 * E18,
                 creator=DEV, recipient=DEV, recipient_vault=False, reserves=(20 * E18, 70_000_000 * E18)):
        self.status, self.buy_tax, self.sell_tax, self.circ = status, buy_tax, sell_tax, circ
        self.creator, self.recipient, self.recipient_vault, self.reserves = creator, recipient, recipient_vault, reserves
        self.batches = []
        self.dex = status == flap.STATUS_DEX
        self.trs, self.rcs = [], {}
        self._build()

    def tr(self, frm, to, amount, tx, block=None):
        self.trs.append({"frm": frm, "to": to, "amount": amount, "tx": tx, "block": block or LAUNCH_BLOCK + len(self.trs),
                         "log_index": len(self.trs)})

    def _build(self):
        sup = flap.SUPPLY
        self.tr(ZERO, flap.PORTAL, sup, "0xlaunch", LAUNCH_BLOCK)
        self.tr(flap.PORTAL, DEV, 100_000_000 * E18, "0xlaunch", LAUNCH_BLOCK)            # покупка дева при запуске
        self.rcs["0xlaunch"] = [
            elog(flap.PORTAL, flap.TOKEN_CREATED, 1, self.creator, 7, FTOKEN, 0, 0, 0),
            elog(flap.SHADOW_FACTORY, flap.PAIR_CREATED, SHADOW, 1),
            tlog(flap.PORTAL, DEV, 100_000_000 * E18, "0xlaunch"), bought(DEV, 100_000_000 * E18, E18 // 2)]
        for i, w in enumerate(W):
            amt = (30 - i) * 1_000_000 * E18
            tx = f"0xbuy{i:02d}"
            if i % 3 == 1:                                    # через агрегатор: Portal -> исполнитель -> кошелёк
                self.tr(flap.PORTAL, EXEC, amt, tx)
                self.tr(EXEC, w, amt, tx)
                self.rcs[tx] = [bought(EXEC, amt, E18 // 10)]
            elif self.dex and i % 3 == 2:                    # после выпуска: своп в паре V2, налог на покупку
                tax = amt * self.buy_tax // 10_000
                self.tr(PAIR, FTOKEN, tax, tx)
                self.tr(PAIR, w, amt - tax, tx)
                self.rcs[tx] = [elog(PAIR, flap.V2_SWAP, E18 // 10, 0, 0, amt)]           # token1 = токен
            else:
                self.tr(flap.PORTAL, w, amt, tx)
                self.rcs[tx] = [bought(w, amt, E18 // 10)]
        # настоящий перевод между кошельками: W[11] получил первым делом от W[0]
        self.trs = [t for t in self.trs if t["to"] != W[11]]
        self.tr(W[0], W[11], 5_000_000 * E18, "0xgift")
        self.rcs["0xgift"] = [tlog(W[0], W[11], 5_000_000 * E18, "0xgift")]
        if self.dex:   # продажа с налогом токенами, ликвидация налога, выпуск в пару
            self.tr(W[2], FTOKEN, 300_000 * E18, "0xsell")
            self.tr(W[2], PAIR, 9_700_000 * E18, "0xsell")
            self.tr(FTOKEN, TAXP, 200_000 * E18, "0xliq")
            self.tr(TAXP, PAIR, 200_000 * E18, "0xliq")
            self.tr(flap.PORTAL, PAIR, 200_000_000 * E18, "0xmigrate")

    def state_words(self):
        return [self.status, 3 * E18, self.circ, 5, 6, 1_918_979_700 * 10 ** 9, H, 2_124_381_054 * E18, 800_000_000 * E18,
                0, 0, 0, self.buy_tax, self.sell_tax, PAIR if self.dex else 0, 264 * 10 ** 15, 0, 0]

    def rpc_batch(self, calls):
        self.batches.append(calls)
        out = []
        for m, p in calls:
            to, sel = p[0]["to"].lower(), p[0]["data"][:10]
            if sel == flap.SEL_STATE:
                out.append(data(*self.state_words()) if to == flap.PORTAL and FTOKEN[2:] in p[0]["data"] else None)
            elif sel == flap.SEL_TAX_PROCESSOR:
                out.append(data(TAXP) if self.buy_tax or self.sell_tax else None)
            elif sel == flap.SEL_MAIN_POOL:
                out.append(data(PAIR))
            elif sel == flap.SEL_TAX_INFO:
                out.append(data(*([10000, 0, 0, 0, self.buy_tax, self.sell_tax] + [0] * 8
                                  + [self.recipient or 0, 0, 0, 0, int(self.recipient_vault), 0])))
            elif sel == flap.SEL_TOKEN0:
                out.append(data(WETH))
            elif sel == flap.SEL_RESERVES:
                out.append(data(self.reserves[0], self.reserves[1], 0))
            else:
                out.append(None)
        return out

    def rpc(self, method, params):
        assert method == "eth_getLogs" and params[0]["address"] == FTOKEN
        return [tlog(ZERO, flap.PORTAL, flap.SUPPLY, "0xlaunch")]

    def patched(self, enabled=True):
        st = fakes.patched()
        p = lambda name, **kw: st.enter_context(mock.patch.object(ch, name, **kw))
        p("rpc_batch", side_effect=self.rpc_batch)
        p("rpc", side_effect=self.rpc)
        p("receipt_logs", side_effect=lambda txs, chunk=50: {h: self.rcs.get(h, []) for h in txs})
        p("get_token_transfers", side_effect=lambda t, b: list(self.trs) if t == FTOKEN else fakes.transfers())
        p("token_supply", side_effect=lambda t: flap.SUPPLY if t == FTOKEN else fakes.SUPPLY)
        st.enter_context(mock.patch.dict(os.environ, {"FLAP_ENABLED": "1" if enabled else "0"}))
        st.enter_context(mock.patch.dict(flap._STATE, clear=True))
        return st


class TestSwitch(unittest.TestCase):

    def test_candidate_by_suffix_only_when_enabled(self):
        with mock.patch.dict(os.environ, {"FLAP_ENABLED": "1"}):
            self.assertTrue(flap.candidate(FTOKEN) and flap.candidate("0x" + "a" * 36 + "8888"))
            self.assertFalse(flap.candidate("0x" + "a" * 36 + "7778") or flap.candidate(fakes.TOKEN))
        for v in ("", "0", "false", "no"):
            with mock.patch.dict(os.environ, {"FLAP_ENABLED": v}):
                self.assertFalse(flap.candidate(FTOKEN))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(flap.enabled())

    def test_disabled_flap_token_rejected_as_before(self):
        c = Chain()
        with c.patched(enabled=False):
            with self.assertRaises(engine.ScanError) as cm:
                engine.scan(FTOKEN)
        self.assertEqual(str(cm.exception), "not a Pons V2 token")
        self.assertEqual(c.batches, [])                    # ни одного запроса Flap

    def test_suffix_but_not_portal_goes_pons_way(self):
        c = Chain()
        with c.patched():
            with self.assertRaises(engine.ScanError) as cm:
                engine.scan(NOTFLAP)
        self.assertEqual(str(cm.exception), "not a Pons V2 or Flap token")
        self.assertEqual(len(c.batches), 1)                # одна проверка Portal (один HTTP-батч)

    def test_pons_unchanged_and_no_extra_requests(self):
        keys = ("score", "band", "parts", "gates", "metrics", "headline", "reason", "operators", "links", "rug",
                "reserve", "reserve_ok", "holders", "launch")
        res = {}
        for on in (False, True):
            c = Chain()
            with c.patched(enabled=on):
                r = engine.scan(fakes.TOKEN)
            self.assertEqual(c.batches, [])
            self.assertNotIn("launchpad", r)
            self.assertNotIn("flap", r)
            res[on] = {k: r[k] for k in keys}
        self.assertEqual(res[False], res[True])


class TestCurve(unittest.TestCase):

    def scan(self, **kw):
        self.c = Chain(**kw)
        with self.c.patched():
            return engine.scan(FTOKEN)

    def test_result_fields(self):
        r = self.scan()
        self.assertEqual(r["launchpad"], "flap")
        f = r["flap"]
        self.assertEqual((f["phase"], f["phase_text"], f["tax"]), ("bonding_curve", "bonding curve 26%",
                                                                   {"buy": 0.04, "sell": 0.04}))
        self.assertEqual((f["tax_recipient"], f["tax_recipient_is_dev"]), (DEV, True))
        self.assertEqual((f["reserve_source"], f["q_factor"], f["shadow_pair"]), ("curve_state", 1.0, SHADOW))
        self.assertEqual(f["dev_buy"], 100_000_000 * E18)
        self.assertEqual(len(self.c.batches), 1)           # состояние Portal — один раз за скан

    def test_reserve_is_virtual_curve_reserve(self):
        r = self.scan()
        self.assertEqual(r["reserve"], H + flap.SUPPLY - 450_000_000 * E18)
        self.assertTrue(r["reserve_ok"])

    def test_dev_from_creator(self):
        r = self.scan()
        self.assertEqual((r["launch"]["deployer"], r["launch"]["creator"]), (DEV, DEV))
        self.assertTrue(next(h for h in r["holders"] if h["wallet"] == DEV)["signals"]["is_deployer"])

    def test_dev_fallback_when_creator_is_infra(self):
        r = self.scan(creator=flap.VAULT_PORTAL)
        self.assertEqual((r["launch"]["deployer"], r["launch"]["creator"]), (DEV, flap.VAULT_PORTAL))

    def test_infra_not_holders(self):
        r = self.scan()
        hs = {h["wallet"] for h in r["holders"]}
        for a in (flap.PORTAL, EXEC, FTOKEN, TAXP, PAIR, SHADOW):
            self.assertNotIn(a, hs)
        self.assertIn(FTOKEN, r["flap"]["infra"])

    def test_aggregator_buys_are_buys_not_transfers_or_distributor(self):
        r = self.scan()
        sig = {h["wallet"]: h["signals"] for h in r["holders"]}
        for i in (1, 4, 7, 10):                            # купили через исполнитель агрегатора
            self.assertEqual(sig[W[i]]["kind"], "buy", W[i])
            self.assertEqual(sig[W[i]]["via"], EXEC)
            self.assertEqual(sig[W[i]]["eth_in"], E18 // 10)
        self.assertEqual(sig[W[11]]["kind"], "transfer")   # настоящий перевод остаётся переводом
        self.assertFalse([l for l in r["links"] if l["kind"] == "distributor"])
        self.assertAlmostEqual(r["metrics"]["transfer"], next(h["share"] for h in r["holders"] if h["wallet"] == W[11]))

    def test_locker_excluded_and_reported(self):
        sab = "0x548129a58bc230549df7f9e33f27e77f6779ff0f"
        self.c = Chain()
        self.c.tr(DEV, sab, 60_000_000 * E18, "0xlock")          # дев залочил покупку в Sablier
        with self.c.patched():
            r = engine.scan(FTOKEN)
        self.assertNotIn(sab, {h["wallet"] for h in r["holders"]})
        self.assertEqual(r["flap"]["locked"], {sab: 60_000_000 * E18})
        self.assertAlmostEqual(r["flap"]["locked_share_supply"], 0.06)

    def test_tax_recipient_not_dev(self):
        r = self.scan(recipient=OTHER_DEV)
        self.assertEqual((r["flap"]["tax_recipient"], r["flap"]["tax_recipient_is_dev"]), (OTHER_DEV, False))

    def test_tax_vault_is_excluded(self):
        r = self.scan(recipient=OTHER_DEV, recipient_vault=True)
        self.assertIn(OTHER_DEV, r["flap"]["infra"])

    def test_no_tax_token(self):
        r = self.scan(buy_tax=0, sell_tax=0)
        self.assertEqual((r["flap"]["tax"], r["flap"]["tax_recipient"], r["flap"]["tax_recipient_is_dev"]),
                         ({"buy": 0.0, "sell": 0.0}, None, False))


class TestDex(unittest.TestCase):

    def scan(self, **kw):
        self.c = Chain(status=flap.STATUS_DEX, buy_tax=300, sell_tax=300, circ=flap.SUPPLY, **kw)
        with self.c.patched():
            return engine.scan(FTOKEN)

    def test_phase_reserve_and_q_factor(self):
        r = self.scan()
        f = r["flap"]
        self.assertEqual((f["phase"], f["phase_text"], f["reserve_source"], f["pool"]), ("dex", "dex", "pair_reserves", PAIR))
        self.assertEqual(r["reserve"], 70_000_000 * E18)    # токенная сторона getReserves (token1)
        self.assertAlmostEqual(f["q_factor"], 0.97)
        big = r["operators"][0]
        q = big["weighted"] * r["circulating"]
        self.assertAlmostEqual(r["metrics"]["impact"], d.dump_impact(q * 0.97, r["reserve"]))
        self.assertLess(r["metrics"]["impact"], d.dump_impact(q, r["reserve"]))

    def test_tax_legs_not_holders_transfers_or_distributors(self):
        r = self.scan()
        hs = {h["wallet"]: h["signals"] for h in r["holders"]}
        self.assertNotIn(FTOKEN, hs)                       # контракт токена копит налог — не холдер
        self.assertNotIn(TAXP, hs)
        for i in (2, 5, 8):                                # купили свопом в паре (налог — перевод пары на токен)
            self.assertEqual(hs[W[i]]["kind"], "buy", W[i])
        self.assertFalse([l for l in r["links"] if l["kind"] == "distributor"])
        self.assertTrue(hs[W[2]]["sold"])                  # продажа: кошелёк -> контракт токена + пара

    def test_pair_swap_eth_in(self):
        r = self.scan()
        sig = {h["wallet"]: h["signals"] for h in r["holders"]}
        self.assertEqual(sig[W[5]]["eth_in"], E18 // 10)   # получено amt × (1 − 3%): в допуске


class TestDetectQFactor(unittest.TestCase):

    def setUp(self):
        self.holders = [(f"0x{i:040x}", (10 - i) * E18, (10 - i) / 55) for i in range(10)]
        self.sig = {a: {"is_deployer": False, "virgin": True, "kind": "transfer", "sniper": False, "sold": False,
                        "launch_bundle": False} for a, _, _ in self.holders}
        self.ops = [{"wallets": [a], "share": s, "share_supply": s, "weighted": s, "level": "single"}
                    for a, _, s in self.holders]
        self.base = {"supply": 55 * E18, "circulating": 55 * E18, "holders_total": 30}

    def test_default_is_unchanged(self):
        a = d.score(self.holders, self.sig, self.ops, self.base, 100 * E18)
        b = d.score(self.holders, self.sig, self.ops, self.base, 100 * E18, q_factor=1)
        self.assertEqual(a, b)
        self.assertEqual(a["metrics"]["impact"], d.dump_impact(self.ops[0]["weighted"] * self.base["circulating"], 100 * E18))

    def test_sell_tax_lowers_impact_and_rug(self):
        a = d.score(self.holders, self.sig, self.ops, self.base, 20 * E18)
        b = d.score(self.holders, self.sig, self.ops, self.base, 20 * E18, q_factor=0.9)
        self.assertLess(b["metrics"]["impact"], a["metrics"]["impact"])
        ra = d.rug_projection(self.holders, self.sig, self.ops, self.base, 20 * E18, "DANGER")
        rb = d.rug_projection(self.holders, self.sig, self.ops, self.base, 20 * E18, "DANGER", q_factor=0.9)
        self.assertAlmostEqual(rb["drop"], d.dump_impact(55 * E18 * 0.9, 20 * E18))
        self.assertLess(rb["drop"], ra["drop"])


if __name__ == "__main__":
    unittest.main()
