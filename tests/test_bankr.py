"""Тесты лаунчпада Bankr (chains/bankr.py + engine + detect): без сети, подставные ответы RPC.

Выключатель BANKR_ENABLED: выключен — всё как раньше (токен Bankr отклоняется, Pons и Flap без изменений);
включён — суффикс ba3 без RPC, затем один eth_call Airlock; порядок Flap → Bankr → Pons; запуск и PoolKey из чека,
исключения, покупки через хук (антиснайп) — покупки, вестинг дева — его доля, не перевод и не в q для impact;
impact и probably rug — по котировкам V4Quoter, не по балансу PoolManager; поля результата."""
import os, threading, time as _t, unittest
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
        self.page_cap = 10 ** 9                 # логов в ответе getLogs (тесты чтения ставят маленький)
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
        logs = self.logs(params[0])
        if len(logs) > self.page_cap:   # как Alchemy: отказ с подсказкой окна, где логов не больше cap
            lo = int(params[0]["fromBlock"], 16)
            b = max(lo, int(logs[self.page_cap]["blockNumber"], 16) - 1)
            raise RuntimeError(f"Log response size exceeded. ... this block range should work: [{hex(lo)}, {hex(b)}]")
        return logs

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
        st.enter_context(mock.patch.dict(bankr._INDEX, clear=True))
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


def until(cond, timeout=3.0):
    t0 = _t.time()
    while not cond() and _t.time() - t0 < timeout:
        _t.sleep(0.01)
    return cond()


def small_pages(cap=5):
    """Чтение маленькими страницами: потолок ответа RPC cap логов, участки первой волны — от 1 блока."""
    return mock.patch.multiple(bankr, READ_CHUNK_MIN=1, READ_CHUNKS=4, READ_TARGET=3), \
        mock.patch.object(flap, "PAGE_LOGS", cap)


