"""Тесты лаунчпада Flap (chains/flap.py + engine + detect): без сети, подставные ответы RPC.

Выключатель FLAP_ENABLED: выключен — всё как раньше (токен Flap отклоняется, Pons без изменений);
включён — суффикс 8888/7777 без RPC, затем одна проверка Portal; исключения, покупки через агрегатор,
налоговые ноги, резерв кривой и пары, q × (1 − sellTax), дев и запасной путь, поля результата."""
import os, threading, unittest
from unittest import mock

import fakes
from fakes import ch, engine, ZERO
from chains import flap, priority
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


def tlog(frm, to, amount, tx, token=FTOKEN, i=0, block=LAUNCH_BLOCK):
    return {"address": token, "topics": [ch.TRANSFER_TOPIC, ch.topic_for(frm), ch.topic_for(to)],
            "data": data(amount), "transactionHash": tx, "blockNumber": hex(block), "logIndex": hex(i)}


def decode_aggregate_call(calldata):
    """calldata Multicall3.aggregate -> [(target, data)] (обратное flap._encode_aggregate)."""
    h = calldata[10:]
    word = lambda i: int(h[64 * i:64 * i + 64], 16)
    n = word(1)
    out = []
    for k in range(n):
        el = 2 + word(2 + k) // 32
        target = "0x" + h[64 * el + 24:64 * el + 64]
        ln = word(el + 2)
        out.append((target, "0x" + h[64 * (el + 3):64 * (el + 3) + 2 * ln]))
    return out


