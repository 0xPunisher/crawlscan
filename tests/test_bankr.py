"""Тесты лаунчпада Bankr (chains/bankr.py + engine + detect): без сети, подставные ответы RPC.

Выключатель BANKR_ENABLED: выключен — всё как раньше (токен Bankr отклоняется, Pons и Flap без изменений);
включён — суффикс ba3 без RPC, затем один eth_call Airlock; порядок Flap → Bankr → Pons; запуск и PoolKey из чека,
исключения, покупки через хук (антиснайп) — покупки, вестинг дева — его доля, не перевод и не в q для impact;
impact и probably rug — по котировкам V4Quoter, не по балансу PoolManager; поля результата."""
import os, unittest
from unittest import mock

import fakes
from fakes import ch, engine, ZERO
from chains import bankr, flap
import detect as d
import early
import test_flap

BTOKEN = "0x" + "b1" * 18 + "0ba3"          # токен Bankr (суффикс ba3)
NOTBANKR = "0x" + "e2" * 18 + "0ba3"        # тот же суффикс, но интегратор не Bankr (Airlock отдаёт нули)
DEV = "0x" + "de" * 20
RELAYER = "0x" + "4e" * 20                  # tx.from запуска (не дев)
FEE1, FEE2 = "0x042455f9990098e11592be1fbd72e6dc68419b13", "0x5f8da8f88ec81e27f2e22fcb9ca5d926c595e508"
ROUTER = "0x6aa80dbbed9ae5ab45fbf61f9644fada3b29326e"
BOT_FEE = "0x" + "fe" * 20                  # получатель комиссии роутера (бот)
STOCK = "0x" + "5c" * 20                    # пара — токенизированная акция
WETH = ch.WETH_ADDR
PM = ch.V4_POOL_MGR
POOL_ID = "0x" + "9d" * 32
LAUNCH_BLOCK = fakes.LAUNCH_BLOCK
E18 = 10 ** 18
SUPPLY = 100_000_000_000 * E18
VEST = SUPPLY * 15 // 100
W = [fakes.wallet(i) for i in range(12)]
word, data = test_flap.word, test_flap.data


def topic(a):
    return ch.topic_for(a)


def lg(address, topics, *xs, tx="0xlaunch"):
    return {"address": address, "topics": topics, "data": data(*xs), "transactionHash": tx, "blockNumber": "0x0",
            "logIndex": "0x0"}


def swap(sender, eth, tokens, tx):
    """V4 Swap пула (токен — currency1): сторона свопающего, токен на выход положительный."""
    return lg(PM, [ch.V4_SWAP_TOPIC, POOL_ID, topic(sender)], (-eth) % (1 << 256), tokens, 1, 1, 0, 7000, tx=tx)


def decode_try_aggregate_call(calldata):
    """calldata Multicall3.tryAggregate(bool, (address,bytes)[]) -> [(target, data)]."""
    h = calldata[10:]
    w = lambda i: int(h[64 * i:64 * i + 64], 16)
    n, out = w(2), []
    for k in range(n):
        el = 3 + w(3 + k) // 32
        ln = w(el + 2)
        out.append(("0x" + h[64 * el + 24:64 * el + 64], "0x" + h[64 * (el + 3):64 * (el + 3) + 2 * ln]))
    return out


def encode_try_result(rets):
    """Ответ tryAggregate: (bool success, bytes returnData)[]; None — неудачный вызов."""
    els = []
    for r in rets:
        b = bytes.fromhex(r[2:]) if r else b""
        els.append(word(1 if r else 0) + word(64) + word(len(b)) + (b + b"\0" * (-len(b) % 32)).hex())
    offs, pos = [], 32 * len(els)
    for e in els:
        offs.append(pos); pos += len(e) // 2
    return "0x" + word(32) + word(len(els)) + "".join(word(o) for o in offs) + "".join(els)