class TestHistory(unittest.TestCase):

    def test_parallel_read_splits_by_suggestion_and_reads_all(self):
        c = Chain()
        c.page_cap = 5
        a, b = small_pages()
        with c.patched(), a, b:
            got = []
            n, done = bankr._read_all(BTOKEN, LAUNCH_BLOCK, c.head, got.extend)
        self.assertTrue(done)
        self.assertEqual(n, len(c.trs))
        self.assertEqual(sorted((t["tx"], t["log_index"]) for t in got), sorted((t["tx"], t["log_index"]) for t in c.trs))
        self.assertGreater(len([m for m, _ in c.calls if m == "eth_getLogs"]), 4)   # делили по подсказке

    def test_cap_and_deadline_stop(self):
        c = Chain()
        with c.patched():
            self.assertFalse(bankr._read_all(BTOKEN, LAUNCH_BLOCK, c.head, lambda p: None, cap=3)[1])
            self.assertFalse(bankr._read_all(BTOKEN, LAUNCH_BLOCK, c.head, lambda p: None, until=0)[1])

    def test_slow_first_wave_with_cap_gives_up(self):
        import time as _t
        c = Chain()
        slow = lambda m, p: (_t.sleep(0.3), c.rpc(m, p))[1]
        with c.patched(), mock.patch.object(ch, "rpc", side_effect=slow), mock.patch.object(bankr, "WAVE_MAX", 0.05):
            t0 = _t.time()
            n, done = bankr._read_all(BTOKEN, LAUNCH_BLOCK, c.head, lambda p: None, cap=10 ** 9)
        self.assertFalse(done)
        self.assertLess(_t.time() - t0, 0.25)

    def test_full_then_indexed_same_result(self):
        c = Chain()
        with c.patched():
            r1 = engine.scan(BTOKEN)
            self.assertEqual(r1["bankr"]["history"]["mode"], "full")
            self.assertIn(BTOKEN, bankr._INDEX)                      # индекс — из полной истории
            r2 = engine.scan(BTOKEN)
        h = r2["bankr"]["history"]
        self.assertEqual((h["mode"], h["top_exact"], h["tail_logs"]), ("indexed", True, 0))
        self.assertFalse(r2["limited"])
        for k in ("score", "band", "gates", "operators", "holders_total", "circulating"):
            self.assertEqual(r1[k], r2[k], k)
        self.assertEqual([(x["wallet"], x["share"], x["signals"]["kind"], x["signals"]["sold"]) for x in r1["holders"]],
                         [(x["wallet"], x["share"], x["signals"]["kind"], x["signals"]["sold"]) for x in r2["holders"]])

    def test_indexed_reads_tail(self):
        c = Chain()
        whale = "0x" + "77" * 20
        with c.patched():
            engine.scan(BTOKEN)
            c.tr(W[3], whale, c.balance(W[3]), "0xmove", c.head + 5)
            c.rcs["0xmove"] = []
            c.head += 20
            r = engine.scan(BTOKEN)
        self.assertEqual(r["bankr"]["history"]["tail_logs"], 1)
        hs = {x["wallet"]: x for x in r["holders"]}
        self.assertIn(whale, hs)
        self.assertNotIn(W[3], hs)
        self.assertEqual(hs[whale]["signals"]["kind"], "transfer")

    def test_big_history_windowed_and_index_started(self):
        c = Chain()
        started, gate = [], threading.Event()

        def build(t, b, ex, est=None):
            started.append((t, b, est))
            gate.wait(5)
            return False, "error"
        with c.patched(), mock.patch.object(bankr, "FULL_MAX_LOGS", 3), \
                mock.patch.object(bankr, "build_index", side_effect=build), \
                mock.patch.dict(bankr._INDEX_BUILDING, clear=True), mock.patch.dict(bankr._INDEX_FAILED, clear=True), \
                mock.patch.dict(bankr._INDEX_QUEUE, clear=True):
            r = engine.scan(BTOKEN)
            h = r["bankr"]["history"]
            self.assertIn(h["index"], ("queued", "building"))                   # поток очереди берёт сразу
            self.assertTrue(h["index_started"])
            self.assertGreater(h["index_eta_s"], 0)
            self.assertTrue(until(lambda: bankr.index_status(BTOKEN)["state"] == "building"))
            gate.set()
            for _ in range(100):
                if BTOKEN not in bankr._INDEX_BUILDING:
                    break
                _t.sleep(0.01)
            self.assertEqual(bankr.index_status(BTOKEN)["state"], "unavailable")   # сбой: не повторяем INDEX_RETRY_S
        self.assertEqual([x[:2] for x in started], [(BTOKEN, LAUNCH_BLOCK)])
        self.assertGreater(started[0][2], 3)                                     # оценка истории — больше потолка
        self.assertNotIn(BTOKEN, bankr._INDEX)

    def test_build_index_balances(self):
        c = Chain()
        c.page_cap = 5
        a, b = small_pages()
        with c.patched(), a, b:
            self.assertEqual(bankr.build_index(BTOKEN, LAUNCH_BLOCK, bankr._sets(BTOKEN, bankr.get_launch(BTOKEN))[1]), (True, None))
            idx = bankr._INDEX[BTOKEN]
        self.assertEqual(idx["head"], c.head)
        for w in W + [DEV, PM, BTOKEN]:
            self.assertEqual(idx["bal"].get(w, 0), c.balance(w), w)


