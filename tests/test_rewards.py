"""Тесты Token Burn & Holder Rewards: правила (rewards.py), хранилище, оркестрация с подставной сетью, API."""
import json, math, os, tempfile, threading, time, unittest, urllib.error, urllib.request
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from unittest import mock

import fakes  # noqa: F401  (запрещает настоящие RPC адаптера Robinhood, ставит пути)
import rewards as rw
import rewards_service as rs
from chains import robinhood as ch
from rewards_store import RewardsStore
import server

TOKEN = "0x" + "ab" * 20
CURVE = "0x" + "c0" * 20
DEV, DEV2 = "0x" + "de" * 20, "0x" + "df" * 20
CONTRACT = "0x" + "cc" * 20
A, B, C, OUTSIDER = ("0x" + f"{i:02x}" * 20 for i in (0xa1, 0xb1, 0xc1, 0xe1))
DAY = "2026-10-04"
DAY_START, DAY_END = rw.day_bounds(DAY)
BLOCK_SECONDS = 10
DAY0 = 1000                                       # первый блок суток
T0 = DAY_START - DAY0 * BLOCK_SECONDS             # timestamp блока 0
LAUNCH = 10


def blk(ts):
    return math.ceil((ts - T0) / BLOCK_SECONDS)


def at(h, m=0, day_start=DAY_START):
    """Блок в момент hh:mm суток."""
    return blk(day_start + h * 3600 + m * 60)


class FakeChain:
    """Подставная Robinhood Chain: блоки каждые 10 с, переводы токена из списка, код контрактов, голова цепи."""

    def __init__(self, head_ts=None):
        self.transfers = []
        self.head_ts = head_ts or DAY_END + 2 * 86400
        self.contracts = {CONTRACT}
        self.calls = {"seed": 0, "launch": 0}
        self.add(ZERO_, CURVE, 10 ** 9, LAUNCH)        # минт в кривую
        self.add(CURVE, A, 1000, 20)                    # A держит весь день до 06:00
        self.add(CURVE, DEV, 5000, 20)                  # разработчик
        self.add(CURVE, CONTRACT, 300, 20)              # контракт
        self.add(A, C, 400, at(6))                      # 06:00: A -> C
        self.add(CURVE, B, 1000, at(12))                # 12:00: B купил
        self.add(DEV, rw.DEAD, 100, at(15))             # сжигание разработчика

    def add(self, frm, to, amount, block, tx=None):
        t = {"frm": frm, "to": to, "amount": amount, "block": block, "tx": tx or f"0xtx{len(self.transfers):04d}",
             "log_index": len(self.transfers)}
        self.transfers.append(t)
        self.transfers.sort(key=lambda t: (t["block"], t["log_index"]))
        return t

    def head(self):
        return blk(self.head_ts)

    def block_at_time(self, ts):
        if ts > self.head_ts:
            raise LookupError("future")
        if ts == rw.seed_time(DAY) or ts > DAY_END:
            self.calls["seed"] += 1
        n = blk(ts)
        return {"number": n, "hash": f"0x{n:064x}", "timestamp": T0 + n * BLOCK_SECONDS}

    def get_token_transfers(self, token, from_block, to_block=None, frm=None, to=None):
        hi = self.head() if to_block is None else to_block
        fs = None if frm is None else ({frm} if isinstance(frm, str) else set(frm))
        ts = None if to is None else ({to} if isinstance(to, str) else set(to))
        return [dict(t) for t in self.transfers if from_block <= t["block"] <= hi
                and (fs is None or t["frm"] in fs) and (ts is None or t["to"] in ts)]

    def get_launch(self, token):
        self.calls["launch"] += 1
        return {"block": LAUNCH, "curve": CURVE, "deployer": DEV, "tx": "0xlaunch"} if token == TOKEN else None

    def patch(self):
        st = ExitStack()
        p = lambda name, fn: st.enter_context(mock.patch.object(ch, name, side_effect=fn))
        p("block_at_time", self.block_at_time)
        p("block_number", self.head)
        p("block_timestamps", lambda blocks: {b: T0 + b * BLOCK_SECONDS for b in blocks})
        p("get_token_transfers", self.get_token_transfers)
        p("get_launch", self.get_launch)
        p("excluded_addresses", lambda curve: {CURVE, ZERO_, rw.DEAD})
        p("is_contract_at", lambda addrs, block: {a: a in self.contracts for a in addrs})
        p("token_decimals", lambda t: 18)
        p("token_supply", lambda t: 10 ** 9 - 50)                     # 50 сожжено через burn()
        p("token_balance", lambda t, a: 100 if a == rw.DEAD else 0)
        return st