def encode_aggregate_result(rets):
    """(blockNumber, bytes[]) для ответа Multicall3.aggregate."""
    n = len(rets)
    body, offs, pos = [], [], 32 * n
    for r in rets:
        b = bytes.fromhex(r[2:])
        el = f"{len(b):064x}" + (b + b"\0" * (-len(b) % 32)).hex()
        offs.append(pos); pos += len(el) // 2; body.append(el)
    return "0x" + word(1) + word(64) + word(n) + "".join(word(o) for o in offs) + "".join(body)


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
        self.batches, self.multicalls, self.getlogs = [], 0, 0
        self.page_cap = 10 ** 9                 # логов в ответе getLogs (тесты окон ставят маленький)
        self.dex = status == flap.STATUS_DEX
        self.trs, self.rcs = [], {}
        self._build()
        self.head = max(t["block"] for t in self.trs) + 10

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

    def balance(self, a):
        return sum(t["amount"] * ((t["to"] == a) - (t["frm"] == a)) for t in self.trs)

    def rpc_batch(self, calls):
        self.batches.append(calls)
        out = []
        for m, p in calls:
            to, sel = p[0]["to"].lower(), p[0]["data"][:10]
            if to == flap.MULTICALL and sel == flap.SEL_AGGREGATE:
                self.multicalls += 1
                out.append(encode_aggregate_result([data(self.balance("0x" + d[-40:]))
                                                    for _, d in decode_aggregate_call(p[0]["data"])]))
            elif sel == flap.SEL_STATE:
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

    def logs(self, flt):
        """eth_getLogs по переводам токена: окно блоков, топики from / to (OR-списки)."""
        lo, hi = int(flt["fromBlock"], 16), (self.head if flt["toBlock"] == "latest" else int(flt["toBlock"], 16))
        tp = flt.get("topics") or []
        ok = lambda i, a: len(tp) <= i or tp[i] is None or ch.topic_for(a) in (tp[i] if isinstance(tp[i], list) else [tp[i]])
        return [tlog(t["frm"], t["to"], t["amount"], t["tx"], i=t["log_index"], block=t["block"])
                for t in sorted(self.trs, key=lambda t: (t["block"], t["log_index"]))
                if lo <= t["block"] <= hi and ok(1, t["frm"]) and ok(2, t["to"])]

    def rpc(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.head)
        assert method == "eth_getLogs" and params[0]["address"] == FTOKEN, (method, params)
        self.getlogs += 1
        logs = self.logs(params[0])
        if len(logs) > self.page_cap:   # как Alchemy: отказ с подсказкой окна, где логов не больше cap
            lo = int(params[0]["fromBlock"], 16)
            b = max(lo, int(logs[self.page_cap]["blockNumber"], 16) - 1)
            raise RuntimeError(f"Log response size exceeded. ... this block range should work: [{hex(lo)}, {hex(b)}]")
        return logs

    def get_token_transfers(self, token, from_block, to_block=None, frm=None, to=None):
        assert token == FTOKEN
        lst = lambda a: None if a is None else ({a} if isinstance(a, str) else set(a))
        f, t_ = lst(frm), lst(to)
        return [t for t in sorted(self.trs, key=lambda t: (t["block"], t["log_index"]))
                if t["block"] >= from_block and (f is None or t["frm"] in f) and (t_ is None or t["to"] in t_)]

    def patched(self, enabled=True):
        st = fakes.patched()
        p = lambda name, **kw: st.enter_context(mock.patch.object(ch, name, **kw))
        p("rpc_batch", side_effect=self.rpc_batch)
        p("rpc", side_effect=self.rpc)
        p("receipt_logs", side_effect=lambda txs, chunk=50: {h: self.rcs.get(h, []) for h in txs})
        p("get_token_transfers", side_effect=lambda t, b, *a, **k: self.get_token_transfers(t, b, *a, **k)
          if t == FTOKEN else fakes.transfers())
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

    def test_sell_via_relay_routers_counts_as_sold(self):
        # роутеры, замеченные на RELAY: продажа через них (кошелёк -> роутер) — продажа, роутер — не холдер
        routers = ("0xca980f000771f70b15647069e9e541ef73f71f2f", "0xb477751b76cf82d00a686a1232f5fcd772414af3")
        self.c = Chain(status=flap.STATUS_DEX, buy_tax=300, sell_tax=300, circ=flap.SUPPLY)
        for w, rt in zip((W[3], W[4]), routers):
            self.assertIn(rt, flap.ROUTERS)
            self.c.tr(w, rt, 1_000_000 * E18, f"0xsell{rt[2:6]}")
        with self.c.patched():
            r = engine.scan(FTOKEN)
        sig = {h["wallet"]: h["signals"] for h in r["holders"]}
        self.assertTrue(sig[W[3]]["sold"])
        self.assertTrue(sig[W[4]]["sold"])
        self.assertFalse({h["wallet"] for h in r["holders"]} & set(routers))
        self.assertEqual({W[3], W[4]} & d.sellers(self.c.trs, flap.ROUTERS), {W[3], W[4]})


WHALE = fakes.wallet(220)
X_DIST = fakes.wallet(200)                 # не холдер: раздал токены двум холдерам в середине истории


def long_chain(whale=False):
    """Токен с длинной историей: запуск и покупки в начале, в середине — продажа холдера, прямой перевод между
    холдерами, общий раздатчик (и, если whale, спящий кит: купил в середине и больше не двигался), дальше —
    шум мелких кошельков до самого конца (свежее окно — только шум)."""
    c = Chain()
    b = LAUNCH_BLOCK + 1000
    fill = [fakes.wallet(100 + i) for i in range(40)]
    for i, f in enumerate(fill):
        c.tr(flap.PORTAL, f, 1000 * E18, f"0xf{i:03d}", b + i)
    c.tr(W[3], flap.PORTAL, 1_000_000 * E18, "0xmidsell", b + 4000)
    c.tr(W[5], W[6], 2_000_000 * E18, "0xmiddirect", b + 4100)
    c.tr(flap.PORTAL, X_DIST, 3_000 * E18, "0xxbuy", b + 4200)
    c.tr(X_DIST, W[8], 1_000 * E18, "0xdist1", b + 4300)
    c.tr(X_DIST, W[9], 1_000 * E18, "0xdist2", b + 4301)
    if whale:
        c.tr(flap.PORTAL, WHALE, 40_000_000 * E18, "0xwhale", b + 4500)
        c.rcs["0xwhale"] = [bought(WHALE, 40_000_000 * E18, E18)]
    for k in range(200):
        c.tr(fill[k % 40], fill[(k + 1) % 40], E18, f"0xn{k:03d}", b + 6000 + 100 * k)
    c.head = max(t["block"] for t in c.trs) + 10
    c.page_cap = 25
    return c