class TestDevGate(unittest.TestCase):

    def setUp(self):
        self.dev = "0x" + "de" * 20
        self.holders = [(self.dev, 300, 0.3)] + [("0x" + f"{i:02x}" * 20, 50, 0.05) for i in range(1, 15)]
        self.sig = {a: {"is_deployer": a == self.dev, "unread": False, "virgin": False, "kind": "buy", "sniper": False,
                        "launch_bundle": False, "sold": False} for a, _, _ in self.holders}
        self.ops = [{"wallets": [a], "share": s, "share_supply": s, "level": None, "weighted": s} for a, _, s in self.holders]
        self.base = {"supply": 1000, "circulating": 1000, "holders_total": 15}

    def score(self, **kw):
        return d.score(self.holders, self.sig, self.ops, self.base, 200, **kw)

    def test_limited_without_dev_capped_risky(self):
        sc = self.score(limited=True)
        self.assertEqual(sc["band"], "RISKY")
        self.assertNotIn("dev_impact", sc["metrics"])

    def test_dev_gate_danger_even_when_limited(self):
        sc = self.score(limited=True, dev={"wallet": self.dev, "sellable": 300, "share_supply": 0.3})
        self.assertEqual(sc["band"], "DANGER")
        self.assertTrue(sc["gates"][0].startswith("deployer could move price −"))
        self.assertAlmostEqual(sc["metrics"]["dev_impact"], d.dump_impact(300, 200))

    def test_locked_vesting_not_sellable_no_gate(self):
        sc = self.score(limited=True, dev={"wallet": self.dev, "sellable": 10, "share_supply": 0.3})
        self.assertEqual(sc["band"], "RISKY")
        self.assertFalse([g for g in sc["gates"] if g.startswith("deployer")])

    def test_not_duplicated_with_biggest_operator_gate(self):
        sc = self.score(dev={"wallet": self.dev, "sellable": 300, "share_supply": 0.3})
        self.assertEqual(sc["band"], "DANGER")
        self.assertEqual(len([g for g in sc["gates"] if not g.startswith("soft:")]), 1)
        self.assertTrue(sc["gates"][0].startswith("biggest operator"))

    def test_engine_dev_gate_on_limited_bankr_scan(self):
        c = Chain(release=10 * SUPPLY // 100)
        orig = bankr.history
        part = lambda *a, **k: (lambda r: (r[0], r[1], dict(r[2], mode="windowed", top_exact=False), r[3]))(orig(*a, **k))
        with c.patched(), mock.patch.object(bankr, "history", side_effect=part):
            r = engine.scan(BTOKEN)
        self.assertTrue(r["limited"])
        self.assertEqual(r["band"], "DANGER")
        self.assertTrue(any(g.startswith("deployer could move price") for g in r["gates"]))
        dh = r["bankr"]["dev_holding"]
        self.assertEqual((dh["wallet"], dh["wallet_balance"], dh["sellable"]), (DEV, 10 * SUPPLY // 100, 10 * SUPPLY // 100))
        self.assertAlmostEqual(dh["share_supply"], 0.15)


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


class TestIndexStability(unittest.TestCase):
    """B2: индекс Bankr под правилами стабильности — LRU по записям, защита по памяти, фон, одно построение."""

    def setUp(self):
        self.st = mock.patch.multiple(bankr, memory_high=lambda: False)
        self.st.start()
        for dct in (bankr._INDEX, bankr._INDEX_BUILDING, bankr._INDEX_FAILED, bankr._INDEX_QUEUE):
            p = mock.patch.dict(dct, clear=True)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.st.stop)

    def ix(self, n):
        ix = bankr._index_new()
        ix["n"] = n
        return ix

    def build(self, c, **kw):
        a, b = small_pages()
        with c.patched(), a, b, mock.patch.multiple(bankr, **kw) if kw else mock.patch.multiple(bankr, EDGES_MAX=64):
            r = bankr.build_index(BTOKEN, LAUNCH_BLOCK, bankr._sets(BTOKEN, bankr.get_launch(BTOKEN))[1])
            self.after = dict(bankr._INDEX)    # c.patched() восстанавливает _INDEX на выходе
            return r

    def test_lru_by_entries_not_only_tokens(self):
        with mock.patch.multiple(bankr, INDEX_MAX_ENTRIES=100, INDEX_MAX=20):
            self.assertTrue(bankr._index_put("0xa", self.ix(40)))
            self.assertTrue(bankr._index_put("0xb", self.ix(40)))
            self.assertTrue(bankr._index_put("0xc", self.ix(40)))      # 120 > 100: вытеснен самый давний
            self.assertEqual(list(bankr._INDEX), ["0xb", "0xc"])
            self.assertFalse(bankr._index_put("0xd", self.ix(101)))    # больше лимита — не хранится
            self.assertNotIn("0xd", bankr._INDEX)

    def test_entries_counted_and_compact(self):
        ok, why = self.build(Chain())
        self.assertEqual((ok, why), (True, None))
        ix = self.after[BTOKEN]
        n = len(ix["bal"]) + len(ix["first"]) + len(ix["sold"]) + sum(len(e) for e in ix["edges"].values())
        self.assertEqual(ix["n"], n)
        self.assertTrue(all(isinstance(v, tuple) for v in ix["first"].values()))   # перевод — кортеж, не словарь

    def test_build_stops_when_too_large(self):
        self.assertEqual(self.build(Chain(), INDEX_MAX_ENTRIES=3), (False, "too_large"))
        self.assertNotIn(BTOKEN, self.after)

    def test_build_stops_near_memory_limit(self):
        with mock.patch.object(bankr, "memory_high", lambda: True):
            self.assertEqual(self.build(Chain()), (False, "memory"))
        self.assertNotIn(BTOKEN, self.after)

    def test_drop_caches_clears_and_aborts_running_build(self):
        c = Chain()
        orig = c.rpc

        def rpc(m, p):
            if m == "eth_getLogs":
                bankr._index_put("0xa", self.ix(1))
                bankr.drop_caches()        # защита по памяти сработала посреди построения
            return orig(m, p)
        with mock.patch.object(c, "rpc", side_effect=rpc):
            self.assertEqual(self.build(c), (False, "memory"))
        self.assertEqual(self.after, {})

    def test_memguard_drops_bankr_index(self):
        import server, memguard
        self.assertIn(bankr.drop_caches, memguard._droppers)
        bankr._index_put("0xa", self.ix(1))
        memguard.relieve()
        self.assertEqual(bankr._INDEX, {})

    def test_memguard_near(self):
        import memguard
        with mock.patch.object(memguard, "soft_limit_mb", return_value=1000):
            with mock.patch.object(memguard, "rss_mb", return_value=900):
                self.assertTrue(memguard.near())
            with mock.patch.object(memguard, "rss_mb", return_value=500):
                self.assertFalse(memguard.near())
            with mock.patch.object(memguard, "rss_mb", return_value=None):
                self.assertFalse(memguard.near())
        with mock.patch.object(memguard, "soft_limit_mb", return_value=0), \
                mock.patch.object(memguard, "rss_mb", return_value=10 ** 6):
            self.assertFalse(memguard.near())

    def test_memguard_near_from_container_limit(self):
        """memfix: мягкий порог — 75% лимита контейнера; построение индекса останавливается с 85% мягкого."""
        import memguard
        with mock.patch.dict(os.environ, {"MEMORY_SOFT_LIMIT_MB": ""}), \
                mock.patch.object(memguard, "cgroup_limit_mb", return_value=8000):
            self.assertEqual(memguard.soft_limit_mb(), 6000)
            with mock.patch.object(memguard, "rss_mb", return_value=5200):
                self.assertTrue(memguard.near())
                self.assertFalse(memguard.over(live=1, cap=3))           # живые сканы при этом пускаются
            with mock.patch.object(memguard, "rss_mb", return_value=5000):
                self.assertFalse(memguard.near())

    def test_build_from_critical_thread_is_background(self):
        """Построение, запущенное откуда угодно (даже из критичного потока), — фон: уступает живым сканам и
        не идёт по лимиту критичной работы; критичный запрос его не ждёт."""
        from chains import priority
        gate, seen = threading.Event(), []

        def build(t, b, ex, est=None):
            seen.append((priority.is_critical(), priority.is_background()))
            gate.wait(5)
            return True, None
        with mock.patch.object(bankr, "build_index", side_effect=build), priority.critical():
            self.assertEqual(bankr._index_start("0xa", 1, set()), "queued")
            self.assertTrue(until(lambda: seen))
            with mock.patch.object(ch._LIMIT, "wait"), mock.patch.object(ch._CRIT_LIMIT, "wait") as cw, \
                    mock.patch.object(ch._BG_LIMIT, "wait") as bgw, priority.live():
                t0 = _t.time()
                ch._rate_limit()                                             # награды во время построения
                self.assertLess(_t.time() - t0, 0.2)
                cw.assert_called_once()
                bgw.assert_not_called()
            gate.set()
            for _ in range(200):
                if not bankr._INDEX_BUILDING:
                    break
                _t.sleep(0.01)
        self.assertEqual(seen, [(False, True)])

    def test_queue(self):
        """Строится один, остальные — в очереди до INDEX_QUEUE_MAX по порядку, повтор не добавляется; переполнена —
        queue_full; позиция и ETA (остаток идущего + впереди + своё)."""
        from chains import priority
        gate, seen = threading.Event(), []

        def build(t, b, ex, est=None):
            seen.append((t, priority.is_background(), priority.is_strict()))
            gate.wait(5)
            return True, None
        with mock.patch.object(bankr, "build_index", side_effect=build):
            self.assertEqual(bankr._index_start("0xa", 1, set(), 10 ** 6), "queued")
            self.assertTrue(until(lambda: "0xa" in bankr._INDEX_BUILDING))
            self.assertEqual(bankr._index_start("0xa", 1, set()), "building")
            for t in ("0xb", "0xc", "0xd", "0xe", "0xf"):
                self.assertEqual(bankr._index_start(t, 1, set(), 10 ** 6), "queued")
            self.assertEqual(bankr._index_start("0xc", 1, set()), "queued")     # повтор — не добавляется
            self.assertEqual(list(bankr._INDEX_QUEUE), ["0xb", "0xc", "0xd", "0xe", "0xf"])
            self.assertEqual(bankr._index_start("0xg", 1, set()), "queue_full")
            a, b, c = (bankr.index_status(t) for t in ("0xa", "0xb", "0xc"))
            self.assertEqual((a["state"], b["state"], b["position"], c["position"]), ("building", "queued", 1, 2))
            self.assertGreater(a["eta_s"], 60)                                    # 1M логов под BACKGROUND_RPS — минуты
            self.assertGreater(c["eta_s"], b["eta_s"])
            self.assertGreaterEqual(b["eta_s"], a["eta_s"] + bankr.index_eta(10 ** 6) - 2)
            self.assertEqual(bankr.index_status("0xg")["state"], "queue_full")
            with mock.patch.object(bankr, "index_status", return_value=c):
                self.assertEqual(engine.bankr_partial("0xc", {})["message"],
                                 "Partial scan: full holder history queued: 2nd in line, check again in about "
                                 f"{-(-c['eta_s'] // 60)} minutes")
            with mock.patch.object(bankr, "index_status", return_value={"state": "queue_full", "eta_s": None}):
                self.assertEqual(engine.bankr_partial("0xg", {})["message"],
                                 "Partial scan: full history is queued, check again later")
            gate.set()
            self.assertTrue(until(lambda: not bankr._INDEX_QUEUE and not bankr._INDEX_BUILDING))
        self.assertEqual([x[0] for x in seen], ["0xa", "0xb", "0xc", "0xd", "0xe", "0xf"])   # по порядку
        self.assertTrue(all(bg and strict for _, bg, strict in seen))            # строгий фон

    def test_queue_waits_for_memory(self):
        """Память у порога: в очередь ставится, но новое построение не начинается, пока память не опустится."""
        seen, high = [], [True]
        with mock.patch.object(bankr, "build_index", side_effect=lambda *a: (seen.append(a[0]), (True, None))[1]), \
                mock.patch.object(bankr, "memory_high", lambda: high[0]), mock.patch.object(bankr, "MEMORY_WAIT_S", 0.05):
            self.assertEqual(bankr._index_start("0xm", 1, set()), "queued")
            _t.sleep(0.3)
            self.assertEqual((seen, bankr.index_status("0xm")["state"]), ([], "queued"))
            high[0] = False
            self.assertTrue(until(lambda: seen == ["0xm"]))

    def test_queue_waits_for_live_scan(self):
        """Живой скан идёт — построение из очереди не начинается до его конца."""
        from chains import priority
        seen = []
        with mock.patch.object(bankr, "build_index", side_effect=lambda *a: (seen.append(a[0]), (True, None))[1]):
            with priority.live():
                self.assertEqual(bankr._index_start("0xl", 1, set()), "queued")
                _t.sleep(0.3)
                self.assertEqual(seen, [])
                self.assertEqual(bankr.index_status("0xl")["state"], "building")   # впереди никого: не «1st in line»
            self.assertTrue(until(lambda: seen == ["0xl"]))

    def test_eta_by_active_time_and_live_estimate(self):
        """ETA: объём — оценка первой волны на ходу, скорость — по активному времени (паузы на живые сканы не в счёт)."""
        from chains import priority
        now = _t.time()
        job = {"started": now - 100, "eta_s": 10 ** 6, "est_total": 10 ** 7, "live0": 5.0,
               "stats": {"logs": 1000, "req": 60, "est_live": 2000}}
        with mock.patch.object(priority, "live_seconds", return_value=35.0):     # 30 с из 100 шли живые сканы
            left, _ = bankr._job_left(job, now)
        self.assertAlmostEqual(left, 70, delta=1)                                 # 1000 логов за 70 с активных
        with mock.patch.object(priority, "live_seconds", return_value=65.0):     # активных 40 с — ещё разгон: по оценке
            left, _ = bankr._job_left(job, now)
        self.assertAlmostEqual(left, bankr.index_eta(2000) * 0.5, delta=1)

    def test_start_limits(self):
        gate, seen = threading.Event(), []

        def build(t, b, ex, est=None):
            from chains import priority
            seen.append(priority.is_background())
            gate.wait(5)
            return True, None
        with mock.patch.object(bankr, "build_index", side_effect=build):
            self.assertEqual(bankr._index_start("0xa", 1, set(), 10 ** 6), "queued")
            self.assertTrue(until(lambda: seen))
            gate.set()
            self.assertTrue(until(lambda: not bankr._INDEX_BUILDING))
        self.assertEqual(seen, [True])                                       # поток построения — фоновый
        bankr._INDEX["0xa"] = self.ix(1)
        self.assertIsNone(bankr._index_start("0xa", 1, set()))              # уже есть
        bankr._INDEX_FAILED["0xd"] = (_t.time() + 60, "too_large")
        self.assertIsNone(bankr._index_start("0xd", 1, set()))
        self.assertEqual(bankr.index_status("0xd")["state"], "too_large")

    def test_background_read_uses_fewer_workers(self):
        from chains import priority
        sizes = []
        real = bankr.ThreadPoolExecutor

        def pool(max_workers, thread_name_prefix):
            sizes.append((max_workers, thread_name_prefix.startswith(priority.BG)))
            return real(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        c = Chain()
        with c.patched(), mock.patch.object(bankr, "ThreadPoolExecutor", side_effect=pool):
            bankr._read_all(BTOKEN, LAUNCH_BLOCK, c.head, lambda p: None)
            th = threading.Thread(target=lambda: bankr._read_all(BTOKEN, LAUNCH_BLOCK, c.head, lambda p: None),
                                  name=priority.BG + "-test")
            th.start()
            th.join()
        self.assertEqual(sizes, [(bankr.READ_WORKERS, False), (bankr.READ_WORKERS_BG, True)])

    def test_strict_read_pauses_for_live_scan(self):
        """Строгий фон: пока идёт живой скан, ни одного нового запроса; ответ, пришедший во время скана, разбирается
        после него; страницы короткие (участки по оценке est), и всё дочитано."""
        from chains import priority
        c = Chain()
        c.page_cap = 5
        log, live_on, first = [], threading.Event(), threading.Event()
        orig = c.rpc

        def rpc(m, p):
            ch._rate_limit()                 # как ch._post перед каждой попыткой
            if m == "eth_getLogs":
                log.append(("req", live_on.is_set()))
                if not first.is_set():
                    first.set()
                    _t.sleep(0.2)            # первый запрос в полёте, когда начинается живой скан
            return orig(m, p)
        got = []

        def sink(page):
            log.append(("page", live_on.is_set()))
            got.extend(page)

        stats = {}

        def read():
            with priority.background(strict=True):
                bankr._read_all(BTOKEN, LAUNCH_BLOCK, c.head, sink, est=len(c.trs), stats=stats)
        a, b = small_pages()
        with c.patched(), a, b, mock.patch.multiple(bankr, READ_TARGET_BG=3, READ_CHUNK_MIN_BG=1), \
                mock.patch.object(ch, "rpc", side_effect=rpc), mock.patch.object(ch._BG_LIMIT, "wait"), \
                mock.patch.object(ch._LIMIT, "wait"):
            th = threading.Thread(target=read, name=priority.BG + "-bankr-index")
            th.start()
            first.wait(2)
            with priority.live():
                live_on.set()
                _t.sleep(0.5)
                live_on.clear()
            th.join(5)
        self.assertEqual(sorted((t["tx"], t["log_index"]) for t in got), sorted((t["tx"], t["log_index"]) for t in c.trs))
        self.assertNotIn(("req", True), log[1:])                                # во время скана новых запросов нет
        self.assertNotIn(("page", True), log)                                   # и разбора тоже
        self.assertGreater(sum(1 for k, _ in log if k == "req"), len(c.trs) // 3)   # короткие участки сразу
        self.assertEqual((stats["logs"], stats["req"]), (len(c.trs), sum(1 for k, _ in log if k == "req")))
        self.assertGreater(stats["est_live"], 0)                                # объём по ответившим участкам


class TestPartialScan(unittest.TestCase):
    """B2: большой токен Bankr при первом скане (индекс строится) — вердикт не лучше RISKY и «Partial scan: …»."""

    def test_score_partial_ceiling(self):
        holders = [("0x" + f"{i:02x}" * 20, 50, 0.05) for i in range(1, 21)]
        sig = {a: {"is_deployer": False, "unread": False, "virgin": False, "kind": "buy", "sniper": False,
                   "launch_bundle": False, "sold": False, "short_history": False} for a, _, _ in holders}
        ops = [{"wallets": [a], "share": s, "share_supply": s, "level": None, "weighted": s} for a, _, s in holders]
        base = {"supply": 1000, "circulating": 1000, "holders_total": 200}
        clean = d.score(holders, sig, ops, base, 10 ** 6)
        self.assertIn(clean["band"], ("CLEAN", "OK"))
        part = d.score(holders, sig, ops, base, 10 ** 6, partial=True)
        self.assertEqual(part["band"], "RISKY")
        self.assertLessEqual(part["score"], d.PARTIAL_SCORE)

    def scan(self, state, eta):
        c = Chain()
        orig = bankr.history
        part = lambda *a, **k: (lambda r: (r[0], r[1], dict(r[2], mode="windowed", top_exact=False), r[3]))(orig(*a, **k))
        with c.patched(), mock.patch.object(bankr, "history", side_effect=part), \
                mock.patch.object(bankr, "index_status", return_value={"state": state, "eta_s": eta}):
            return engine.scan(BTOKEN)

    def test_partial_message_building(self):
        r = self.scan("building", 50)
        self.assertEqual(r["partial_scan"]["message"],
                         "Partial scan: building the full holder history, check again in about a minute")
        self.assertNotIn(r["band"], ("CLEAN", "OK"))
        self.assertIn("about 7 minutes", self.scan("building", 400)["partial_scan"]["message"])
        self.assertIn("too large", self.scan("too_large", None)["partial_scan"]["message"])

    def test_full_scan_has_no_partial(self):
        _, r = scan()
        self.assertIsNone(r["partial_scan"])

    def test_pons_unchanged(self):
        with fakes.patched():
            r = engine.scan(fakes.TOKEN)
        self.assertIsNone(r["partial_scan"])
        self.assertNotIn("launchpad", r)


class TestB2Server(unittest.TestCase):

    def test_cache_bypassed_when_index_ready(self):
        import server
        jid = "j1"
        res = {"token": BTOKEN, "partial_scan": {"state": "building"}}
        with mock.patch.dict(server.JOBS, {jid: {"token": BTOKEN, "done": True, "result": res, "ts": _t.time()}}), \
                mock.patch.dict(server.BY_TOKEN, {BTOKEN: jid}):
            with mock.patch.object(bankr, "index_status", return_value={"state": "building", "eta_s": 60}):
                self.assertEqual(server._reuse(BTOKEN, _t.time()), jid)
            with mock.patch.object(bankr, "index_status", return_value={"state": "ready", "eta_s": 0}):
                self.assertIsNone(server._reuse(BTOKEN, _t.time()))       # индекс готов — новый, полный скан

    def test_config_and_index_endpoint(self):
        import json, server, urllib.request, urllib.error
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_port}"
        try:
            for v, want in (("1", True), ("0", False)):
                with mock.patch.dict(os.environ, {"BANKR_ENABLED": v}):
                    with urllib.request.urlopen(url + "/api/config") as r:
                        self.assertIs(json.loads(r.read())["bankr"], want)
            with mock.patch.dict(os.environ, {"BANKR_ENABLED": "1"}), \
                    mock.patch.object(bankr, "index_status", return_value={"state": "building", "eta_s": 90}):
                with urllib.request.urlopen(url + "/api/index?token=" + BTOKEN) as r:
                    self.assertEqual(json.loads(r.read()), {"token": BTOKEN, "state": "building", "eta_s": 90})
            with mock.patch.dict(os.environ, {"BANKR_ENABLED": "0"}):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(url + "/api/index?token=" + BTOKEN)
                self.assertEqual(cm.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_partial_not_snapshotted(self):
        import server
        with mock.patch.object(server.alerts, "enabled", return_value=True), \
                mock.patch.object(server, "alerts_store") as st:
            server.record_snapshot({"token": BTOKEN, "partial_scan": {"state": "building"}})
        st.assert_not_called()


class TestB2Bot(unittest.TestCase):

    def res(self, **kw):
        r = {"token": BTOKEN, "chain": "robinhood", "header": {"ticker": "CHOP"}, "band": "RISKY", "score": 55,
             "holders": [], "operators": [], "metrics": {}, "gates": [], "launchpad": "bankr",
             "bankr": {"pair": {"kind": "eth", "symbol": "ETH"},
                       "vesting": {"total_share_supply": 0.15, "unlocked_share_supply": 0.0}}}
        r.update(kw)
        return r

    def test_bankr_line(self):
        from bot import text as T
        self.assertEqual(T.bankr_line(self.res()), "Bankr · ETH pair · dev vesting 15%")
        r = self.res(bankr={"pair": {"kind": "token", "symbol": "META"},
                            "vesting": {"total_share_supply": 0.3, "unlocked_share_supply": 0.15}})
        self.assertEqual(T.bankr_line(r), "Bankr · META pair · dev vesting 30% (15% unlocked)")
        self.assertEqual(T.bankr_line(self.res(bankr={"pair": {"kind": "eth"}, "vesting": {}})), "Bankr · ETH pair")
        self.assertIsNone(T.bankr_line({"launchpad": None}))
        self.assertIn("Bankr · ETH pair · dev vesting 15%", T.verdict(self.res()).split("\n")[1])

    def test_partial_line(self):
        from bot import text as T
        msg = "Partial scan: building the full holder history, check again in about a minute"
        v = T.verdict(self.res(partial_scan={"state": "building", "message": msg}))
        self.assertIn("⏳ " + msg, v)
        self.assertNotIn("Partial", T.verdict(self.res()))

    def test_help_with_bankr(self):
        from bot import text as T
        self.assertIn("Pons V2, Flap and Bankr on Robinhood Chain", T.help_text(True, True))
        self.assertIn("Pons V2 and Bankr on Robinhood Chain", T.help_text(False, True))
        self.assertEqual(T.help_text(True), T.help_text(True, False))
        self.assertEqual(T.help_text(False, False), T.HELP)


class TestB2Frontend(unittest.TestCase):
    """Собранный index.html: правки Bankr есть и все — под флагом с сервера (bankrOn из /api/config)."""

    def test_built_page(self):
        import json, re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "index.html"), encoding="utf-8").read()
        a = re.search(r'<script type="__bundler/template">', src).end()
        t = json.loads(src[a:src.find("</script>", a)])
        self.assertIn("bankrOn:!!d.bankr", t)
        self.assertIn("state={bankrOn:false,", t)
        self.assertIn("bankrOf(r)?'Bankr · Uniswap V4'", t)
        self.assertIn("this.state.bankrOn&&this.state.view==='scan'", t)
        self.assertIn("bankr:!!this.state.bankrOn&&x.launchpad==='bankr'", t)
        self.assertIn("'/api/index?token='", t)
        self.assertIn("dev vesting: ${bankrPct(v.total_share_supply)} (${bankrPct(v.unlocked_share_supply)} unlocked)", t)
        self.assertIn('<sc-if value="{{partialOn}}"', t)
        m = t.index("Bankr launchpad</span>")
        self.assertIn('<sc-if value="{{bankrOn2}}"', t[m - 600:m])
