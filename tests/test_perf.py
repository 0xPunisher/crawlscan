"""Скорость и стабильность (ветка perf): кэш истории переводов между сканами (TRANSFER_CACHE_ENABLED),
свой лимит фоновой работы (BACKGROUND_RPS) и уступка живым сканам, MAX_CONCURRENT из env. Без сети."""
import os, sys, threading, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakes
from fakes import ch, engine
from chains import priority
import early
import rewards_service as rs
import server

KEYS = ("holders", "holders_total", "circulating", "operators", "links", "packs", "score", "band", "parts",
        "gates", "metrics", "headline", "reason", "rug", "unread", "reserve", "reserve_ok")
NEW = [fakes.wallet(i) for i in range(20, 24)]      # новые покупатели в хвосте


class Chain:
    """Подставная история токена: get_token_transfers по окну блоков (и фильтрам), голова, totalSupply."""

    def __init__(self):
        self.trs = fakes.transfers()
        self.head = max(t["block"] for t in self.trs) + 30
        self.calls = []                              # (from_block, to_block) запросов истории

    def add(self, frm, to, amount, block=None):
        block = self.head + 1 if block is None else block
        idx = 1 + max((t["log_index"] for t in self.trs if t["block"] == block), default=0)
        self.trs.append({"frm": frm, "to": to, "amount": amount, "tx": f"0xt{len(self.trs)}", "block": block,
                         "log_index": idx})
        self.head = max(self.head, block + 5)

    def supply(self):
        z = fakes.ZERO
        return sum(t["amount"] for t in self.trs if t["frm"] == z) - sum(t["amount"] for t in self.trs if t["to"] == z)

    def get_token_transfers(self, token, from_block, to_block=None, frm=None, to=None):
        hi = self.head if to_block is None else to_block
        self.calls.append((from_block, hi))
        pick = lambda a: None if a is None else {a} if isinstance(a, str) else set(a)
        f, t_ = pick(frm), pick(to)
        out = [dict(t) for t in self.trs if from_block <= t["block"] <= hi
               and (f is None or t["frm"] in f) and (t_ is None or t["to"] in t_)]
        return sorted(out, key=lambda t: (t["block"], t["log_index"]))

    def patches(self, stack):
        p = lambda name, **kw: stack.enter_context(mock.patch.object(ch, name, **kw))
        p("get_token_transfers", side_effect=self.get_token_transfers)
        p("block_number", side_effect=lambda: self.head)
        p("token_supply", side_effect=lambda t: self.supply())
        return stack


def summary(res):
    return {k: res[k] for k in KEYS}


class TransferCacheBase(unittest.TestCase):

    def setUp(self):
        ch.tcache_clear()
        self.chain = Chain()

    def tearDown(self):
        ch.tcache_clear()

    def scan(self, cache):
        """Скан с кэшем или без; кэш истории кошельков движка очищается, чтобы сравнивать только переводы."""
        with fakes.patched() as st:
            self.chain.patches(st)
            st.enter_context(mock.patch.object(ch, "TRANSFER_CACHE_ENABLED", cache))
            self.chain.calls.clear()
            events = []
            res = engine.scan(fakes.TOKEN, events.append)
        # wallet_flag идут в порядке готовности потоков истории — сравниваем события без номера и времени, как набор
        return res, sorted(repr(sorted((k, v) for k, v in e.items() if k not in ("t", "i"))) for e in events)

    def assert_same(self):
        """Скан без кэша и скан с кэшем (тёплым — только хвост) дают одно и то же."""
        off, ev_off = self.scan(False)
        on, ev_on = self.scan(True)
        self.assertEqual(summary(on), summary(off))
        self.assertEqual(ev_on, ev_off)
        return off, on


