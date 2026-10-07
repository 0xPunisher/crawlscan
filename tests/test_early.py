"""Early buyers: адаптер Solana (early_buyers / early_status), detect.early_report, сервис early.py и /api/early.
Без сети: транзакции jsonParsed собираются здесь, RPC подменены."""
import json, os, threading, time, unittest, urllib.error, urllib.request
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest import mock

import fakes
from fakes import engine, market
from chains import solana as sol
import detect as d
import early
import server

MINT = sol.b58encode(bytes([7]) * 32)
CURVE = sol.b58encode(bytes([8]) * 32)
POOL_PDA = sol.b58encode(bytes([9]) * 32)
DEV = sol.b58encode(bytes([19]) * 32)
W = [sol.b58encode(bytes([20 + i]) * 32) for i in range(30)]
SINK = sol.b58encode(bytes([90]) * 32)       # общий кошелёк, куда два покупателя переводят токены
SLOT0, TS0 = 5000, 1_800_000_000
PDAS = {CURVE, POOL_PDA}
SUPPLY = 1_000_000_000


def acc(owner):
    return "acc_" + owner[:8]


def tx(slot, moves, trade=True):
    """Транзакция jsonParsed: moves = {владелец: (было, стало)} по MINT; trade — есть ли DEX-программа (pump.fun)."""
    owners = list(moves)
    keys = [{"pubkey": acc(o)} for o in owners]
    bal = lambda side: [{"accountIndex": i, "mint": MINT, "owner": o,
                         "uiTokenAmount": {"amount": str(moves[o][side])}} for i, o in enumerate(owners)]
    prog = sol.PUMP if trade else sol.TOKEN
    return {"slot": slot, "blockTime": TS0 + (slot - SLOT0) // 2,
            "transaction": {"message": {"accountKeys": keys, "instructions": [{"programId": prog}]}},
            "meta": {"preTokenBalances": bal(0), "postTokenBalances": bal(1), "innerInstructions": [],
                     "preBalances": [0] * len(keys), "postBalances": [0] * len(keys)}}


def sig(i):
    return f"sig{i:04d}"


class Chain:
    """Подставная Solana: кривая с транзакциями покупок, балансы аккаунтов, история аккаунтов."""

    def __init__(self, n_buyers=25, history=None, balances=None):
        self.txs, sigs = {}, []
        curve_bal = 800_000_000
        self.txs[sig(0)] = tx(SLOT0, {CURVE: (0, curve_bal), DEV: (0, 30_000_000)})   # create + дев-бай
        sigs.append({"signature": sig(0), "err": None, "blockTime": TS0, "slot": SLOT0})
        for i in range(n_buyers):
            s = sig(i + 1)
            if i == 3:   # перевод, не покупка
                self.txs[s] = tx(SLOT0 + i + 1, {W[0]: (5, 4), SINK: (0, 1)}, trade=False)
            else:
                self.txs[s] = tx(SLOT0 + i + 1, {CURVE: (curve_bal, curve_bal - 1000 * (i + 1)), W[i]: (0, 1000 * (i + 1))})
            sigs.append({"signature": s, "err": None, "blockTime": TS0 + i, "slot": SLOT0 + i + 1})
        self.sigs = sigs
        self.history = history or {}     # аккаунт -> [подписи новые → старые]
        self.balances = balances or {}   # аккаунт -> сейчас (нет — закрыт)
        self.calls = []

    def rpc(self, method, params):
        self.calls.append(method)
        if method == "getTokenSupply":
            return {"value": {"amount": str(SUPPLY)}}
        if method == "getMultipleAccounts":
            return {"value": [None if a not in self.balances else
                              {"data": {"parsed": {"info": {"tokenAmount": {"amount": str(self.balances[a])}}}}}
                              for a in params[0]]}
        raise AssertionError(method)

    def rpc_batch(self, calls, chunk=50):
        out = []
        for m, p in calls:
            self.calls.append(m)
            assert m == "getSignaturesForAddress", m
            out.append([{"signature": s, "err": None, "slot": self.txs[s]["slot"]} for s in self.history.get(p[0], [])])
        return out

    def get_transactions(self, sigs):
        self.calls.append(f"getTransaction x{len(sigs)}")
        return {s: self.txs.get(s) for s in sigs}

    def patch(self, st):
        p = lambda name, **kw: st.enter_context(mock.patch.object(sol, name, **kw))
        p("rpc", side_effect=self.rpc)
        p("rpc_batch", side_effect=self.rpc_batch)
        p("get_transactions", side_effect=self.get_transactions)
        p("is_pda", side_effect=lambda a: a in PDAS)
        p("get_launch", side_effect=AssertionError("get_launch не нужен: запуск в памяти"))
        st.enter_context(mock.patch.dict(sol._LAUNCH, {MINT: {"block": SLOT0, "ts": TS0, "deployer": DEV,
                                                                "curve": CURVE}}))
        st.enter_context(mock.patch.dict(sol._LAUNCH_SIGS, {MINT: self.sigs}))
        st.enter_context(mock.patch.dict(sol._SUPPLY, clear=True))


