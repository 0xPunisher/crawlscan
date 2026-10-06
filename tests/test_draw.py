"""Тесты розыгрыша среди холдеров: правила (draw.py), хранилище, оркестрация с подставной сетью, API."""
import json, os, tempfile, threading, time, unittest, urllib.error, urllib.request
from contextlib import ExitStack
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

import fakes  # noqa: F401  (запрещает настоящие RPC адаптера Robinhood, ставит пути)
import draw as dr
import draw_service as ds
from draw_store import Store
from chains import solana as sol
import server

MINT = sol.b58encode(bytes([42]) * 32)
W = [sol.b58encode(bytes([60 + i]) * 32) for i in range(6)]       # кошельки (точки на кривой не важны: is_pda подменяем)
POOL = sol.b58encode(bytes([99]) * 32)
DAY = "2026-10-04"
TS = int(datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc).timestamp())
BLOCKHASH = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


def ts_hour(h, day=DAY):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=h, tzinfo=timezone.utc).timestamp()) + 30


class TestRules(unittest.TestCase):

    def test_aggregate_by_owner(self):
        accs = [{"owner": "a", "amount": 5}, {"owner": "a", "amount": "7"}, {"owner": "b", "amount": 0}, {"owner": "c", "amount": 1}]
        self.assertEqual(dr.aggregate(accs), {"a": 12, "c": 1})

    def test_day_weights_average(self):
        # 4 сделанных снимка (остальные часы — простой): a во всех, b в двух, c в одном
        snaps = [{"a": 100, "b": 40}, {"a": 100}, {"a": 100, "b": 40, "c": 3}, {"a": 100}]
        self.assertEqual(dr.day_weights(snaps), {"a": 100, "b": 20})   # c: 3 // 4 = 0 — выбывает
        self.assertEqual(dr.day_weights([]), {})

    def test_threshold(self):
        t = dr.threshold(6, price_usd=0.002, min_usd=10)                # 10 $ / 0.002 $ = 5000 токенов
        self.assertEqual((t["mode"], t["min_raw"]), ("usd", 5000 * 10 ** 6))
        t = dr.threshold(6, price_usd=None, min_usd=10, min_tokens=1000)
        self.assertEqual((t["mode"], t["min_raw"]), ("tokens", 1000 * 10 ** 6))
        t = dr.threshold(6, price_usd=None, min_usd=10, min_tokens=None)
        self.assertEqual((t["mode"], t["min_raw"]), ("none", 0))
        self.assertEqual(dr.apply_threshold({"a": 5000 * 10 ** 6, "b": 4999 * 10 ** 6}, 5000 * 10 ** 6), {"a": 5000 * 10 ** 6})

    def test_only_wallets(self):
        self.assertTrue(dr.is_wallet(dr.SYSTEM_PROGRAM, on_curve=True))
        self.assertTrue(dr.is_wallet(None, on_curve=True))                 # кошелёк без SOL
        self.assertFalse(dr.is_wallet(dr.SYSTEM_PROGRAM, on_curve=False))  # system-owned PDA
        self.assertFalse(dr.is_wallet(sol.PUMP_SWAP, on_curve=False))      # пул
        self.assertFalse(dr.is_wallet(sol.PUMP, on_curve=True))            # аккаунт программы
        self.assertFalse(dr.is_wallet(None, on_curve=False))               # PDA без аккаунта

    def test_pick_deterministic_and_verify(self):
        parts = {W[0]: 300, W[1]: 100, W[2]: 600}
        a, b = dr.pick(parts, BLOCKHASH), dr.pick(dict(reversed(list(parts.items()))), BLOCKHASH)
        self.assertEqual(a, b)                                             # порядок ввода не важен
        self.assertEqual(a["total"], 1000)
        self.assertEqual(a["list_hash"], dr.list_hash(parts))
        self.assertEqual(dr.canonical(parts), json.dumps(sorted([[k, v] for k, v in parts.items()]), separators=(",", ":")))
        # ручной пересчёт: накопленная сумма по адресам в порядке сортировки
        acc, expect = 0, None
        for addr, w in sorted(parts.items()):
            acc += w
            if acc > a["r"]:
                expect = addr; break
        self.assertEqual(a["winner"], expect)
        rec = {"list_hash": a["list_hash"], "blockhash": BLOCKHASH, "winner": a["winner"], "total_weight": 1000, "r": a["r"]}
        self.assertTrue(dr.verify(rec, parts)["ok"])
        self.assertFalse(dr.verify(dict(rec, winner=W[5]), parts)["ok"])          # подменили победителя
        self.assertFalse(dr.verify(rec, dict(parts, **{W[3]: 1}))["ok"])           # подменили список
        self.assertFalse(dr.verify(dict(rec, blockhash="1" * 44), parts)["ok"])    # подменили blockhash

    def test_pick_empty(self):
        self.assertIsNone(dr.pick({}, BLOCKHASH)["winner"])

    def test_times(self):
        self.assertEqual(datetime.fromtimestamp(dr.draw_time(DAY), timezone.utc).isoformat(), "2026-10-05T00:05:00+00:00")
        self.assertEqual(datetime.fromtimestamp(dr.seed_time(DAY), timezone.utc).isoformat(), "2026-10-05T00:01:00+00:00")
        self.assertEqual(dr.hour_key(TS), "2026-10-04T10")