ZERO_ = rw.ZERO


class TestRules(unittest.TestCase):

    def test_time_weighted_half_day(self):
        # купил в 12:00 и держал до конца суток — половина веса; A отдал 400 в 06:00
        trs = FakeChain().transfers
        ts_of = {t["block"]: T0 + t["block"] * BLOCK_SECONDS for t in trs}
        w = rw.time_weights(trs, DAY0, blk(DAY_END), ts_of, DAY_START, DAY_END)
        self.assertEqual(w[B], 500)                                   # 1000 * 12 ч / 24 ч
        self.assertEqual(w[A], 1000 * 6 // 24 + 600 * 18 // 24)       # 250 + 450 = 700
        self.assertEqual(w[C], 300)                                   # 400 * 18 / 24
        self.assertEqual(w[DEV], (5000 * 15 + 4900 * 9) // 24)       # сжёг 100 в 15:00

    def test_balance_before_day_and_sold_before(self):
        trs = [{"frm": ZERO_, "to": A, "amount": 10, "block": 5, "log_index": 0},
               {"frm": A, "to": B, "amount": 10, "block": 6, "log_index": 1}]   # A продал всё до начала суток
        w = rw.time_weights(trs, DAY0, blk(DAY_END), {}, DAY_START, DAY_END)
        self.assertNotIn(A, w)
        self.assertEqual(w[B], 10)

    def test_exclusions(self):
        weights = {A: 7, DEV: 50, CURVE: 99, rw.DEAD: 5, ZERO_: 3, CONTRACT: 9, B: 0}
        self.assertEqual(rw.eligible(weights, {DEV, CURVE}, {CONTRACT}), {A: 7})

    def test_pick_deterministic_and_verify(self):
        parts = {A: 700, B: 500, C: 300}
        h = "0x" + "11" * 32
        a, b = rw.pick(parts, h), rw.pick(dict(reversed(list(parts.items()))), h)
        self.assertEqual(a, b)
        rec = {"list_hash": a["list_hash"], "blockhash": h, "winner": a["winner"], "total_weight": a["total"], "r": a["r"]}
        self.assertTrue(rw.verify(rec, parts)["ok"])
        self.assertFalse(rw.verify(rec, dict(parts, **{A: 701}))["ok"])

    def test_find_burns(self):
        trs = [{"frm": DEV, "to": rw.DEAD, "amount": 5}, {"frm": DEV, "to": ZERO_, "amount": 7},
               {"frm": A, "to": rw.DEAD, "amount": 9}, {"frm": DEV, "to": B, "amount": 1},
               {"frm": DEV, "to": ZERO_, "amount": 0}]
        self.assertEqual([t["amount"] for t in rw.find_burns(trs, [DEV.upper().replace("0X", "0x")])], [5, 7])

    def test_match_payouts(self):
        after = rw.draw_time(DAY)
        draws = [{"day": DAY, "winner": A, "payout_tx": None}, {"day": "2026-10-05", "winner": A, "payout_tx": None}]
        trs = [{"frm": DEV, "to": A, "amount": 1, "block": 1, "tx": "0xearly", "log_index": 0},     # до розыгрыша
               {"frm": B, "to": A, "amount": 2, "block": 2, "tx": "0xnotdev", "log_index": 0},     # не разработчик
               {"frm": DEV, "to": A, "amount": 3, "block": 3, "tx": "0xpay1", "log_index": 0},
               {"frm": DEV, "to": A, "amount": 4, "block": 4, "tx": "0xpay2", "log_index": 0}]
        ts_of = {1: after - 1, 2: after + 1, 3: after + 2, 4: rw.draw_time("2026-10-05") + 5}
        m = rw.match_payouts(draws, trs, [DEV], ts_of)
        self.assertEqual({d: t["tx"] for d, t in m.items()}, {DAY: "0xpay1", "2026-10-05": "0xpay2"})
        self.assertEqual(rw.match_payouts(draws, trs, [DEV], ts_of, {("0xpay1", 0), ("0xpay2", 0)}), {})

    def test_total_burned_and_next_burn(self):
        self.assertEqual(rw.total_burned(1000, 950, 30), 80)
        self.assertEqual(rw.next_burn(DAY_START, 12), DAY_START + 12 * 3600)
        self.assertEqual(rw.next_burn(DAY_START + 11 * 3600, 12), DAY_START + 12 * 3600)
        self.assertEqual(rw.next_burn(DAY_START + 12 * 3600, 12), DAY_END)
        self.assertEqual(rw.next_burn(DAY_START + 13 * 3600, 6), DAY_START + 18 * 3600)


class TestService(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RewardsStore(os.path.join(self.tmp.name, "draw.db"))
        self.cfg = {"enabled": True, "token": TOKEN, "dev_wallets": [DEV], "invalid_dev_wallets": [],
                    "burn_interval_hours": 12.0, "start_day": DAY}
        self.chain = FakeChain()
        self.st = self.chain.patch()
        self.st.__enter__()

    def tearDown(self):
        self.st.__exit__(None, None, None)
        self.store.close()
        self.tmp.cleanup()

    def test_draw_weights_exclusions_and_idempotency(self):
        row = rs.run_draw(self.store, self.cfg, DAY, now=rw.draw_time(DAY))
        self.assertEqual(self.store.participants(DAY), {A: 700, B: 500, C: 300})   # без DEV, кривой, контракта, dead
        self.assertEqual((row["status"], row["participants"], row["total_weight"]), ("done", 3, 1500))
        self.assertEqual((row["start_block"], row["end_block"]), (DAY0, blk(DAY_END)))
        self.assertEqual(row["seed_block"], blk(rw.seed_time(DAY)))
        self.assertTrue(rs.verify_json(self.store, DAY)["ok"])
        # повтор: ничего не пересчитывается и не дублируется
        self.assertEqual(rs.run_draw(self.store, self.cfg, DAY, now=rw.draw_time(DAY)), row)
        self.assertEqual(self.chain.calls["seed"], 1)
        self.assertEqual(len(self.store.draws()), 1)

    def test_recompute_without_db_is_deterministic(self):
        launch = rs.launch_info(None, TOKEN)
        a, pa, _ = rs.compute_day(TOKEN, DAY, [DEV], launch, 18)
        b, pb, _ = rs.compute_day(TOKEN, DAY, [DEV], launch, 18)
        a.pop("created_at"); b.pop("created_at")
        self.assertEqual((a, pa), (b, pb))

    def test_timestamps_from_logs(self):
        # RPC отдал blockTimestamp в логах — отдельных запросов времени блоков нет, результат тот же
        launch = rs.launch_info(None, TOKEN)
        a, pa, _ = rs.compute_day(TOKEN, DAY, [DEV], launch, 18)
        plain = self.chain.get_token_transfers
        with_ts = lambda *a, **k: [dict(t, ts=T0 + t["block"] * BLOCK_SECONDS) for t in plain(*a, **k)]
        with mock.patch.object(ch, "get_token_transfers", side_effect=with_ts), \
             mock.patch.object(ch, "block_timestamps", side_effect=AssertionError("extra RPC")):
            b, pb, _ = rs.compute_day(TOKEN, DAY, [DEV], launch, 18)
        a.pop("created_at"); b.pop("created_at")
        self.assertEqual((a, pa), (b, pb))

    def test_not_ready(self):
        with self.assertRaises(rs.NotReady):
            rs.run_draw(self.store, self.cfg, DAY, now=rw.draw_time(DAY) - 1)
        self.chain.head_ts = rw.seed_time(DAY) - 1                     # seed-блока ещё нет
        with self.assertRaises(rs.NotReady):
            rs.run_draw(self.store, self.cfg, DAY, now=rw.draw_time(DAY))
        self.assertIsNone(self.store.get_draw(DAY))

    def test_zero_participants(self):
        self.cfg["dev_wallets"] = [DEV, A, B, C]                       # все холдеры — разработчик
        row = rs.run_draw(self.store, self.cfg, DAY, now=rw.draw_time(DAY))
        self.assertEqual((row["status"], row["participants"], row["winner"], row["seed_block"]), ("no_eligible", 0, None, None))
        self.assertEqual(self.chain.calls["seed"], 0)                  # seed не запрашивается
        self.assertTrue(rs.verify_json(self.store, DAY)["ok"])
        self.assertEqual(rs.draw_json(row)["payout_status"], "no_winner")

    def test_burns_detected_once(self):
        self.assertEqual(rs.check_burns(self.store, self.cfg), 1)
        self.assertEqual(rs.check_burns(self.store, self.cfg), 0)      # курсор: повтор не дублирует
        self.chain.head_ts += 1000                                     # цепь ушла вперёд на 100 блоков
        self.chain.add(DEV, ZERO_, 50, self.chain.head() - 50)         # burn() -> Transfer на 0x0
        self.chain.add(A, rw.DEAD, 7, self.chain.head() - 50)          # не разработчик — не считается
        self.assertEqual(rs.check_burns(self.store, self.cfg), 1)
        self.assertEqual(self.store.burned_total(TOKEN), (150, 2))
        self.store.set_meta("burn_cursor:" + TOKEN, LAUNCH - 1)         # сбой курсора: перечитали всё — без дублей
        self.assertEqual(rs.check_burns(self.store, self.cfg), 0)
        self.assertEqual(self.store.burned_total(TOKEN), (150, 2))
        last = self.store.burns(TOKEN, 1)[0]
        self.assertEqual((last["amount"], last["to_addr"]), (50, ZERO_))

    def test_dev_wallets_change_rescans(self):
        rs.check_burns(self.store, self.cfg)
        self.chain.add(DEV2, rw.DEAD, 9, 30)
        self.assertEqual(rs.check_burns(self.store, self.cfg), 0)      # DEV2 ещё не разработчик
        self.cfg["dev_wallets"] = [DEV, DEV2]
        self.assertEqual(rs.check_burns(self.store, self.cfg), 2)      # набор поменялся — пересобрано с запуска
        self.assertEqual(self.store.burned_total(TOKEN), (109, 2))

    def test_payout_detected_once(self):
        row = rs.run_draw(self.store, self.cfg, DAY, now=rw.draw_time(DAY))
        win = row["winner"]
        self.assertEqual(rs.check_payouts(self.store, self.cfg), 0)    # выплаты нет
        self.chain.add(DEV, win, 11, blk(DAY_END) + 10)                # 00:01:40 — раньше 00:05, не выплата
        self.chain.add(OUTSIDER, win, 12, blk(rw.draw_time(DAY)) + 5)  # не с DEV_WALLETS
        self.assertEqual(rs.check_payouts(self.store, self.cfg), 0)
        pay = self.chain.add(DEV, win, 25, blk(rw.draw_time(DAY)) + 30)
        self.chain.add(DEV, win, 26, blk(rw.draw_time(DAY)) + 40)
        self.assertEqual(rs.check_payouts(self.store, self.cfg), 1)
        self.assertEqual(rs.check_payouts(self.store, self.cfg), 0)
        row = self.store.get_draw(DAY)
        self.assertEqual((row["payout_tx"], row["payout_amount"], row["payout_from"]), (pay["tx"], 25, DEV))
        self.assertEqual(rs.draw_json(row)["payout_status"], "paid")

    def test_supply_and_status(self):
        rs.check_burns(self.store, self.cfg)
        rs.refresh_supply(self.store, self.cfg)
        rs.decimals(self.store, TOKEN)
        rs.run_draw(self.store, self.cfg, DAY, now=rw.draw_time(DAY))
        st = rs.status_json(self.store, self.cfg, now=DAY_END + 3600)
        self.assertEqual(st["total_burned"]["amount"], 50 + 100)       # minted - totalSupply + dead
        self.assertEqual((st["burned_by_dev"]["amount"], st["burned_by_dev"]["count"]), (100, 1))
        self.assertEqual(st["last_burn"]["amount"], 100)
        self.assertEqual(st["next_burn"], "2026-10-05T12:00:00+00:00")
        self.assertEqual(st["next_draw"], "2026-10-06T00:05:00+00:00")
        self.assertEqual((st["last_draw"]["day"], st["participants_last"], st["last_draw"]["payout_status"]), (DAY, 3, "pending"))
        self.assertAlmostEqual(st["last_draw"]["chance"], st["last_draw"]["weight"] / 1500)

    def test_scheduler_draws_pending_and_survives_errors(self):
        sch = rs.Scheduler(self.store, self.cfg)
        with mock.patch.object(rs, "refresh_supply", side_effect=RuntimeError("rpc down")):
            sch.tick(now=rw.draw_time(DAY) + 60)                       # сапплай упал — розыгрыш и сжигания прошли
        self.assertIsNotNone(self.store.get_draw(DAY))
        self.assertEqual(self.store.burned_total(TOKEN)[1], 1)
        with mock.patch.object(rs, "run_draw", side_effect=RuntimeError("boom")):
            sch.tick(now=rw.draw_time("2026-10-05") + 60)               # ошибка розыгрыша не роняет поток
        self.assertIsNone(self.store.get_draw("2026-10-05"))

    def test_pending_days_start(self):
        self.cfg["start_day"] = None
        now = rw.draw_time(DAY) + 60
        self.assertEqual(rs.pending_days(self.store, self.cfg, now), [])           # первый запуск: с сегодняшних суток
        self.assertEqual(rs.pending_days(self.store, self.cfg, now + 86400), ["2026-10-05"])


class TestRewardsAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown(); cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"REWARDS_ENABLED": "true", "REWARDS_TOKEN": TOKEN, "DEV_WALLETS": DEV,
                                                "DRAW_DB_PATH": os.path.join(self.tmp.name, "d.db")})
        self.env.start()
        self.chain = FakeChain()
        self.st = self.chain.patch()
        self.st.__enter__()
        store = server.rewards_store()
        cfg = rs.config()
        rs.run_draw(store, cfg, DAY, now=rw.draw_time(DAY))
        rs.check_burns(store, cfg)

    def tearDown(self):
        self.st.__exit__(None, None, None)
        with server._stores_lock:
            for st in server._rw_stores.values():
                st.close()
            server._rw_stores.clear()
        self.env.stop(); self.tmp.cleanup()

    def req(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_read_endpoints(self):
        code, st = self.req("/api/rewards/status")
        self.assertEqual((code, st["enabled"], st["token"], st["dev_wallets"]), (200, True, TOKEN, [DEV]))
        self.assertEqual((st["last_draw"]["day"], st["participants_last"]), (DAY, 3))
        code, hist = self.req("/api/rewards/history?limit=5")
        self.assertEqual((code, [d["day"] for d in hist["draws"]], len(hist["burns"])), (200, [DAY], 1))
        code, parts = self.req(f"/api/rewards/{DAY}/participants")
        self.assertEqual((code, [p["address"] for p in parts["participants"]]), (200, sorted([A, B, C])))
        self.assertEqual(sum(p["weight"] for p in parts["participants"]), parts["total_weight"])
        code, v = self.req(f"/api/rewards/{DAY}/verify")
        self.assertEqual((code, v["ok"], v["inputs"]["seed_block"]), (200, True, blk(rw.seed_time(DAY))))
        self.assertEqual(self.req("/api/rewards/2026-01-01/verify")[0], 404)
        self.assertEqual(self.req("/api/rewards/nope")[0], 404)

    def test_disabled(self):
        with mock.patch.dict(os.environ, {"REWARDS_ENABLED": "false"}):
            self.assertEqual(self.req("/api/rewards/status"), (200, {"enabled": False}))
            self.assertEqual(self.req("/api/rewards/history")[0], 404)
            self.assertIsNone(server.start_rewards_scheduler())


if __name__ == "__main__":
    unittest.main()