class TestStatus(unittest.TestCase):

    def test_statuses(self):
        S = d.early_status
        self.assertEqual(S(100, 100), "holding_all")
        self.assertEqual(S(100, 150, sold=10), "added")
        self.assertEqual(S(100, 60, sold=40), "sold_part")
        self.assertEqual(S(100, 0, sold=100), "sold_all")
        self.assertEqual(S(100, 0.5, sold=99.5), "sold_all")              # пыль < 1% купленного — вышел
        self.assertEqual(S(100, 0, sold=10, moved=90), "moved")
        self.assertEqual(S(100, 50, sold=10, moved=40), "moved")          # частично, перевёл больше, чем продал
        self.assertEqual(S(100, 0, sold=20, burned=80), "burned")
        self.assertEqual(S(100, 0, sold=50, moved=50), "sold_all")        # поровну — продажа
        self.assertEqual(S(100, 0), "sold_all")                           # выходы не разобраны (partial)

    def test_moved_threshold(self):
        S = d.early_status
        self.assertEqual(d.EARLY_MOVED_MIN, 0.20)
        self.assertEqual(S(100, 95, moved=5), "holding_all")              # мелкий перевод — держит остальное
        self.assertEqual(S(100, 97, moved=5), "added")                    # и докупил
        self.assertEqual(S(100, 0, sold=85, moved=15), "sold_all")        # остальное продал
        self.assertEqual(S(100, 40, sold=45, moved=15), "sold_part")
        self.assertEqual(S(100, 0, moved=19), "sold_all")                 # 19% — ещё не moved
        self.assertEqual(S(100, 80, moved=20), "moved")                   # ровно 20% — moved
        self.assertEqual(S(100, 0, sold=70, moved=30), "sold_all")        # moved ≥ 20%, но продал больше
        rep = d.early_report({"launch": {"block": 1, "ts": 0, "deployer": "x"},
                              "buyers": [{"wallet": "w", "block": 1, "ts": 0, "bought": 100}]},
                             {"supply": 1000, "wallets": {"w": {"now": 95, "sold": 0, "moved": {"z": 5}, "burned": 0,
                                                                "partial": False}}})
        b = rep["buyers"][0]
        self.assertEqual((b["status"], b["moved_share_supply"], b["moved_to"]), ("holding_all", 0.005, ["z"]))
        self.assertEqual(rep["summary"]["same_destination"], [])