class TestTransferCacheSameResult(TransferCacheBase):

    def test_cold_cache_same_and_full_read(self):
        off, on = self.assert_same()
        self.assertEqual(self.chain.calls, [(fakes.LAUNCH_BLOCK, self.chain.head)])   # холодный — полное чтение
        self.assertEqual(ch.TCACHE_STATS["miss"] >= 1, True)
        self.assertIn(fakes.TOKEN, ch._TCACHE)

    def warm(self):
        self.scan(True)
        self.assertIn(fakes.TOKEN, ch._TCACHE)
        return self.chain.head

    def assert_tail_only(self, prev_head):
        """Тёплый скан читал только хвост: с prev_head − LAG + 1, а не с запуска."""
        self.assertEqual(self.chain.calls, [(prev_head - ch.TRANSFER_CACHE_LAG + 1, self.chain.head)])

    def test_new_buys_in_tail(self):
        h = self.warm()
        for i, w in enumerate(NEW):
            self.chain.add(fakes.CURVE, w, fakes.SUPPLY // 100 * (3 + i))
        self.chain.add(fakes.CURVE, fakes.WALLETS[4], fakes.SUPPLY // 50)       # докупка старого холдера
        off, on = self.assert_same()
        self.assert_tail_only(h)
        self.assertTrue(set(NEW) & {x["wallet"] for x in on["holders"]})

    def test_sale(self):
        h = self.warm()
        w = fakes.WALLETS[0]
        bal = sum(t["amount"] for t in self.chain.trs if t["to"] == w)
        self.chain.add(w, fakes.CURVE, bal * 9 // 10)
        off, on = self.assert_same()
        self.assert_tail_only(h)
        self.assertTrue(next(x for x in on["holders"] if x["wallet"] == w)["signals"]["sold"])

    def test_transfer_between_holders(self):
        h = self.warm()
        self.chain.add(fakes.WALLETS[5], fakes.WALLETS[7], fakes.SUPPLY // 1000)
        off, on = self.assert_same()
        self.assert_tail_only(h)
        pair = {fakes.WALLETS[5], fakes.WALLETS[7]}
        self.assertTrue(any({l["a"], l["b"]} == pair for l in on["links"]))

    def test_sold_to_new_wallet_and_burn(self):
        h = self.warm()
        self.chain.add(fakes.WALLETS[1], NEW[0], fakes.SUPPLY // 200)           # перевод новому кошельку
        self.chain.add(fakes.WALLETS[2], fakes.ZERO, fakes.SUPPLY // 1000)      # burn(): totalSupply меньше
        self.assert_same()
        self.assert_tail_only(h)

    def test_late_log_near_head_is_reread(self):
        """Нода отдала не все логи у головы: перевод в уже прочитанном блоке (в пределах LAG) появился позже."""
        h = self.warm()
        self.chain.add(fakes.CURVE, NEW[1], fakes.SUPPLY // 40, block=h - 3)
        off, on = self.assert_same()
        self.assertIn(NEW[1], {x["wallet"] for x in on["holders"]})

    def test_several_rounds(self):
        self.warm()
        for i in range(3):
            self.chain.add(fakes.CURVE, NEW[i], fakes.SUPPLY // 100 * (5 + i))
            self.chain.add(fakes.WALLETS[i + 3], fakes.CURVE, fakes.SUPPLY // 1000)
            self.assert_same()
        self.assertGreaterEqual(ch.TCACHE_STATS["hit"], 3)


class TestTransferCacheIntegrity(TransferCacheBase):

    def full_read(self):
        return (fakes.LAUNCH_BLOCK, self.chain.head) in self.chain.calls

    def test_corrupted_entry_full_read(self):
        self.scan(True)
        e = ch._TCACHE[fakes.TOKEN]
        e["transfers"] = e["transfers"][:-2]          # длина не совпала с записанной
        off, ev = self.scan(False)
        on, ev_on = self.scan(True)
        self.assertTrue(self.full_read())
        self.assertEqual(summary(on), summary(off))
        self.assertEqual(len(ch._TCACHE[fakes.TOKEN]["transfers"]), len(self.chain.trs))   # переписан целиком

    def test_unordered_entry_full_read(self):
        self.scan(True)
        e = ch._TCACHE[fakes.TOKEN]
        trs = list(e["transfers"])
        trs[1], trs[2] = trs[2], trs[1]
        e["transfers"] = trs
        e["last"] = ch._key(trs[-1])
        self.scan(True)
        self.assertTrue(self.full_read())

    def test_incomplete_history_supply_mismatch_full_read(self):
        """Склейка не сходится с totalSupply (пропал перевод в истории) — полное чтение."""
        self.scan(True)
        e = ch._TCACHE[fakes.TOKEN]
        trs = [t for t in e["transfers"] if t["frm"] != fakes.ZERO]   # потеряли минт, запись при этом цела
        e.update(transfers=trs, n=len(trs), last=ch._key(trs[-1]))
        self.chain.add(fakes.CURVE, NEW[0], fakes.SUPPLY // 100)
        off, _ = self.scan(False)
        on, _ = self.scan(True)
        self.assertTrue(self.full_read())
        self.assertEqual(summary(on), summary(off))

    def test_inconsistent_full_read_not_cached(self):
        with mock.patch.object(self.chain, "supply", return_value=fakes.SUPPLY + 1):
            self.scan(True)
        self.assertNotIn(fakes.TOKEN, ch._TCACHE)

    def test_other_launch_block_not_used(self):
        self.scan(True)
        self.assertIsNone(ch._tcache_get(fakes.TOKEN, fakes.LAUNCH_BLOCK + 1))
        self.assertNotIn(fakes.TOKEN, ch._TCACHE)

    def test_head_behind_cache_full_read(self):
        """Нода отдала голову ниже прочитанной (отстала) — кэш не используется, полное чтение."""
        self.scan(True)
        self.chain.head -= 3
        self.scan(True)
        self.assertTrue(self.full_read())


class TestTransferCacheLRU(unittest.TestCase):

    def setUp(self):
        ch.tcache_clear()

    def tearDown(self):
        ch.tcache_clear()

    @staticmethod
    def trs(n, block=100):
        return [{"frm": "0xa", "to": "0xb", "amount": 1, "tx": f"0x{i}", "block": block + i, "log_index": 0}
                for i in range(n)]

    def test_evicts_least_recently_used(self):
        with mock.patch.object(ch, "TRANSFER_CACHE_MAX", 100):
            ch._tcache_put("t1", 100, self.trs(40), 1000)
            ch._tcache_put("t2", 100, self.trs(40), 1000)
            self.assertIsNotNone(ch._tcache_get("t1", 100))          # t1 использован — t2 теперь старший
            ch._tcache_put("t3", 100, self.trs(40), 1000)
            self.assertEqual(list(ch._TCACHE), ["t1", "t3"])
            self.assertEqual(ch._TCACHE_SIZE[0], 80)
            ch._tcache_put("t4", 100, self.trs(70), 1000)
            self.assertEqual(list(ch._TCACHE), ["t4"])
            self.assertEqual(ch._TCACHE_SIZE[0], 70)

    def test_too_big_not_cached(self):
        with mock.patch.object(ch, "TRANSFER_CACHE_MAX", 10):
            ch._tcache_put("t1", 100, self.trs(5), 1000)
            ch._tcache_put("big", 100, self.trs(11), 1000)
            self.assertEqual(list(ch._TCACHE), ["t1"])
            self.assertEqual(ch._TCACHE_SIZE[0], 5)

    def test_replace_keeps_size(self):
        ch._tcache_put("t1", 100, self.trs(5), 1000)
        ch._tcache_put("t1", 100, self.trs(8), 1010)
        self.assertEqual(ch._TCACHE_SIZE[0], 8)
        ch._tcache_put("t1", 100, self.trs(3), 1005)                   # старее записанной — не затирает
        self.assertEqual(ch._TCACHE["t1"]["n"], 8)

    def test_default_limit(self):
        self.assertEqual(ch.TRANSFER_CACHE_MAX, 200_000)


class TestEarlyFromCache(TransferCacheBase):
    """remember_scan при включённом кэше хранит только отметку скана, история для early buyers — из кэша."""

    def test_scan_history_from_cache(self):
        with fakes.patched() as st:
            self.chain.patches(st)
            st.enter_context(mock.patch.object(ch, "TRANSFER_CACHE_ENABLED", True))
            engine.scan(fakes.TOKEN)
            self.assertIsNone(ch._SCAN[fakes.TOKEN][2])
            launch, trs, supply = ch.scan_history(fakes.TOKEN)
            self.assertIs(trs, ch._TCACHE[fakes.TOKEN]["transfers"])
            self.assertEqual(len(trs), len(self.chain.trs))
            self.assertEqual(supply, fakes.SUPPLY)
            ch.tcache_clear()                                            # вытеснен — early читает сам
            self.assertIsNone(ch.scan_history(fakes.TOKEN))

    def test_scan_history_without_cache_as_before(self):
        with fakes.patched() as st:
            self.chain.patches(st)
            engine.scan(fakes.TOKEN)
            self.assertEqual(len(ch._SCAN[fakes.TOKEN][2]), len(self.chain.trs))
            self.assertEqual(ch._TCACHE, {})


class TestSwitchesOff(TransferCacheBase):

    def test_cache_off_by_default(self):
        if "TRANSFER_CACHE_ENABLED" not in os.environ:
            self.assertFalse(ch.TRANSFER_CACHE_ENABLED)

    def test_cache_off_reads_full_every_time(self):
        self.scan(False)
        self.chain.add(fakes.CURVE, NEW[0], fakes.SUPPLY // 100)
        self.scan(False)
        self.assertEqual(self.chain.calls, [(fakes.LAUNCH_BLOCK, self.chain.head)])
        self.assertEqual(ch._TCACHE, {})

    def test_max_concurrent_env(self):
        self.assertEqual(server._env_int("MAX_CONCURRENT_X", 3), 3)
        with mock.patch.dict(os.environ, {"MAX_CONCURRENT_X": "5"}):
            self.assertEqual(server._env_int("MAX_CONCURRENT_X", 3), 5)
        with mock.patch.dict(os.environ, {"MAX_CONCURRENT_X": "zero"}):
            self.assertEqual(server._env_int("MAX_CONCURRENT_X", 3), 3)
        if "MAX_CONCURRENT" not in os.environ:
            self.assertEqual(server.MAX_CONCURRENT, 3)

    def test_background_rps_zero_is_old_behaviour(self):
        with mock.patch.dict(os.environ, {"BACKGROUND_RPS": "0"}):
            self.assertEqual(priority.background_rps(), 0)
            self.assertEqual(priority.background_scale(8), 1.0)
            with priority.background():
                self.assertFalse(priority.is_background())               # награды / early — не фон, как раньше
        with mock.patch.dict(os.environ, {"BACKGROUND_RPS": ""}):
            self.assertEqual(priority.background_rps(), 2.0)
            self.assertEqual(priority.background_scale(8), 4.0)
        with mock.patch.dict(os.environ, {"BACKGROUND_RPS": "nope"}):
            self.assertEqual(priority.background_rps(), 2.0)


class TestBackground(unittest.TestCase):

    def test_throttle_rate_and_sleep_outside_lock(self):
        th = priority.Throttle(20)                      # 50 мс на слот
        t0 = time.time()
        for _ in range(5):
            th.wait()
        self.assertGreaterEqual(time.time() - t0, 0.19)
        th = priority.Throttle(1)
        th.wait()                                        # первый слот — сразу, следующий через 1 с
        sleeper = threading.Thread(target=th.wait)
        sleeper.start()
        time.sleep(0.1)
        self.assertTrue(sleeper.is_alive())
        self.assertTrue(th.lock.acquire(timeout=0.05))   # спящий не держит замок
        th.lock.release()
        sleeper.join(3)
        self.assertEqual(priority.Throttle(0).gap, 0.0)

    def test_background_own_limit_live_not_throttled(self):
        main, bg = priority.Throttle(1000), priority.Throttle(10)
        t0 = time.time()
        for _ in range(5):
            priority.rate_limit(main, bg)                # живой поток: только общий лимит
        self.assertLess(time.time() - t0, 0.1)
        out = []

        def run():
            s = time.time()
            for _ in range(5):
                priority.rate_limit(main, bg)
            out.append(time.time() - s)

        t = threading.Thread(target=run, name=f"{priority.BG}-t")
        t.start(); t.join(5)
        self.assertGreaterEqual(out[0], 0.39)            # 10 RPS: 5 запросов — не меньше 0.4 с

    def test_background_yields_to_live_scan(self):
        main, bg = priority.Throttle(1000), priority.Throttle(1000)
        done, started = [], threading.Event()

        def run():
            with priority.background():
                started.set()
                priority.rate_limit(main, bg)
                done.append(time.time())

        with mock.patch.dict(os.environ, {"BACKGROUND_RPS": "2"}):
            cm = priority.live()
            cm.__enter__()
            t = threading.Thread(target=run)
            t.start()
            started.wait(2)
            time.sleep(0.2)
            self.assertEqual(done, [])                   # ждёт конца живого скана
            t_end = time.time()
            cm.__exit__(None, None, None)
            t.join(3)
        self.assertGreaterEqual(done[0], t_end)

    def test_adapter_rate_limit_uses_background_limiter(self):
        for a in (ch, fakes.engine.sol):
            with mock.patch.object(a._BG_LIMIT, "wait") as bgw, mock.patch.object(a._LIMIT, "wait") as mw:
                a._rate_limit()
                bgw.assert_not_called()
                t = threading.Thread(target=a._rate_limit, name=f"{priority.BG}-x")
                t.start(); t.join(2)
                bgw.assert_called_once()
                self.assertEqual(mw.call_count, 2)

    def test_rewards_scheduler_is_background(self):
        seen = []
        sch = rs.Scheduler(store=None, cfg={"token": "x", "dev_wallets": [], "invalid_dev_wallets": []})
        with mock.patch.dict(os.environ, {"BACKGROUND_RPS": "2"}), \
                mock.patch.object(sch, "tick", side_effect=lambda: (seen.append(priority.is_background()), sch.stop())):
            sch.run()
        self.assertEqual(seen, [True])

    def test_background_scan_budget_scaled(self):
        deadlines = []
        real = engine._RankGate

        def gate(width, slot_s=None):
            deadlines.append(slot_s)
            return real(width, slot_s)

        with fakes.patched(), mock.patch.object(engine, "_RankGate", side_effect=gate), \
                mock.patch.object(ch, "HISTORY_PARALLEL", 1), mock.patch.dict(os.environ, {"BACKGROUND_RPS": "2"}):
            engine.scan(fakes.TOKEN)
            engine._HIST.clear()
            with priority.background():
                engine.scan(fakes.TOKEN)
        live, bg = deadlines
        scale = priority.background_scale(ch.RPS) if ch.RPS > 2 else 1.0
        self.assertAlmostEqual(bg / live, scale, delta=0.05 * scale)

    def test_early_background_without_scan_history(self):
        with mock.patch.object(ch, "scan_history", return_value=None):
            self.assertTrue(early.background("robinhood", fakes.TOKEN))
        with mock.patch.object(ch, "scan_history", return_value=({}, [], 1)):
            self.assertFalse(early.background("robinhood", fakes.TOKEN))
        self.assertFalse(early.background("solana", "x"))               # у Solana истории скана нет — как раньше

    def test_early_endpoint_context(self):
        seen = []

        def get(token, flags=None):
            seen.append((priority.is_background(), priority.live_count()))
            return {"token": token, "chain": "robinhood", "available": False, "reason": "x"}

        h = mock.MagicMock()
        h.path = f"/api/early?token={fakes.TOKEN}"
        with mock.patch.object(early, "get", side_effect=get), mock.patch.object(server, "record_early"), \
                mock.patch.dict(os.environ, {"BACKGROUND_RPS": "2"}):
            with mock.patch.object(early, "background", return_value=True):
                server.H.do_GET(h)
            with mock.patch.object(early, "background", return_value=False):
                server.H.do_GET(h)
            with mock.patch.dict(os.environ, {"BACKGROUND_RPS": "0"}), \
                    mock.patch.object(early, "background", return_value=True):
                server.H.do_GET(h)
        self.assertEqual(seen, [(True, 0), (False, 1), (False, 1)])


if __name__ == "__main__":
    unittest.main()