def windowed(max_logs=30, samples=2):
    """Маленькие окна для подставной истории: порог, окно запуска, страница RPC, свежих страниц, выборок."""
    return mock.patch.multiple(flap, HISTORY_MAX_LOGS=max_logs, EARLY_BLOCKS=100, PAGE_LOGS=25, RECENT_PAGES=1,
                               SAMPLE_PAGES=samples)


class TestWindowedHistory(unittest.TestCase):
    KEYS = ("score", "band", "parts", "gates", "metrics", "headline", "reason", "operators", "links", "rug",
            "circulating", "reserve")

    def scan(self, c, max_logs, samples=2):
        with c.patched(), windowed(max_logs, samples):
            return engine.scan(FTOKEN)

    def test_windowed_equals_full(self):
        full = self.scan(long_chain(), 10 ** 9)
        c = long_chain()
        win = self.scan(c, 30)
        self.assertEqual(full["flap"]["history"]["mode"], "full")
        h = win["flap"]["history"]
        self.assertEqual(h["mode"], "windowed")
        self.assertTrue(h["top_exact"] and h["early_complete"])
        self.assertLess(h["logs_read"], len(c.trs))          # прочитана не вся история
        for k in self.KEYS:
            self.assertEqual(win[k], full[k], k)
        strip = lambda r: [(x["wallet"], x["share"], x["signals"]) for x in r["holders"]]
        self.assertEqual(strip(win), strip(full))
        kinds = {l["kind"] for l in win["links"]}
        self.assertTrue({"direct", "distributor"} <= kinds, kinds)   # связи из середины истории найдены
        sig = {x["wallet"]: x["signals"] for x in win["holders"]}
        self.assertTrue(sig[W[3]]["sold"])                   # продажа из середины истории
        self.assertGreaterEqual(c.multicalls, 1)

    def test_sampling_finds_mid_history_whale(self):
        full = self.scan(long_chain(whale=True), 10 ** 9)
        win = self.scan(long_chain(whale=True), 30, samples=8)
        self.assertIn(WHALE, [x["wallet"] for x in win["holders"]])     # кит из середины — в выборке
        self.assertTrue(win["flap"]["history"]["top_exact"])
        self.assertFalse(win["limited"])
        for k in self.KEYS:
            self.assertEqual(win[k], full[k], k)

    def test_dormant_whale_reported_not_exact(self):
        full = self.scan(long_chain(whale=True), 10 ** 9)
        win = self.scan(long_chain(whale=True), 30, samples=0)
        self.assertIn(WHALE, [x["wallet"] for x in full["holders"]])
        self.assertNotIn(WHALE, [x["wallet"] for x in win["holders"]])   # не попал ни в одно окно
        h = win["flap"]["history"]
        self.assertFalse(h["top_exact"])
        self.assertLess(h["coverage"], 0.97)
        self.assertEqual(win["circulating"], full["circulating"])        # оборот — по балансам, точный
        self.assertTrue(win["limited"])                                  # топ не точный: сигналы ограничены
        self.assertIn("top holders partially read", win["headline"])

    def test_small_history_read_fully_in_pages(self):
        c = long_chain()
        r = self.scan(c, 10 ** 9)
        self.assertEqual((r["flap"]["history"]["mode"], r["flap"]["history"]["logs"]), ("full", len(c.trs)))
        self.assertGreater(c.getlogs, 1)                     # страницами по подсказке RPC
        self.assertEqual(c.multicalls, 0)

    def test_deadline_stops_reading(self):
        c = long_chain()
        with c.patched(), windowed(10 ** 9):
            flap.detect(FTOKEN)
            trs, base, info, _ = flap.history(FTOKEN, LAUNCH_BLOCK, flap.SUPPLY, {flap.PORTAL, ZERO},
                                              deadline=0.0)   # время уже вышло: одна страница и окна без свежих
        self.assertEqual(info["mode"], "windowed")
        self.assertEqual(info["recent_logs"], 0)
        self.assertTrue(base["balances"])

    def test_multicall_splits_on_413(self):
        import urllib.error
        c = long_chain()
        real = c.rpc_batch
        def picky(calls):   # как RPC: большой запрос — 413
            if len(calls) > 1 or len(decode_aggregate_call(calls[0][1][0]["data"])) > 3:
                raise urllib.error.HTTPError("rpc", 413, "Payload Too Large", {}, None)
            return real(calls)
        addrs = sorted({t["to"] for t in c.trs})
        with c.patched(), mock.patch.object(ch, "rpc_batch", side_effect=picky), \
                mock.patch.multiple(flap, MULTICALL_CHUNK=10, MULTICALL_PER_HTTP=3):
            got = flap.balances(FTOKEN, addrs)
        self.assertEqual(got, {a: c.balance(a) for a in addrs})

    def test_constants_match_detect(self):
        self.assertEqual((flap.HOLDERS_N, flap.DUST_SHARE), (d.TOP_N, d.DUST_SHARE))

    def test_multicall_roundtrip(self):
        calls = [(FTOKEN, flap.SEL_BALANCE + flap._arg(a)) for a in W[:3]]
        self.assertEqual(decode_aggregate_call(flap._encode_aggregate(calls)), calls)
        self.assertEqual(flap._decode_aggregate(encode_aggregate_result([data(5), data(7)])), [data(5), data(7)])