class TestSolanaAdapter(unittest.TestCase):

    def test_first_buyers(self):
        c = Chain()
        with ExitStack() as st:
            c.patch(st)
            data = sol.early_buyers(MINT, 20)
        bs = data["buyers"]
        self.assertEqual(len(bs), 20)
        self.assertEqual(bs[0]["wallet"], DEV)                            # дев-бай в create — первый покупатель
        self.assertEqual(bs[0]["bought"], 30_000_000)
        ws = [b["wallet"] for b in bs]
        self.assertNotIn(CURVE, ws)                                       # кривая — не покупатель
        self.assertNotIn(SINK, ws)                                        # получил переводом — не покупатель
        self.assertNotIn(W[3], ws)
        self.assertEqual(len(set(ws)), 20)
        self.assertEqual(bs[1], {"wallet": W[0], "block": SLOT0 + 1, "ts": TS0, "tx": sig(1), "bought": 1000,
                                 "account": acc(W[0])})
        self.assertEqual(data["launch"], {"block": SLOT0, "ts": TS0, "deployer": DEV, "curve": CURVE})
        self.assertEqual([x for x in c.calls if x.startswith("getTransaction")], [f"getTransaction x{len(c.sigs)}"])

    def test_few_buyers(self):
        c = Chain(n_buyers=5)
        with ExitStack() as st:
            c.patch(st)
            data = sol.early_buyers(MINT, 20)
        self.assertEqual(len(data["buyers"]), 5)                          # dev + 4 покупки

    def test_status_sold_moved_burned(self):
        a = lambda i: acc(W[i])
        c = Chain(history={a(0): ["out0"], a(1): ["out1b", "out1a"], a(2): ["out2"], a(4): ["out4"],
                           a(5): ["burn5"]},
                  balances={acc(DEV): 30_000_000, a(1): 0, a(4): 0, a(5): 0, a(6): 9000})
        c.txs["out0"] = tx(SLOT0 + 50, {W[0]: (1000, 0), POOL_PDA: (10, 1010)})             # продал всё
        c.txs["out1a"] = tx(SLOT0 + 51, {W[1]: (2000, 1000), POOL_PDA: (0, 1000)})          # продал половину
        c.txs["out1b"] = tx(SLOT0 + 52, {W[1]: (1000, 0), SINK: (0, 1000)}, trade=False)  # остальное перевёл
        c.txs["out2"] = tx(SLOT0 + 53, {W[2]: (3000, 0), SINK: (1000, 4000)}, trade=False)  # перевёл всё на тот же
        c.txs["out4"] = tx(SLOT0 + 54, {W[4]: (5000, 0), W[20]: (0, 3000), W[21]: (0, 2000)}, trade=False)
        c.txs["burn5"] = tx(SLOT0 + 55, {W[5]: (6000, 0)}, trade=False)                     # сжёг (нет получателя)
        with ExitStack() as st:
            c.patch(st)
            data = sol.early_buyers(MINT, 8)
            status = sol.early_status(MINT, data["buyers"])
            rep = d.early_report(data, status, flags={W[1]: ["operator"], DEV: []})
        ws = status["wallets"]
        self.assertEqual(ws[DEV]["now"], 30_000_000)
        self.assertEqual((ws[W[0]]["sold"], ws[W[0]]["now"]), (1000, 0))                   # аккаунт закрыт — 0
        self.assertEqual((ws[W[1]]["sold"], ws[W[1]]["moved"]), (1000, {SINK: 1000}))
        self.assertEqual(ws[W[2]]["moved"], {SINK: 3000})
        self.assertEqual(ws[W[4]]["moved"], {W[20]: 3000, W[21]: 2000})                    # двум получателям
        self.assertEqual(ws[W[5]]["burned"], 6000)
        self.assertEqual(ws[W[6]]["now"], 9000)
        self.assertNotIn(acc(DEV), [x for x in c.calls])                                    # держит — истории не читаем
        rows = {r["wallet"]: r for r in rep["buyers"]}
        self.assertEqual([r["rank"] for r in rep["buyers"]], list(range(1, 9)))
        self.assertEqual({w: rows[w]["status"] for w in (DEV, W[0], W[1], W[2], W[4], W[5], W[6])},
                         {DEV: "holding_all", W[0]: "sold_all", W[1]: "sold_all", W[2]: "moved",
                          W[4]: "moved", W[5]: "burned", W[6]: "added"})
        self.assertTrue(rows[DEV]["dev"])
        self.assertEqual(rows[W[1]]["scan_flags"], ["operator"])
        self.assertEqual(rows[DEV]["scan_flags"], [])                                       # скан проверял, флагов нет
        self.assertIsNone(rows[W[0]]["scan_flags"])                                         # скан не проверял
        self.assertEqual((rows[W[2]]["block_offset"], rows[W[2]]["dt_s"]), (3, 1))
        self.assertAlmostEqual(rows[DEV]["bought_share_supply"], 0.03)
        self.assertEqual(rep["summary"]["same_destination"], [{"to": SINK, "wallets": [W[1], W[2]]}])
        self.assertEqual((rep["summary"]["exited"], rep["summary"]["holding"]), (6, 2))     # 0,1,2,4,5,7 вышли
        self.assertAlmostEqual(rep["summary"]["now_share_supply"], (30_000_000 + 9000) / SUPPLY)
        self.assertTrue(rows[W[7]]["partial"])                    # баланс 0, а выходов не нашли — разобрано не всё
        self.assertFalse(rows[W[0]]["partial"])
        self.assertTrue(rep["summary"]["partial"])

    def test_partial_when_history_long(self):
        hist = [f"h{i}" for i in range(sol.EARLY_OUT_CAP + 5)]
        c = Chain(history={acc(W[0]): hist}, balances={acc(DEV): 30_000_000})
        for i, s in enumerate(hist):
            c.txs[s] = tx(SLOT0 + 100 + i, {W[0]: (40, 0), POOL_PDA: (0, 40)})
        with ExitStack() as st:
            c.patch(st)
            data = sol.early_buyers(MINT, 2)
            status = sol.early_status(MINT, data["buyers"])
        self.assertTrue(status["wallets"][W[0]]["partial"])
        self.assertIn(f"getTransaction x{sol.EARLY_OUT_CAP}", c.calls)                     # не больше EARLY_OUT_CAP
        self.assertFalse(status["wallets"][DEV]["partial"])

    def test_deadline_marks_partial(self):
        c = Chain(history={acc(W[0]): ["x"]}, balances={})
        with ExitStack() as st:
            c.patch(st)
            data = sol.early_buyers(MINT, 2)
            status = sol.early_status(MINT, data["buyers"], deadline=time.time() - 1)
        self.assertTrue(status["wallets"][W[0]]["partial"])
        self.assertNotIn("getSignaturesForAddress", c.calls)

    def test_get_launch_fills_launch_sigs(self):
        self.assertTrue(hasattr(sol, "_LAUNCH_SIGS"))
        self.assertGreaterEqual(sol.EARLY_TX_MAX, 60)