class FakeNet:
    """Подставная сеть: холдеры по часам, владельцы, decimals, цена, seed-блок; считает вызовы."""

    def __init__(self, holders, programs, price=0.001, decimals=6):
        self.holders, self.programs, self.price, self.decimals = holders, programs, price, decimals
        self.calls = {"accounts": 0, "owners": 0, "seed": 0}

    def patch(self):
        st = ExitStack()
        p = lambda name, fn: st.enter_context(mock.patch.object(ds, name, side_effect=fn))
        p("fetch_token_accounts", self._accounts)
        p("owner_programs", self._owners)
        p("token_decimals", lambda mint: self.decimals)
        p("token_price", lambda mint: self.price)
        p("seed_block", self._seed)
        st.enter_context(mock.patch.object(sol, "_post", side_effect=AssertionError("network")))
        st.enter_context(mock.patch.object(sol, "is_pda", side_effect=lambda a: a == POOL))
        return st

    def _accounts(self, mint):
        self.calls["accounts"] += 1
        return [{"owner": o, "amount": v} for o, v in self.holders().items()], 123

    def _owners(self, addrs):
        self.calls["owners"] += 1
        return {a: self.programs.get(a, dr.SYSTEM_PROGRAM) for a in addrs}

    def _seed(self, ts):
        self.calls["seed"] += 1
        return {"slot": 777, "blockhash": BLOCKHASH, "block_time": ts}