class TestDetectQFactor(unittest.TestCase):

    def setUp(self):
        self.holders = [(f"0x{i:040x}", (10 - i) * E18, (10 - i) / 55) for i in range(10)]
        self.sig = {a: {"is_deployer": False, "virgin": True, "kind": "transfer", "sniper": False, "sold": False,
                        "launch_bundle": False, "unread": False} for a, _, _ in self.holders}
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


class TestMainRulesOnFlap(unittest.TestCase):
    """Правила вердикта из main (одиночный кит в тонком пуле, probably rug только по поведению) — для налоговых
    токенов Flap так же, как для Pons, с impact после налога (q × q_factor)."""

    def setUp(self):
        whale = "0x" + "aa" * 20
        rest = [f"0x{i:040x}" for i in range(1, 10)]
        self.holders = [(whale, 50 * E18, 0.5)] + [(a, 50 * E18 // 9, 0.5 / 9) for a in rest]
        self.sig = {a: {"is_deployer": False, "virgin": False, "kind": "buy", "sniper": False, "sold": False,
                        "launch_bundle": False, "unread": False, "short_history": False} for a, _, _ in self.holders}
        self.ops = [{"wallets": [a], "share": s_, "share_supply": s_, "weighted": s_, "level": "single"}
                    for a, _, s_ in self.holders]
        self.base = {"supply": 100 * E18, "circulating": 100 * E18, "holders_total": 30}

    def score(self, reserve, q_factor):
        return d.score(self.holders, self.sig, self.ops, self.base, reserve, q_factor=q_factor)

    def test_independent_whale_thin_pool_is_soft_risky_with_tax(self):
        sc = self.score(50 * E18, 0.9)
        self.assertAlmostEqual(sc["metrics"]["impact"], d.dump_impact(50 * E18 * 0.9, 50 * E18))
        self.assertGreaterEqual(sc["metrics"]["impact"], d.GATE_IMPACT)
        self.assertEqual(sc["band"], "RISKY")
        self.assertTrue(sc["gates"] and all(g.startswith("soft:") for g in sc["gates"]), sc["gates"])
        self.assertLessEqual(sc["score"], d.SOFT_GATE_SCORE)
        rug = d.rug_projection(self.holders, self.sig, self.ops, self.base, 50 * E18, "DANGER", behavioral=False,
                               q_factor=0.9)
        self.assertIsNone(rug)                                       # кит без поведенческих сигналов — не rug

    def test_suspect_whale_is_danger_with_tax(self):
        self.sig[self.holders[0][0]]["kind"] = "transfer"            # улика: вход переводом
        sc = self.score(50 * E18, 0.9)
        self.assertEqual(sc["band"], "DANGER")
        self.assertTrue(any(g.startswith("biggest operator could move price") for g in sc["gates"]), sc["gates"])

    def test_tax_can_move_impact_below_gate(self):
        """Порог тонкого пула — по impact после налога: на кривой (q_factor 1) правило есть, после налога 3% — нет."""
        r = 118 * E18
        self.assertTrue(any("thin liquidity" in g for g in self.score(r, 1)["gates"]))
        self.assertFalse(any("thin liquidity" in g for g in self.score(r, 0.97)["gates"]))


class TestNewMainFeaturesOnFlap(unittest.TestCase):
    """Кэш переводов, порядок чтения кошельков и фон (BACKGROUND_RPS) — для сканов Flap."""

    def test_biggest_wallets_first_window(self):
        widths = []
        real = engine._RankGate
        c = Chain()
        with c.patched(), mock.patch.object(engine, "_RankGate",
                                            side_effect=lambda w, s=None: widths.append(w) or real(w, s)):
            engine.scan(FTOKEN)
        self.assertEqual(flap.HISTORY_PARALLEL, ch.HISTORY_PARALLEL)
        self.assertEqual(widths, [ch.HISTORY_PARALLEL])

    def test_transfer_cache_not_used_and_same_result(self):
        keys = ("holders", "operators", "links", "score", "band", "gates", "parts", "rug", "flap")
        out = {}
        for on in (False, True):
            ch.tcache_clear()
            c = long_chain()
            with c.patched(), windowed(30), mock.patch.object(ch, "TRANSFER_CACHE_ENABLED", on):
                r = engine.scan(FTOKEN)
            out[on] = {k: r[k] for k in keys}
            out[on]["flap"] = {k: v for k, v in r["flap"].items() if k != "history"}
            self.assertEqual(ch._TCACHE, {})                         # Flap кэш не пишет и не читает
        self.assertEqual(out[True], out[False])

    def test_background_scan_requests_are_background(self):
        """Фоновый скан Flap (окнами, с параллельными выборками): все запросы в сеть — фоновые
        (уступают живым сканам и идут под BACKGROUND_RPS), бюджет растянут как у Pons."""
        c = long_chain()
        flags = []
        rpc = c.rpc
        c.rpc = lambda m, p: flags.append(priority.is_background()) or rpc(m, p)
        with mock.patch.dict(os.environ, {"BACKGROUND_RPS": "2"}), c.patched(), windowed(30):
            th = threading.Thread(target=lambda: engine.scan(FTOKEN), name=f"{priority.BG}-alerts")
            th.start(); th.join(30)
        self.assertTrue(flags and all(flags), flags)


if __name__ == "__main__":
    unittest.main()


class TestF2Header(unittest.TestCase):
    """Шаг F2: шапка токена Flap на кривой — имя и тикер из контракта, цена Portal × ETH/USD, капа, ликвидность."""

    def scan(self, eth=2000.0, gt=None, **kw):
        self.c = Chain(**kw)
        with self.c.patched() as st:
            self.eth = st.enter_context(mock.patch("market.native_usd", return_value=eth))
            if gt is not None:
                st.enter_context(mock.patch("market.fetch_market", return_value=gt))
            return engine.scan(FTOKEN)

    def test_curve_price_from_portal(self):
        gt = {"name": "GT name", "ticker": "GT", "price_usd": None, "liquidity_usd": 0.0, "vol24h_usd": 0.0,
              "source": "gt"}
        r = self.scan(gt=gt)
        h = r["header"]
        self.assertEqual((h["name"], h["ticker"]), ("Synthetic", "SYN"))      # из контракта, не из GT
        self.assertAlmostEqual(h["price_usd"], 5 / E18 * 2000)                 # price Portal (wei за токен) × ETH/USD
        self.assertAlmostEqual(h["mcap_usd"], 5 / E18 * 2000 * 1e9)            # × весь сапплай
        self.assertAlmostEqual(h["liquidity_usd"], 3 * 2000)                   # ETH в кривой × ETH/USD
        self.assertIsNone(h["vol24h_usd"])                                     # объём кривой GT не знает
        self.assertEqual(r["flap"]["price_eth"], 5 / E18)
        self.eth.assert_called_once()

    def test_curve_without_eth_price(self):
        h = self.scan(eth=None)["header"]
        self.assertIsNone(h["price_usd"])
        self.assertIsNone(h["mcap_usd"])
        self.assertEqual(h["ticker"], "SYN")

    def test_dex_header_from_market_as_before(self):
        gt = {"name": "Tas", "ticker": "TAS", "price_usd": 0.001, "mcap_usd": 1e6, "liquidity_usd": 5e4,
              "vol24h_usd": 1e5, "source": "gt"}
        r = self.scan(gt=gt, status=flap.STATUS_DEX, buy_tax=200, sell_tax=200, circ=flap.SUPPLY)
        self.assertEqual({k: r["header"][k] for k in ("name", "ticker", "price_usd", "mcap_usd", "vol24h_usd")},
                         {"name": "Tas", "ticker": "TAS", "price_usd": 0.001, "mcap_usd": 1e6, "vol24h_usd": 1e5})
        self.assertIsNone(r["flap"]["price_eth"])
        self.eth.assert_not_called()                                           # после выпуска ETH/USD не нужен


class TestF2Early(unittest.TestCase):
    """Шаг F2: early buyers Flap — покупки через Portal (кривая) и пару V2 (после выпуска), инфраструктура исключена."""

    def run_early(self, n=20, **kw):
        self.c = Chain(**kw)
        with self.c.patched():
            data = flap.early_buyers(FTOKEN, n)
            status = flap.early_status(FTOKEN, data["buyers"], launch=data["launch"])
        return data, status

    def test_curve_buyers(self):
        data, _ = self.run_early()
        ws = [b["wallet"] for b in data["buyers"]]
        self.assertEqual(ws, [DEV] + W[:11])                 # дев первым; W[11] получил переводом — не покупатель
        self.assertNotIn(EXEC, ws)                            # исполнитель агрегатора — инфраструктура
        self.assertEqual(data["launch"]["deployer"], DEV)
        self.assertEqual(data["buyers"][0]["bought"], 100_000_000 * E18)

    def test_dex_buyers_and_sells(self):
        data, status = self.run_early(status=flap.STATUS_DEX, buy_tax=300, sell_tax=300, circ=flap.SUPPLY)
        ws = [b["wallet"] for b in data["buyers"]]
        for i in (2, 5, 8):                                   # купили свопом в паре V2
            self.assertIn(W[i], ws)
        for infra in (FTOKEN, TAXP, PAIR, flap.PORTAL):       # налоговые ноги и рынок — не покупатели
            self.assertNotIn(infra, ws)
        s = status["wallets"][W[2]]
        self.assertEqual(s["sold"], 10_000_000 * E18)         # продажа: налог на токен + пара — рынок
        self.assertEqual(s["moved"], {})

    def test_sell_through_unknown_contract_by_receipt(self):
        self.c = Chain(status=flap.STATUS_DEX, buy_tax=0, sell_tax=0, circ=flap.SUPPLY)
        bot = "0x" + "b0" * 20
        self.c.tr(W[3], bot, 1_000_000 * E18, "0xbotsell")
        self.c.tr(bot, PAIR, 1_000_000 * E18, "0xbotsell")
        self.c.rcs["0xbotsell"] = [tlog(W[3], bot, 1_000_000 * E18, "0xbotsell"),
                                   tlog(bot, PAIR, 1_000_000 * E18, "0xbotsell")]
        self.c.tr(W[4], W[11], 2_000_000 * E18, "0xmove")
        self.c.rcs["0xmove"] = [tlog(W[4], W[11], 2_000_000 * E18, "0xmove")]
        with self.c.patched():
            data = flap.early_buyers(FTOKEN, 20)
            st = flap.early_status(FTOKEN, data["buyers"], launch=data["launch"])["wallets"]
        self.assertEqual((st[W[3]]["sold"], st[W[3]]["moved"]), (1_000_000 * E18, {}))
        self.assertEqual(st[W[4]]["moved"], {W[11]: 2_000_000 * E18})

    def test_not_flap(self):
        c = Chain()
        with c.patched():
            self.assertIsNone(flap.early_buyers(NOTFLAP, 20))

    def test_api_early_routes_flap_and_keeps_pons(self):
        import early
        c = Chain()
        with c.patched():
            early.clear_cache()
            with mock.patch.object(ch, "early_buyers", side_effect=AssertionError("Pons path")):
                out = early.get(FTOKEN)
            self.assertTrue(out["available"])
            self.assertEqual(out["buyers"][0]["wallet"], DEV)
            self.assertTrue(out["buyers"][0]["dev"])
            self.assertFalse(early.background("robinhood", FTOKEN))   # у Flap нет истории скана — расчёт живой
            early.clear_cache()
            out = early.get(NOTFLAP)                                    # суффикс есть, Portal не знает — путь Pons
            self.assertEqual(out["reason"], "not a Pons V2 or Flap token")
        early.clear_cache()
        with Chain().patched(enabled=False):
            self.assertEqual(early.get(FTOKEN)["reason"], "not a Pons V2 token")   # выключено — как раньше


class TestF2Config(unittest.TestCase):

    def test_config_has_flap_flag(self):
        import json, server
        from http.server import ThreadingHTTPServer
        import urllib.request
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            for v, want in (("1", True), ("0", False)):
                with mock.patch.dict(os.environ, {"FLAP_ENABLED": v}):
                    with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/api/config") as r:
                        self.assertIs(json.loads(r.read())["flap"], want)
        finally:
            srv.shutdown()
            srv.server_close()


class TestF2Frontend(unittest.TestCase):
    """Собранный index.html: правки Flap есть и все — под флагом с сервера (flapOn из /api/config)."""

    def test_built_page(self):
        import json, re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "index.html"), encoding="utf-8").read()
        a = re.search(r'<script type="__bundler/template">', src).end()
        t = json.loads(src[a:src.find("</script>", a)])
        self.assertIn("flapOn:!!d.flap", t)                                     # флаг — из /api/config
        self.assertIn("state={flapOn:false,", t)                                # по умолчанию выключено
        self.assertIn("chart appears after the token graduates", t)
        self.assertIn("'https://flap.sh/robinhood/'", t)
        self.assertIn("flap:!!this.state.flapOn&&x.launchpad==='flap'", t)      # лента: бейдж только при флаге
        self.assertIn("this.state.flapOn&&this.state.view==='scan'", t)         # шапка: только при флаге
        for m in re.finditer(r"\(Pons, Flap\)|Flap launchpad</span>", t):       # тексты с площадками — под sc-if
            self.assertIn('<sc-if value="{{flapOn2}}"', t[max(0, m.start() - 600):m.start()])