from chains import robinhood as rh

RT = "0x" + "ab" * 20
RCURVE = "0x" + "c0" * 20
RDEV = "0x" + "de" * 20
BOT = "0x" + "b0" * 20          # сторонний бот-роутер (не в рынке): продажа видна только по чеку
RCONTRACT = "0x" + "cc" * 20    # контракт-получатель — не покупатель
RW = ["0x" + f"{i + 1:02x}" * 20 for i in range(30)]
RSINK = "0x" + "99" * 20
LB = 1000


class RChain:
    """Подставной Robinhood: переводы с фильтрами getLogs, чеки, классификация входа."""

    def __init__(self, n=24, late_from=None):
        self.trs, self.receipts, self.calls = [], {}, []
        add = lambda frm, to, amt, block, tx, ts=True: self.trs.append(
            {"frm": frm, "to": to, "amount": amt, "tx": tx, "block": block, "log_index": len(self.trs)}
            | ({"ts": 1_800_000_000 + (block - LB) * 2} if ts else {}))
        add(RCURVE, RDEV, 50_000, LB, "0xlaunch")
        add(RCURVE, RCONTRACT, 1, LB + 1, "0xc")
        for i in range(n):
            b = LB + 2 + i if late_from is None or i < late_from else LB + rh.EARLY_WINDOW + 50 + i
            add(RCURVE, RW[i], 100 * (i + 1), b, f"0xbuy{i}", ts=i % 2 == 0)
        add(RW[0], RW[25], 5, LB + 40, "0xgift")               # RW[25] получил переводом — не покупатель
        self.transfer_only = {RW[25]}
        self.add = add

    def get_token_transfers(self, token, from_block, to_block=None, frm=None, to=None):
        self.calls.append(("logs", from_block, to_block, bool(frm), bool(to)))
        f = lambda a, flt: flt is None or a in ([flt] if isinstance(flt, str) else flt)
        return [dict(t) for t in self.trs if t["block"] >= from_block and (to_block is None or t["block"] <= to_block)
                and f(t["frm"], frm) and f(t["to"], to)]

    def classify(self, token, trs, wallets):
        self.calls.append(("classify", len(wallets)))
        return {w: {"kind": "transfer" if w in self.transfer_only else "buy"} for w in wallets}

    def rpc_batch(self, calls):
        self.calls.append(("receipts", len(calls)))
        return [{"logs": self.receipts.get(p[0], [])} for _, p in calls]

    def patch(self, st):
        p = lambda name, **kw: st.enter_context(mock.patch.object(rh, name, **kw))
        p("get_launch", return_value={"block": LB, "curve": RCURVE, "deployer": RDEV, "tx": "0xlaunch"})
        p("block_number", return_value=LB + 100_000)
        p("get_token_transfers", side_effect=self.get_token_transfers)
        p("is_contract", side_effect=lambda addrs: {a: a == RCONTRACT for a in addrs})
        p("classify_entries", side_effect=self.classify)
        p("block_timestamps", side_effect=lambda bl: {b: 1_800_000_000 + (b - LB) * 2 for b in bl})
        p("token_supply", return_value=1_000_000)
        p("rpc_batch", side_effect=self.rpc_batch)
        p("_post", side_effect=fakes._no_network)
        st.enter_context(mock.patch.dict(rh._SCAN, clear=True))
        st.enter_context(mock.patch.dict(rh._RECEIPTS, clear=True))