class Chain:
    """Подставная сеть токена Bankr: Airlock, чек запуска, переводы, свопы, котировки V4Quoter (пул x*y=k с
    виртуальным резервом токена X, меньше баланса PoolManager — как у свежей мультикривой)."""

    def __init__(self, release=5 * SUPPLY // 100, available=0, pair=WETH, virtual=2 * SUPPLY // 100, quotes_ok=True,
                 vest=True, creator=RELAYER):
        self.release, self.available, self.pair, self.X, self.quotes_ok = release, available, pair, virtual, quotes_ok
        self.vest, self.creator = vest, creator
        self.calls, self.trs, self.rcs = [], [], {}
        self._build()
        self.head = max(t["block"] for t in self.trs) + 10

    def tr(self, frm, to, amount, tx, block=None):
        self.trs.append({"frm": frm, "to": to, "amount": amount, "tx": tx,
                         "block": block if block is not None else LAUNCH_BLOCK + 40 + len(self.trs),
                         "log_index": len(self.trs)})

    def _build(self):
        pool = SUPPLY - (VEST if self.vest else 0)
        if self.vest:
            self.tr(ZERO, BTOKEN, VEST, "0xlaunch", LAUNCH_BLOCK)
        self.tr(ZERO, bankr.AIRLOCK, pool, "0xlaunch", LAUNCH_BLOCK)
        self.tr(bankr.AIRLOCK, bankr.INITIALIZER, pool, "0xlaunch", LAUNCH_BLOCK)
        self.tr(bankr.INITIALIZER, PM, pool, "0xlaunch", LAUNCH_BLOCK)
        c0, c1 = sorted([self.pair, BTOKEN])
        self.rcs["0xlaunch"] = [
            lg(PM, [bankr.INITIALIZE, POOL_ID, topic(c0), topic(c1)], 0x800000, 200, bankr.INITIALIZER, 1, 239000),
            lg(bankr.INITIALIZER, [bankr.BENEFICIARIES, topic(BTOKEN)], 32, 2, bankr.PROTOCOL, 5 * 10 ** 16, DEV,
               95 * 10 ** 16),
            lg(bankr.HOOK, [bankr.HOOK_BENEFICIARIES, POOL_ID], 32, 2, FEE1, E18 // 3, FEE2, E18 - E18 // 3)]
        if self.vest:
            self.rcs["0xlaunch"].append(lg(BTOKEN, [bankr.VESTING, topic(DEV), topic(ZERO)], VEST))
        for i, w in enumerate(W[:11]):
            gross = (3 - i * 0.1) * SUPPLY / 100
            gross = int(gross)
            tx = f"0xbuy{i:02d}"
            blk = LAUNCH_BLOCK + 5 + i if i < 3 else None          # первые трое — в первые секунды (антиснайп 70%)
            fee = gross * 70 // 100 if i < 3 else gross // 100
            logs = [swap(ROUTER if i % 2 else w, E18 // 10, gross, tx),
                    swap(bankr.HOOK, E18 // 1000, 0, tx)]           # собственный своп хука (комиссия) — не покупка
            self.tr(PM, bankr.HOOK, fee, tx, blk)
            if i % 2:                                              # через роутер бота: комиссия боту
                self.tr(PM, ROUTER, gross - fee, tx, blk)
                self.tr(ROUTER, BOT_FEE, (gross - fee) // 20_000, tx, blk)   # комиссия бота — пыль
                self.tr(ROUTER, w, gross - fee - (gross - fee) // 20_000, tx, blk)
            else:
                self.tr(PM, w, gross - fee, tx, blk)
            self.tr(bankr.HOOK, PM, fee, tx, blk)                  # комиссия обратно в LP
            self.rcs[tx] = logs
        self.tr(W[0], W[11], SUPPLY // 200, "0xgift")              # настоящий перевод
        self.rcs["0xgift"] = []
        if self.vest and self.release:                              # дев забрал часть вестинга
            self.tr(BTOKEN, DEV, self.release, "0xrelease", LAUNCH_BLOCK + 8)
            self.rcs["0xrelease"] = []
        self.tr(W[2], PM, SUPPLY // 1000, "0xsell")                # продажа
        self.rcs["0xsell"] = []

    def balance(self, a):
        return sum(t["amount"] * ((t["to"] == a) - (t["frm"] == a)) for t in self.trs)

    def out(self, q):
        """Котировка продажи q токенов: x*y=k с виртуальным резервом X и 1000 ETH."""
        return 1000 * E18 * q // (self.X + q)

    def rpc_batch(self, calls):
        self.calls.append(("batch", calls))
        res = []
        for m, p in calls:
            if m == "eth_getTransactionReceipt":
                res.append({"logs": self.rcs.get(p[0], [])})
            elif m == "eth_getTransactionByHash":
                res.append({"from": self.creator})
            elif m == "eth_call" and p[0]["to"] == flap.MULTICALL and p[0]["data"].startswith(bankr.SEL_TRY_AGGREGATE):
                rets = []
                for target, inner in decode_try_aggregate_call(p[0]["data"]):
                    assert target == bankr.V4_QUOTER and inner.startswith(bankr.SEL_QUOTE)
                    q = int(inner[10 + 64 * 7:10 + 64 * 8], 16)
                    rets.append(data(self.out(q), 100_000) if self.quotes_ok else None)
                res.append(encode_try_result(rets))
            elif m == "eth_call" and p[0]["data"].startswith(bankr.SEL_AVAILABLE):
                res.append(data(self.available))
            elif m == "eth_call" and p[0]["to"] == flap.MULTICALL:
                res.append(test_flap.encode_aggregate_result(
                    [data(self.balance("0x" + dd[-40:])) for _, dd in test_flap.decode_aggregate_call(p[0]["data"])]))
            else:
                res.append(None)
        return res

    def logs(self, flt):
        lo, hi = int(flt["fromBlock"], 16), (self.head if flt["toBlock"] == "latest" else int(flt["toBlock"], 16))
        tp = flt.get("topics") or []
        ok = lambda i, a: len(tp) <= i or tp[i] is None or topic(a) in (tp[i] if isinstance(tp[i], list) else [tp[i]])
        return [test_flap.tlog(t["frm"], t["to"], t["amount"], t["tx"], token=BTOKEN, i=t["log_index"], block=t["block"])
                for t in sorted(self.trs, key=lambda t: (t["block"], t["log_index"]))
                if lo <= t["block"] <= hi and ok(1, t["frm"]) and ok(2, t["to"])]

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_call":
            assert params[0]["to"] == bankr.AIRLOCK and params[0]["data"].startswith(bankr.SEL_ASSET_DATA)
            if params[0]["data"][-40:] != BTOKEN[2:]:
                return data(*[0] * 10)
            return data(self.pair, 0, 0xdead, bankr.MIGRATOR, bankr.INITIALIZER, BTOKEN, 0xdead,
                        SUPPLY - (VEST if self.vest else 0), SUPPLY, bankr.INTEGRATOR)
        assert method == "eth_getLogs" and params[0]["address"] == BTOKEN, (method, params)
        return self.logs(params[0])

    def get_token_transfers(self, token, from_block, to_block=None, frm=None, to=None):
        lst = lambda a: None if a is None else ({a} if isinstance(a, str) else set(a))
        f, t_ = lst(frm), lst(to)
        return [t for t in sorted(self.trs, key=lambda t: (t["block"], t["log_index"]))
                if t["block"] >= from_block and (to_block is None or t["block"] <= to_block)
                and (f is None or t["frm"] in f) and (t_ is None or t["to"] in t_)]

    def patched(self, enabled=True, flap_on=False):
        st = fakes.patched()
        p = lambda name, **kw: st.enter_context(mock.patch.object(ch, name, **kw))
        p("rpc_batch", side_effect=self.rpc_batch)
        p("rpc", side_effect=self.rpc)
        p("receipt_logs", side_effect=lambda txs, chunk=50: {h: self.rcs.get(h, []) for h in txs})
        p("token_supply", side_effect=lambda t: SUPPLY if t == BTOKEN else fakes.SUPPLY)
        p("get_token_transfers", side_effect=lambda t, b, *a, **k: self.get_token_transfers(t, b, *a, **k)
          if t == BTOKEN else fakes.transfers())
        p("token_meta", side_effect=lambda t: {"name": "Stock", "symbol": "STK"} if t == STOCK else
          {"name": "Synthetic", "symbol": "SYN"})
        st.enter_context(mock.patch.dict(os.environ, {"BANKR_ENABLED": "1" if enabled else "0",
                                                      "FLAP_ENABLED": "1" if flap_on else "0"}))
        st.enter_context(mock.patch.dict(bankr._STATE, clear=True))
        st.enter_context(mock.patch.dict(bankr._LAUNCH, clear=True))
        return st


def scan(**kw):
    c = Chain(**kw)
    with c.patched():
        r = engine.scan(BTOKEN)
    return c, r


class TestSwitch(unittest.TestCase):

    def test_candidate_by_suffix_only_when_enabled(self):
        with mock.patch.dict(os.environ, {"BANKR_ENABLED": "1"}):
            self.assertTrue(bankr.candidate(BTOKEN) and bankr.candidate(BTOKEN.upper().replace("0X", "0x")))
            self.assertFalse(bankr.candidate("0x" + "a" * 37 + "ba4") or bankr.candidate(fakes.TOKEN))
        for v in ("", "0", "false", "no"):
            with mock.patch.dict(os.environ, {"BANKR_ENABLED": v}):
                self.assertFalse(bankr.candidate(BTOKEN))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(bankr.enabled())

    def test_disabled_bankr_token_rejected_as_before(self):
        c = Chain()
        with c.patched(enabled=False):
            with self.assertRaises(engine.ScanError) as cm:
                engine.scan(BTOKEN)
        self.assertEqual(str(cm.exception), "not a Pons V2 token")
        self.assertEqual(c.calls, [])                       # ни одного запроса Bankr

    def test_suffix_but_not_bankr_goes_pons_way_after_one_call(self):
        c = Chain()
        with c.patched():
            with self.assertRaises(engine.ScanError) as cm:
                engine.scan(NOTBANKR)
        self.assertEqual(str(cm.exception), "not a Pons V2 or Bankr token")
        self.assertEqual([m for m, _ in c.calls], ["eth_call"])   # одна проверка Airlock

    def test_error_texts(self):
        cases = {(False, False): "not a Pons V2 token", (True, False): "not a Pons V2 or Flap token",
                 (False, True): "not a Pons V2 or Bankr token", (True, True): "not a Pons V2, Flap or Bankr token"}
        for (fl, bk), text in cases.items():
            with mock.patch.dict(os.environ, {"FLAP_ENABLED": "1" if fl else "0", "BANKR_ENABLED": "1" if bk else "0"}):
                self.assertEqual(engine.not_launchpad("robinhood"), text)
        self.assertEqual(engine.not_launchpad("solana"), "not a pump.fun token")

    def test_pons_unchanged_and_no_extra_requests(self):
        keys = ("score", "band", "parts", "gates", "metrics", "headline", "reason", "operators", "links", "rug",
                "reserve", "reserve_ok", "holders", "launch")
        res = {}
        for on in (False, True):
            c = Chain()
            with c.patched(enabled=on, flap_on=on):
                r = engine.scan(fakes.TOKEN)
            self.assertEqual(c.calls, [])                    # Pons без суффикса ba3 — ни одного запроса Bankr
            self.assertNotIn("launchpad", r)
            self.assertNotIn("bankr", r)
            res[on] = {k: r[k] for k in keys}
        self.assertEqual(res[False], res[True])

    def test_pons_with_ba3_suffix_one_extra_call_same_result(self):
        pons_ba3 = "0x" + "ab" * 18 + "0ba3"
        res = {}
        for on in (False, True):
            c = Chain()
            with c.patched(enabled=on), mock.patch.object(
                    ch, "get_launch", side_effect=lambda t: {"block": LAUNCH_BLOCK, "curve": fakes.CURVE,
                                                             "deployer": fakes.DEPLOYER, "tx": "0xlaunch"}):
                r = engine.scan(pons_ba3)
            self.assertEqual(len(c.calls), 1 if on else 0)
            res[on] = {k: r[k] for k in ("score", "band", "metrics", "headline", "holders")}
        self.assertEqual(res[False], res[True])

    def test_flap_unchanged_and_no_extra_requests(self):
        keys = ("score", "band", "parts", "gates", "metrics", "headline", "operators", "links", "rug", "reserve",
                "flap", "launchpad")
        res = {}
        for on in (False, True):
            c = test_flap.Chain()
            with c.patched(), mock.patch.dict(os.environ, {"BANKR_ENABLED": "1" if on else "0"}):
                r = engine.scan(test_flap.FTOKEN)
            res[on] = ({k: r[k] for k in keys}, len(c.batches), c.getlogs)
        self.assertEqual(res[False], res[True])
        self.assertEqual(res[True][0]["launchpad"], "flap")


class TestScan(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.c, cls.r = scan()
        cls.sig = {h["wallet"]: h["signals"] for h in cls.r["holders"]}

    def test_result_fields(self):
        r, b = self.r, self.r["bankr"]
        self.assertEqual(r["launchpad"], "bankr")
        self.assertEqual(b["pool_id"], POOL_ID)
        self.assertEqual(b["pool_key"], {"currency0": WETH, "currency1": BTOKEN, "fee": 0x800000, "tick_spacing": 200,
                                         "hooks": bankr.INITIALIZER})
        self.assertEqual(b["pair"], {"kind": "eth", "address": WETH, "symbol": "ETH"})
        self.assertEqual(b["impact_source"], "v4_quoter")
        self.assertEqual((b["dev"], b["creator"]), (DEV, RELAYER))
        self.assertEqual(set(b["fee_recipients"]), {bankr.PROTOCOL, FEE1, FEE2})

    def test_launch_and_dev(self):
        self.assertEqual(self.r["launch"]["block"], LAUNCH_BLOCK)
        self.assertEqual(self.r["launch"]["deployer"], DEV)          # получатель 95% комиссий, а не tx.from

    def test_dev_falls_back_to_tx_from_without_fee_beneficiary(self):
        c = Chain(creator=DEV)
        c.rcs["0xlaunch"][1] = lg(bankr.INITIALIZER, [bankr.BENEFICIARIES, topic(BTOKEN)], 32, 1, bankr.PROTOCOL, E18)
        with c.patched():
            r = engine.scan(BTOKEN)
        self.assertEqual(r["launch"]["deployer"], DEV)

    def test_infra_not_holders(self):
        hs = {h["wallet"] for h in self.r["holders"]}
        for a in (PM, bankr.INITIALIZER, bankr.HOOK, BTOKEN, bankr.AIRLOCK, ROUTER, bankr.PROTOCOL, FEE1, FEE2):
            self.assertNotIn(a, hs)

    def test_vesting_attributed_to_dev(self):
        v = self.r["bankr"]["vesting"]
        rel = 5 * SUPPLY // 100
        self.assertEqual((v["total"], v["released"], v["in_contract"]), (VEST, rel, VEST - rel))
        self.assertEqual((v["unlocked"], v["locked"]), (rel, VEST - rel))
        self.assertAlmostEqual(v["total_share_supply"], 0.15)
        self.assertAlmostEqual(v["unlocked_share_supply"], 0.05)
        dev = next(h for h in self.r["holders"] if h["wallet"] == DEV)
        self.assertAlmostEqual(dev["share_supply"], 0.15)            # на кошельке 5% + в вестинге 10%
        self.assertEqual(self.sig[DEV]["kind"], "vesting")           # не «получено переводом»
        self.assertTrue(self.sig[DEV]["is_deployer"])
        self.assertTrue(self.r["operators"][0]["wallets"] == [DEV])  # дев — крупнейший оператор, доля видна

    def test_vesting_not_counted_as_transfer(self):
        share = {h["wallet"]: h["share"] for h in self.r["holders"]}
        self.assertAlmostEqual(self.r["metrics"]["transfer"], share[W[11]])   # только настоящий перевод
        self.assertEqual(self.sig[W[11]]["kind"], "transfer")

    def test_antisnipe_buys_are_buys_with_eth(self):
        for i in range(11):
            self.assertEqual(self.sig[W[i]]["kind"], "buy", i)
            self.assertEqual(self.sig[W[i]]["eth_in"], E18 // 10, i)   # валовая сторона свопа, не своп хука
        for i in range(3):
            self.assertTrue(self.sig[W[i]]["sniper"])
        self.assertFalse([l for l in self.r["links"] if l["kind"] == "distributor"])
        self.assertNotIn(BOT_FEE, {h["wallet"] for h in self.r["holders"]})   # пыль комиссии бота

    def test_sell_detected(self):
        self.assertTrue(self.sig[W[2]]["sold"])
        self.assertFalse(self.sig[W[3]]["sold"])

    def test_impact_from_quoter_not_pool_balance(self):
        m, r = self.r["metrics"], self.r
        circ = r["circulating"]
        dev = r["operators"][0]
        q = dev["weighted"] * circ - (VEST - 5 * SUPPLY // 100)      # невыкупаемый вестинг не в q
        want = d.dump_impact(q, self.c.X)                            # подставной Quoter = x*y=k с резервом X
        self.assertAlmostEqual(m["impact"], want, delta=0.03)
        self.assertGreater(m["impact"], d.dump_impact(q, r["reserve"]) + 0.2)   # по балансу PM — сильно меньше
        self.assertEqual(r["reserve"], self.c.balance(PM))
        self.assertTrue(r["reserve_ok"])

    def test_one_quote_http_and_cheap_launch(self):
        batches = [cl for m, cl in self.c.calls if m == "batch"]
        quotes = [b for b in batches if any(isinstance(p[0], dict) and p[0].get("to") == flap.MULTICALL and
                                             p[0]["data"].startswith(bankr.SEL_TRY_AGGREGATE) for _, p in b)]
        self.assertEqual(len(quotes), 1)                             # все котировки — один HTTP
        self.assertEqual(len(quotes[0]), 2)                          # + доступное в вестинге (один бенефициар)
        n = len(decode_try_aggregate_call(quotes[0][0][1][0]["data"]))
        self.assertEqual(n, 2 * (len(bankr.QUOTE_GRID) + 1))
        airlock = [p for m, p in self.c.calls if m == "eth_call"]
        self.assertEqual(len(airlock), 1)                            # определение — один eth_call за скан


class TestVestingAndImpact(unittest.TestCase):

    def test_unreleased_vesting_dev_without_wallet_transfers(self):
        c, r = scan(release=0)
        sig = {h["wallet"]: h["signals"] for h in r["holders"]}
        self.assertEqual(sig[DEV]["kind"], "vesting")
        self.assertEqual(r["bankr"]["vesting"]["released"], 0)
        dev = next(h for h in r["holders"] if h["wallet"] == DEV)
        self.assertAlmostEqual(dev["share_supply"], 0.15)
        # всё в вестинге и не разблокировано: дев не может продать — его impact 0, жёсткого правила нет
        self.assertEqual(r["operators"][0]["wallets"], [DEV])
        self.assertEqual(r["metrics"]["impact"], 0.0)
        self.assertFalse([g for g in r["gates"] if g.startswith("biggest operator")])

    def test_available_vesting_is_sellable(self):
        c, r = scan(release=0, available=VEST)
        self.assertEqual(r["bankr"]["vesting"]["unlocked"], VEST)
        self.assertEqual(r["bankr"]["vesting"]["locked"], 0)
        self.assertGreater(r["metrics"]["impact"], 0.5)

    def test_extra_tokens_on_token_contract_not_dev(self):
        c = Chain(release=0)
        c.tr(W[5], BTOKEN, SUPPLY // 1000, "0xstray")               # кто-то прислал токены на контракт токена
        c.rcs["0xstray"] = []
        with c.patched():
            r = engine.scan(BTOKEN)
        v = r["bankr"]["vesting"]
        self.assertEqual((v["in_contract"], v["locked"], v["released"]), (VEST, VEST, 0))
        dev = next(h for h in r["holders"] if h["wallet"] == DEV)
        self.assertAlmostEqual(dev["share_supply"], 0.15)

    def test_no_vesting(self):
        c, r = scan(vest=False)
        v = r["bankr"]["vesting"]
        self.assertEqual((v["total"], v["unlocked"], v["locked"]), (0, 0, 0))
        self.assertNotIn(DEV, {h["wallet"] for h in r["holders"]})

    def test_quotes_failed_liquidity_not_measured(self):
        c, r = scan(quotes_ok=False)
        self.assertFalse(r["reserve_ok"])
        self.assertIsNone(r["metrics"]["impact"])
        self.assertIsNone(r["bankr"]["impact_source"])
        self.assertIsNone(r["rug"])

    def test_stock_pair(self):
        c, r = scan(pair=STOCK)
        self.assertEqual(r["bankr"]["pair"], {"kind": "token", "address": STOCK, "symbol": "STK"})
        sig = {h["wallet"]: h["signals"] for h in r["holders"]}
        self.assertTrue(all(sig[W[i]]["eth_in"] is None and sig[W[i]]["kind"] == "buy" for i in range(3)))


class TestCurveImpact(unittest.TestCase):

    def test_interpolation(self):
        curve = [(10, 0.1), (20, 0.5), (40, 0.9)]
        self.assertEqual(d.curve_impact(0, curve), 0.0)
        self.assertAlmostEqual(d.curve_impact(5, curve), 0.05)
        self.assertAlmostEqual(d.curve_impact(15, curve), 0.3)
        self.assertAlmostEqual(d.curve_impact(100, curve), 0.9)

    def test_curve_from_quotes_matches_xyk(self):
        X = 10 ** 24
        qs = []
        for f in (bankr.QUOTE_REF,) + bankr.QUOTE_GRID:
            q = int(10 ** 25 * f)
            qs += [q, q * bankr.QUOTE_STEP // 100]
        raw = encode_try_result([data(1000 * E18 * q // (X + q), 1) for q in qs])
        for q, imp in bankr.impact_curve(qs, raw):
            self.assertAlmostEqual(imp, d.dump_impact(q, X), delta=0.02)

    def test_pons_score_identical_without_curve(self):
        holders = [("0x" + "11" * 20, 300, 0.3), ("0x" + "22" * 20, 200, 0.2)] + \
                  [("0x" + f"{i:02x}" * 20, 50, 0.05) for i in range(3, 13)]
        sig = {a: {"is_deployer": False, "unread": False, "virgin": False, "kind": "buy", "sniper": False,
                   "launch_bundle": False, "sold": False} for a, _, _ in holders}
        ops = [{"wallets": [a], "share": s, "share_supply": s, "level": None, "weighted": s} for a, _, s in holders]
        base = {"supply": 1000, "circulating": 1000, "holders_total": 12}
        self.assertEqual(d.score(holders, sig, ops, base, 500), d.score(holders, sig, ops, base, 500,
                                                                         impact_curve=None, locked=None))


class TestEarly(unittest.TestCase):

    def test_early_routes_to_bankr(self):
        c = Chain()
        with c.patched(), mock.patch.object(ch, "early_buyers", side_effect=AssertionError("Pons path")):
            early.clear_cache()
            out = early.get(BTOKEN)
        self.assertTrue(out["available"])
        self.assertNotIn(DEV, [b["wallet"] for b in out["buyers"]])   # вестинг дева — не покупка
        self.assertEqual(out["buyers"][0]["wallet"], W[0])

    def test_early_disabled_as_before(self):
        c = Chain()
        with c.patched(enabled=False):
            early.clear_cache()
            out = early.get(BTOKEN)
        self.assertEqual(out["reason"], "not a Pons V2 token")


if __name__ == "__main__":
    unittest.main()