class TestService(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "draw.db"))
        self.cfg = {"enabled": True, "mint": MINT, "min_usd": 10.0, "min_tokens": None}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def net(self, **kw):
        state = {"h": 0}
        big = 20_000 * 10 ** 6                                                  # 20 000 токенов = 20 $ при 0.001 $
        def holders():
            h = {W[0]: big, W[1]: big // 4, POOL: 10 ** 15, W[2]: big}
            if state["h"] % 2:                                                  # W[2] держит только в чётные часы
                h.pop(W[2])
            return h
        n = FakeNet(holders, {W[3]: sol.PUMP}, **kw)
        n.state = state
        return n

    def test_snapshot_excludes_non_wallets_and_is_idempotent(self):
        n = self.net()
        with n.patch():
            s1 = ds.take_snapshot(self.store, MINT, now=ts_hour(10))
            s2 = ds.take_snapshot(self.store, MINT, now=ts_hour(10) + 600)      # тот же час — повтора нет
        self.assertTrue(s1["taken"]); self.assertFalse(s2["taken"])
        self.assertEqual(n.calls["accounts"], 1)
        self.assertNotIn(POOL, s1["balances"])                                 # PDA пула исключён
        self.assertEqual(set(s1["balances"]), {W[0], W[1], W[2]})
        self.assertEqual(len(self.store.snapshots_of_day(DAY, MINT)), 1)
        with n.patch():                                                        # владельцы закэшированы: повторно не спрашиваем
            ds.take_snapshot(self.store, MINT, now=ts_hour(11))
        self.assertEqual(n.calls["owners"], 1)

    def test_draw_weights_threshold_winner_and_idempotency(self):
        n = self.net()
        with n.patch():
            for h in range(4):                                                 # 4 снимка из 24 (простой)
                n.state["h"] = h
                ds.take_snapshot(self.store, MINT, now=ts_hour(h))
            with self.assertRaises(ds.NotReady):
                ds.run_draw(self.store, MINT, DAY, self.cfg, now=dr.draw_time(DAY) - 60)
            row = ds.run_draw(self.store, MINT, DAY, self.cfg, now=dr.draw_time(DAY) + 1)
            again = ds.run_draw(self.store, MINT, DAY, self.cfg, now=dr.draw_time(DAY) + 99)
        self.assertEqual(row, again)                                           # повтор не дублирует
        self.assertEqual(n.calls["seed"], 1)
        parts = self.store.participants(DAY)
        big = 20_000 * 10 ** 6
        # W[1]: 5 000 токенов = 5 $ < 10 $ — ниже порога; W[2]: в 2 снимках из 4 -> средний 10 000 = 10 $ — проходит
        self.assertEqual(parts, {W[0]: big, W[2]: big // 2})
        self.assertEqual((row["status"], row["snapshots"], row["participants"]), ("done", 4, 2))
        self.assertEqual((row["threshold_mode"], int(row["threshold_raw"])), ("usd", 10_000 * 10 ** 6))
        self.assertIn(row["winner"], parts)
        self.assertTrue(dr.verify(row, parts)["ok"])
        self.assertTrue(ds.verify_json(self.store, DAY)["ok"])

    def test_no_eligible_holders(self):
        n = self.net(price=1e-9)                                               # цена ничтожна: никто не проходит 10 $
        with n.patch():
            ds.take_snapshot(self.store, MINT, now=ts_hour(5))
            row = ds.run_draw(self.store, MINT, DAY, self.cfg, now=dr.draw_time(DAY) + 1)
        self.assertEqual((row["status"], row["winner"], row["participants"]), ("no_eligible", None, 0))
        self.assertEqual(n.calls["seed"], 0)
        self.assertEqual(ds.draw_json(row)["status_text"], "no eligible holders")
        self.assertEqual(self.store.set_payout(DAY, 1, "SOL", "tx"), "no_winner")  # приз переносится

    def test_threshold_tokens_when_no_price(self):
        n = self.net(price=None)
        with n.patch():
            ds.take_snapshot(self.store, MINT, now=ts_hour(5))
            row = ds.run_draw(self.store, MINT, DAY, dict(self.cfg, min_tokens=6000), now=dr.draw_time(DAY) + 1)
        self.assertEqual(row["threshold_mode"], "tokens")
        self.assertEqual(set(self.store.participants(DAY)), {W[0], W[2]})

    def test_scheduler_tick_survives_errors(self):
        sch = ds.Scheduler(self.store, self.cfg)
        with mock.patch.object(ds, "fetch_token_accounts", side_effect=RuntimeError("boom")), \
                mock.patch.object(ds, "log"):
            sch.tick(now=ts_hour(3))                                           # ошибка снимка не роняет
        self.assertFalse(self.store.has_snapshot(dr.hour_key(ts_hour(3))))


class TestDrawAPI(unittest.TestCase):

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
        self.env = mock.patch.dict(os.environ, {"DRAW_ENABLED": "true", "DRAW_MINT": MINT, "ADMIN_TOKEN": "s3cret",
                                                "DRAW_DB_PATH": os.path.join(self.tmp.name, "d.db")})
        self.env.start()
        store = server.draw_store()
        parts = {W[0]: 300, W[1]: 700}
        res = dr.pick(parts, BLOCKHASH)
        store.save_draw({"day": DAY, "mint": MINT, "status": "done", "snapshots": 24, "list_hash": res["list_hash"],
                         "seed_time": dr.seed_time(DAY), "seed_slot": 777, "blockhash": BLOCKHASH, "r": res["r"],
                         "winner": res["winner"], "winner_weight": res["winner_weight"], "total_weight": res["total"],
                         "participants": 2, "decimals": 6, "price_usd": 0.001, "threshold_mode": "usd",
                         "threshold_raw": 1, "created_at": int(time.time())}, parts)

    def tearDown(self):
        with server._stores_lock:
            for st in server._stores.values():
                st.close()
            server._stores.clear()
        self.env.stop(); self.tmp.cleanup()

    def req(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                   headers={"content-type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_read_endpoints(self):
        code, st = self.req("/api/draw/status")
        self.assertEqual((code, st["enabled"], st["mint"]), (200, True, MINT))
        code, latest = self.req("/api/draw/latest")
        self.assertEqual((code, latest["day"]), (200, DAY))
        code, hist = self.req("/api/draw/history?limit=5")
        self.assertEqual((code, [d["day"] for d in hist["draws"]]), (200, [DAY]))
        code, parts = self.req(f"/api/draw/{DAY}/participants")
        self.assertEqual((code, [p["address"] for p in parts["participants"]]), (200, sorted([W[0], W[1]])))
        code, v = self.req(f"/api/draw/{DAY}/verify")
        self.assertEqual((code, v["ok"]), (200, True))
        self.assertEqual(self.req("/api/draw/2026-01-01/verify")[0], 404)

    def test_payout_admin_only(self):
        body = {"prize_amount": 1.5, "prize_currency": "SOL", "payout_tx": "5xTx"}
        self.assertEqual(self.req(f"/api/draw/{DAY}/payout", body)[0], 403)                                   # без токена
        self.assertEqual(self.req(f"/api/draw/{DAY}/payout", body, {"X-Admin-Token": "wrong"})[0], 403)       # неверный
        code, row = self.req(f"/api/draw/{DAY}/payout", body, {"X-Admin-Token": "s3cret"})
        self.assertEqual((code, row["prize_currency"], row["payout_tx"]), (200, "SOL", "5xTx"))
        code, _ = self.req(f"/api/draw/{DAY}/payout", dict(body, payout_tx="other"), {"X-Admin-Token": "s3cret"})
        self.assertEqual(code, 409)                                                                           # уже выплачено
        with mock.patch.dict(os.environ, {"ADMIN_TOKEN": ""}):
            self.assertEqual(self.req(f"/api/draw/{DAY}/payout", body, {"X-Admin-Token": ""})[0], 403)        # токен не задан

    def test_disabled(self):
        with mock.patch.dict(os.environ, {"DRAW_ENABLED": "false"}):
            self.assertEqual(self.req("/api/draw/status"), (200, {"enabled": False, "mint": MINT}))
            self.assertEqual(self.req("/api/draw/latest")[0], 404)
            self.assertIsNone(server.start_draw_scheduler())                  # планировщик не стартует


if __name__ == "__main__":
    unittest.main()