class TestRobinhoodAdapter(unittest.TestCase):

    def test_first_buyers_window(self):
        c = RChain()
        with ExitStack() as st:
            c.patch(st)
            data = rh.early_buyers(RT, 20)
        ws = [b["wallet"] for b in data["buyers"]]
        self.assertEqual(ws, [RDEV] + RW[:19])                       # контракт и инфраструктура — не покупатели
        self.assertEqual(data["buyers"][2], {"wallet": RW[1], "block": LB + 3, "ts": 1_800_000_006, "tx": "0xbuy1",
                                             "bought": 200})          # ts из блока, когда в логе его нет
        self.assertEqual(data["launch"], {"block": LB, "ts": 1_800_000_000, "deployer": RDEV, "curve": RCURVE})
        self.assertEqual([x for x in c.calls if x[0] == "logs"], [("logs", LB, LB + rh.EARLY_WINDOW, False, False)])
        self.assertFalse([x for x in c.calls if x[0] == "classify"])  # всё прямо с кривой — без чеков

    def test_bot_router_entry_checked_by_receipt(self):
        c = RChain(n=3)
        c.trs.insert(1, {"frm": BOT, "to": RW[26], "amount": 7, "tx": "0xbot", "block": LB, "log_index": 99})
        c.trs.insert(2, {"frm": RW[0], "to": RW[27], "amount": 1, "tx": "0xg", "block": LB, "log_index": 98})
        c.transfer_only.add(RW[27])
        with ExitStack() as st:
            c.patch(st)
            data = rh.early_buyers(RT, 20)
        ws = [b["wallet"] for b in data["buyers"]]
        self.assertIn(RW[26], ws)                                     # бот-роутер: покупка по чеку
        self.assertNotIn(RW[27], ws)                                  # от кошелька, чек — перевод
        self.assertEqual([x for x in c.calls if x[0] == "classify"], [("classify", 3)])   # RW[26], RW[27], RW[25]

    def test_window_grows_when_few_buyers(self):
        c = RChain(late_from=10)                                      # половина покупок — после первого окна
        with ExitStack() as st:
            c.patch(st)
            data = rh.early_buyers(RT, 20)
        self.assertEqual(len(data["buyers"]), 20)
        logs = [x for x in c.calls if x[0] == "logs"]
        self.assertEqual(len(logs), 2)
        self.assertEqual(logs[1][1:3], (LB + rh.EARLY_WINDOW + 1, LB + 2 * rh.EARLY_WINDOW))

    def test_status(self):
        c = RChain()
        c.add(RW[1], RCURVE, 200, LB + 60, "0xsell1")                # продал на кривую (рынок)
        c.add(RW[2], BOT, 150, LB + 61, "0xsell2")                   # продал через бот-роутер (по чеку)
        c.add(RW[2], RSINK, 150, LB + 62, "0xmove2")                 # остальное перевёл
        c.add(RW[3], RSINK, 400, LB + 63, "0xmove3")                 # перевёл всё на тот же кошелёк
        c.add(RW[4], "0x000000000000000000000000000000000000dead", 500, LB + 64, "0xburn4")
        c.add(RCURVE, RW[5], 1000, LB + 65, "0xbuy5b")               # докупил
        c.receipts["0xsell2"] = [{"topics": [rh.CURVE_SELL], "address": RCURVE, "data": "0x"}]
        with ExitStack() as st:
            c.patch(st)
            data = rh.early_buyers(RT, 8)
            status = rh.early_status(RT, data["buyers"], launch=data["launch"])
            rep = d.early_report(data, status)
        ws = status["wallets"]
        self.assertEqual(ws[RW[1]]["sold"], 200)
        self.assertEqual((ws[RW[2]]["sold"], ws[RW[2]]["moved"]), (150, {RSINK: 150}))
        self.assertEqual(ws[RW[3]]["moved"], {RSINK: 400})
        self.assertEqual(ws[RW[4]]["burned"], 500)
        self.assertEqual(ws[RW[0]]["now"], 95)                        # 100 − 5 (подарил RW[25])
        self.assertEqual(ws[RW[5]]["now"], 1600)
        rows = {r["wallet"]: r for r in rep["buyers"]}
        self.assertEqual({w: rows[w]["status"] for w in (RDEV, RW[0], RW[1], RW[2], RW[3], RW[4], RW[5])},
                         {RDEV: "holding_all", RW[0]: "holding_all", RW[1]: "sold_all", RW[2]: "sold_all",
                          RW[3]: "moved", RW[4]: "burned", RW[5]: "added"})
        self.assertEqual(rep["summary"]["same_destination"], [{"to": RSINK, "wallets": [RW[2], RW[3]]}])
        self.assertEqual([x for x in c.calls if x[0] == "logs"][-2:],
                         [("logs", LB, None, False, True), ("logs", LB, None, True, False)])   # входы и выходы — 2 getLogs
        self.assertEqual([x for x in c.calls if x[0] == "receipts"], [("receipts", 4)])         # чеки только не-рынка

    def test_status_partial_and_deadline(self):
        c = RChain()
        for i in range(rh.EARLY_OUT_CAP + 3):
            c.add(RW[1], RSINK, 1, LB + 100 + i, f"0xm{i}")
        with ExitStack() as st:
            c.patch(st)
            data = rh.early_buyers(RT, 4)
            status = rh.early_status(RT, data["buyers"], launch=data["launch"])
            late = rh.early_status(RT, data["buyers"], deadline=time.time() - 1, launch=data["launch"])
        self.assertTrue(status["wallets"][RW[1]]["partial"])
        self.assertEqual(sum(status["wallets"][RW[1]]["moved"].values()), rh.EARLY_OUT_CAP)
        self.assertTrue(late["wallets"][RW[1]]["partial"])            # чеки не проверены
        self.assertFalse(status["wallets"][RDEV]["partial"])


class TestScanReuse(unittest.TestCase):
    """Robinhood: история переводов скана — без окна и без getLogs по топикам; Solana: сапплай скана."""

    def remember(self, c, age=0):
        trs = c.get_token_transfers(RT, LB)
        c.calls.clear()
        with mock.patch.object(rh.time, "time", return_value=time.time() - age):
            rh.remember_scan(RT, {"block": LB, "curve": RCURVE, "deployer": RDEV, "tx": "0xlaunch"}, trs, 777)

    def test_buyers_and_status_from_scan(self):
        c = RChain()
        c.add(RW[1], RCURVE, 200, LB + 60, "0xsell1")
        with ExitStack() as st:
            c.patch(st)
            st.enter_context(mock.patch.object(rh, "get_launch", side_effect=AssertionError("запуск — из скана")))
            st.enter_context(mock.patch.object(rh, "block_number", side_effect=AssertionError("окно не нужно")))
            self.remember(c)
            c.add(RW[2], RCURVE, 300, LB + 70, "0xsell2")                 # продал уже после скана — хвост
            data = rh.early_buyers(RT, 8)
            self.assertFalse([x for x in c.calls if x[0] == "logs"])      # покупатели — без запросов переводов
            status = rh.early_status(RT, data["buyers"], launch=data["launch"])
        self.assertEqual([b["wallet"] for b in data["buyers"]], [RDEV] + RW[:7])
        self.assertEqual([x for x in c.calls if x[0] == "logs"], [("logs", LB + 60, None, False, False)])  # только хвост
        ws = status["wallets"]
        self.assertEqual((ws[RW[1]]["sold"], ws[RW[1]]["now"]), (200, 0))   # из истории скана
        self.assertEqual((ws[RW[2]]["sold"], ws[RW[2]]["now"]), (300, 0))   # из хвоста
        self.assertEqual(status["supply"], 777)                             # сапплай скана

    def test_tail_dedup(self):
        c = RChain()
        c.add(RW[1], RCURVE, 200, LB + 60, "0xsell1")
        with ExitStack() as st:
            c.patch(st)
            self.remember(c)
            merged = rh._with_tail(RT, rh.scan_history(RT)[1])
        self.assertEqual(len(merged), len(c.trs))                           # последний блок не задвоился

    def test_stale_scan_ignored(self):
        c = RChain()
        with ExitStack() as st:
            c.patch(st)
            self.remember(c, age=rh.SCAN_TTL + 1)
            self.assertIsNone(rh.scan_history(RT))
            rh.early_buyers(RT, 5)
        self.assertEqual([x for x in c.calls if x[0] == "logs"][0], ("logs", LB, LB + rh.EARLY_WINDOW, False, False))

    def test_token_facts_remembers(self):
        with ExitStack() as st:
            p = lambda name, **kw: st.enter_context(mock.patch.object(rh, name, **kw))
            st.enter_context(mock.patch.dict(rh._SCAN, clear=True))
            p("get_token_transfers", return_value=[{"frm": RCURVE, "to": RW[0], "amount": 5, "tx": "0x1", "block": LB,
                                                    "log_index": 0}])
            p("token_supply", return_value=1000)
            rh.token_facts(RT, {"block": LB, "curve": RCURVE, "deployer": RDEV, "tx": "0x0"})
            launch, trs, supply = rh.scan_history(RT)
        self.assertEqual((launch["curve"], len(trs), supply), (RCURVE, 1, 1000))

    def test_receipts_and_block_times_cached(self):
        calls = []
        def batch(cs):
            calls.append(len(cs))
            return [{"logs": [{"x": 1}]} if m == "eth_getTransactionReceipt" else {"timestamp": hex(100)} for m, _ in cs]
        with mock.patch.object(rh, "rpc_batch", side_effect=batch), mock.patch.dict(rh._RECEIPTS, clear=True), \
                mock.patch.dict(rh._BLOCK_TS, clear=True):
            rh.receipt_logs(["0xa", "0xb"])
            self.assertEqual(rh.receipt_logs(["0xa", "0xb", "0xc"]), {"0xa": [{"x": 1}], "0xb": [{"x": 1}], "0xc": [{"x": 1}]})
            rh.block_timestamps([5, 6])
            self.assertEqual(rh.block_timestamps([5, 6, 7]), {5: 100, 6: 100, 7: 100})
        self.assertEqual(calls, [2, 1, 2, 1])                               # прочитанное — не спрашиваем снова

    def test_solana_supply_from_scan(self):
        c = Chain(balances={acc(DEV): 30_000_000})
        with ExitStack() as st:
            c.patch(st)
            sol._SUPPLY[MINT] = (time.time(), 555)
            data = sol.early_buyers(MINT, 2)
            self.assertEqual(sol.early_status(MINT, data["buyers"])["supply"], 555)
            self.assertNotIn("getTokenSupply", c.calls)
            sol._SUPPLY[MINT] = (time.time() - sol.SCAN_TTL - 1, 555)
            self.assertEqual(sol.early_status(MINT, data["buyers"])["supply"], SUPPLY)


class TestService(unittest.TestCase):

    def setUp(self):
        early.clear_cache()
        self.addCleanup(early.clear_cache)
        market.clear_cache()
        self.addCleanup(market.clear_cache)
        st = ExitStack()
        self.addCleanup(st.close)
        st.enter_context(mock.patch.dict(os.environ, {"SOLANA_ENABLED": "true"}))
        st.enter_context(mock.patch("builtins.print"))
        self.c = Chain(history={acc(W[0]): ["out0"]}, balances={acc(DEV): 30_000_000})
        self.c.txs["out0"] = tx(SLOT0 + 50, {W[0]: (1000, 0), POOL_PDA: (10, 1010)})
        self.c.patch(st)

    def test_result_and_cache(self):
        r = early.get(MINT)
        self.assertEqual((r["available"], r["chain"], len(r["buyers"]), r["block"]), (True, "solana", 20, SLOT0))
        self.assertEqual(r["summary"]["buyers"], 20)
        self.assertNotIn("stale_at", r)
        n = len(self.c.calls)
        r2 = early.get(MINT, flags={W[0]: ["virgin"]})
        self.assertEqual(len(self.c.calls), n)                              # статусы из кэша (10 мин)
        self.assertEqual(r2["buyers"][1]["scan_flags"], ["virgin"])        # флаги — по последнему скану
        key = ("solana", MINT)
        ts, data, status = early._STATUS[key]
        early._STATUS[key] = (ts - early.STATUS_TTL - 1, data, status)
        early.get(MINT)
        self.assertNotIn("getTransaction x60", self.c.calls[n:])          # покупатели — из суточного кэша
        self.assertIn("getMultipleAccounts", self.c.calls[n:])            # балансы — заново

    def test_failure_stale_then_error(self):
        early.get(MINT)
        key = ("solana", MINT)
        ts, data, status = early._STATUS[key]
        early._STATUS[key] = (ts - early.STATUS_TTL - 1, data, status)
        with mock.patch.object(sol, "early_status", side_effect=RuntimeError("rpc down")):
            r = early.get(MINT)
            self.assertEqual(r["stale_at"], int(ts - early.STATUS_TTL - 1))  # последний удачный
            self.assertEqual(len(r["buyers"]), 20)
            early._STATUS[key] = (ts - early.STALE_TTL - 1, data, status)
            r = early.get(MINT)
        self.assertEqual((r["available"], r["error"]), (True, "temporarily unavailable"))
        self.assertNotIn("buyers", r)

    def test_not_available(self):
        market.established_put(MINT, "solana", {"age_days": 400})
        self.assertEqual(early.get(MINT)["reason"], "too established")
        with mock.patch.object(engine, "CHAINS", engine.CHAINS | {"robinhood": object()}):
            self.assertEqual(early.get(fakes.TOKEN), {"token": fakes.TOKEN, "chain": "robinhood", "available": False,
                                                      "reason": "not supported for this chain yet"})
        with self.assertRaises(engine.ScanError):
            early.get("hello")

    def test_not_pump_fun_and_long_history(self):
        sol._LAUNCH_SIGS.pop(MINT)
        with mock.patch.object(sol, "get_launch", return_value=None):
            self.assertEqual(early.get(MINT)["reason"], "not a pump.fun token")
        with mock.patch.object(sol, "get_launch",
                               side_effect=RuntimeError("launch not found: bonding curve history too long")):
            self.assertEqual(early.get(MINT)["reason"], "launch history too long")

    def test_single_flight(self):
        gate, calls = threading.Event(), []
        real = sol.early_status

        def slow(*a, **k):
            calls.append(1)
            gate.wait(2)
            return real(*a, **k)
        out = []
        with mock.patch.object(sol, "early_status", side_effect=slow):
            ts = [threading.Thread(target=lambda: out.append(early.get(MINT))) for _ in range(3)]
            for t in ts:
                t.start()
            time.sleep(0.2)
            gate.set()
            for t in ts:
                t.join(5)
        self.assertEqual(len(calls), 1)                                     # один расчёт на токен
        self.assertEqual([len(r["buyers"]) for r in out], [20, 20, 20])


class TestApi(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_bad_address(self):
        self.assertEqual(self.get("/api/early?token=hello"), (400, {"error": "not a token address"}))

    def test_flags_from_last_scan(self):
        res = {"holders": [{"wallet": W[0], "signals": {"virgin": True}}, {"wallet": W[1], "signals": {}},
                           {"wallet": W[2], "signals": {"virgin": False}}],
               "operators": [{"wallets": [W[1], W[2]]}, {"wallets": [W[0]]}]}
        with server._lock:
            server.JOBS["j1"] = {"token": MINT, "done": True, "result": res, "ts": time.time()}
            server.BY_TOKEN[MINT] = "j1"
        try:
            self.assertEqual(server.scan_flags(MINT), {W[0]: ["virgin"], W[1]: ["operator"], W[2]: ["operator"]})
            seen = {}
            with mock.patch.dict(os.environ, {"SOLANA_ENABLED": "true"}), \
                    mock.patch.object(early, "get", side_effect=lambda t, f: seen.update(f=f) or {"ok": 1}):
                self.assertEqual(self.get(f"/api/early?token={MINT}"), (200, {"ok": 1}))
            self.assertEqual(seen["f"][W[0]], ["virgin"])
        finally:
            with server._lock:
                server.JOBS.pop("j1", None)
                server.BY_TOKEN.pop(MINT, None)
        self.assertIsNone(server.scan_flags(MINT))                          # скана нет


if __name__ == "__main__":
    unittest.main()
