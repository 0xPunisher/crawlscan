"""Тесты engine.scan на синтетическом токене (подставной адаптер из tests/fakes.py, без сети)."""
import threading, time, unittest
from unittest import mock

import fakes
from fakes import ch, engine

STAGES = ["launch", "transfers", "entries", "wallets", "links"]
EVENT_KEYS = {"i", "t", "type", "spider", "wallet", "detail"}


class TestScan(unittest.TestCase):

    def scan(self, token=fakes.TOKEN):
        events = []
        res = engine.scan(token, events.append)
        return res, events

    def test_full_scan(self):
        with fakes.patched():
            res, events = self.scan()
        self.assertEqual(res["token"], fakes.TOKEN)
        self.assertEqual(len(res["holders"]), fakes.N_WALLETS)
        self.assertEqual(res["holders_total"], fakes.N_WALLETS)
        self.assertIsInstance(res["score"], int)
        self.assertIn(res["band"], ("CLEAN", "OK", "RISKY", "DANGER"))
        self.assertEqual(res["unread"], [])
        self.assertTrue(res["headline"].startswith(f"{fakes.N_WALLETS} wallets → "))
        self.assertEqual(res["header"]["ticker"], "SYN")               # GT пустой -> имя и тикер из контракта
        # бандл одной транзакцией -> доказанный оператор из BUNDLE кошельков
        top = [o for o in res["operators"] if len(o["wallets"]) > 1]
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["level"], "proven")
        self.assertEqual(sorted(top[0]["wallets"]), sorted(fakes.WALLETS[:fakes.BUNDLE]))
        self.assertTrue(any(g.startswith("soft:") for g in res["gates"]))
        self.assertLessEqual(res["score"], 59)                          # мягкое стоп-правило
        self.assertAlmostEqual(sum(h["share"] for h in res["holders"]), 1.0)  # доли — от оборота

    def test_event_order_and_format(self):
        with fakes.patched():
            _, events = self.scan()
        self.assertEqual([e["i"] for e in events], list(range(len(events))))
        self.assertEqual([e["t"] for e in events], sorted(e["t"] for e in events))
        for e in events:
            self.assertTrue(EVENT_KEYS <= e.keys(), e)
        types = [e["type"] for e in events]
        self.assertEqual([e["detail"] for e in events if e["type"] == "stage"], STAGES)
        self.assertEqual(types[-1], "done")
        self.assertEqual(types.count("done"), 1)
        # порядок блоков: стадии -> пауки -> флаги кошельков -> links -> связи/кластеры -> done
        first = {t: types.index(t) for t in set(types)}
        last = {t: len(types) - 1 - types[::-1].index(t) for t in set(types)}
        self.assertLess(last["spider_move"], first["wallet_flag"])
        self.assertLess(last["wallet_flag"], first["link"])
        self.assertLess(last["link"], first["cluster"])
        moves = [e for e in events if e["type"] == "spider_move"]
        flags = [e for e in events if e["type"] == "wallet_flag"]
        self.assertEqual([e["wallet"] for e in moves], fakes.WALLETS)
        self.assertEqual([e["spider"] for e in moves], [i % engine.SPIDERS for i in range(fakes.N_WALLETS)])
        self.assertEqual(sorted(e["wallet"] for e in flags), sorted(fakes.WALLETS))
        for e in flags:
            self.assertTrue({"flags", "kind", "share", "share_supply"} <= e.keys())
            self.assertIn(e["kind"], ("buy", "transfer"))
        virgin = {e["wallet"] for e in flags if "virgin" in e["flags"]}
        self.assertEqual(virgin, {w for i, w in enumerate(fakes.WALLETS) if i % 3 == 0})
        links = [e for e in events if e["type"] == "link"]
        self.assertEqual(len(links), fakes.BUNDLE - 1)                  # звезда от первого кошелька
        self.assertTrue(all(e["level"] == "proven" and e["kind"] == "same_tx" for e in links))
        done = events[-1]
        self.assertTrue({"score", "band", "headline"} <= done.keys())
        self.assertTrue(done["detail"])                                  # главная причина

    def test_time_budget(self):
        delay = 3.0
        with fakes.patched(history_fn=fakes.slow_history(delay)), \
                mock.patch.object(engine, "BUDGET", 1.0), mock.patch.object(engine, "DETECT_RESERVE", 0.2):
            t0 = time.time()
            res, events = self.scan()
            elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0 + 0.5)                              # не ждём медленные кошельки
        slow = {w for i, w in enumerate(fakes.WALLETS) if i % 2 == 1}
        self.assertEqual(set(res["unread"]), slow)
        flags = {e["wallet"]: e for e in events if e["type"] == "wallet_flag"}
        self.assertEqual(set(flags), set(fakes.WALLETS))                 # флаг есть и у непрочитанных
        for w in slow:
            self.assertIn("unread", flags[w]["flags"])
            self.assertNotIn("virgin", flags[w]["flags"])
        self.assertEqual(events[-1]["type"], "done")

    def test_biggest_wallets_read_first(self):
        # медленная история у всех; окно 2 и маленький бюджет — успевают крупнейшие, а не случайные
        order = []
        def slow(wallet_, before_block, token, window=None, cap=4):
            order.append(wallet_)
            time.sleep(0.15)
            return 4
        with fakes.patched(history_fn=slow), mock.patch.object(engine, "BUDGET", 1.2), \
                mock.patch.object(engine, "DETECT_RESERVE", 0.2), mock.patch.object(ch, "HISTORY_PARALLEL", 2, create=True):
            res = engine.scan(fakes.TOKEN)
        by_share = [h["wallet"] for h in res["holders"]]
        read = [w for w in by_share if w not in res["unread"]]
        self.assertTrue(read, "ничего не прочитано")
        self.assertEqual(read, by_share[:len(read)])                     # прочитаны ровно крупнейшие
        rank = {w: i for i, w in enumerate(by_share)}
        for i, w in enumerate(order[:len(read)]):                         # начаты в порядке доли (внутри окна 2 —
            self.assertLess(rank[w], i + 2, (i, w))                       # соседи могут поменяться местами)

    def test_rank_gate_slow_task_yields_slot(self):
        gate = engine._RankGate(1, slot_s=0.05)
        started = []
        def task(name, delay):
            started.append((name, time.time()))
            time.sleep(delay)
        t0 = time.time()
        th = [threading.Thread(target=gate.run, args=(0, task, "slow", 0.5)),
              threading.Thread(target=gate.run, args=(1, task, "fast", 0.0))]
        for t in th:
            t.start()
        for t in th:
            t.join()
        got = dict(started)
        self.assertLess(got["fast"] - t0, 0.3)                            # не ждал медленного до конца
        gate2 = engine._RankGate(1)
        gate2.close()
        self.assertIsNone(gate2.run(5, task, "never", 0))                 # бюджет вышел — без чтения

    def test_not_pons_token(self):
        with fakes.patched():
            events = []
            with self.assertRaises(engine.ScanError) as cm:
                engine.scan(fakes.OTHER, events.append)
            self.assertFalse(ch.get_token_transfers.called)
        self.assertEqual(str(cm.exception), "not a Pons V2 token")
        self.assertEqual([(e["type"], e["detail"]) for e in events], [("stage", "launch")])

    def test_bad_address(self):
        with fakes.patched():
            for bad in ("0x123", "", None, "hello", "0x" + "g" * 40, "0x" + "a" * 41, fakes.TOKEN[2:]):
                events = []
                with self.assertRaises(engine.ScanError) as cm:
                    engine.scan(bad, events.append)
                self.assertEqual(str(cm.exception), "not a token address", bad)
                self.assertEqual(events, [])
            self.assertFalse(ch.get_launch.called)

    def test_address_case_normalized(self):
        with fakes.patched():
            res, _ = self.scan(fakes.TOKEN.upper().replace("0X", "0x"))
        self.assertEqual(res["token"], fakes.TOKEN)


class TestMarketTimeout(unittest.TestCase):
    """Шапка GeckoTerminal не задерживает скан: ≤ 3 с на запрос, движок ждёт ≤ MARKET_WAIT от старта."""

    def test_slow_header_not_awaited(self):
        def slow(token, network="robinhood", **kw):
            time.sleep(1.5)
            return {"name": "Late", "liquidity_usd": 10.0}
        with fakes.patched(), mock.patch.object(fakes.market, "fetch_market", side_effect=slow), \
                mock.patch.object(engine, "MARKET_WAIT", 0.2):
            t0 = time.time()
            res, events = TestScan.scan(self)
            elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0)                                    # не ждали 1.5 с
        self.assertFalse([g for g in res["gates"] if "liquidity" in g])  # скор без правила ликвидности
        self.assertIsNone(res["header"]["liquidity_usd"])
        self.assertEqual(res["header"]["ticker"], "SYN")                 # имя — из контракта
        time.sleep(1.5)                                                  # поздний ответ GT не меняет результат
        self.assertIsNone(res["header"]["liquidity_usd"])

    def test_gt_budget(self):
        self.assertLessEqual(fakes.market.GT_BUDGET, 3.0)
        self.assertLessEqual(fakes.market._gt.__defaults__[0], 3.0)
        timeouts = []

        def rate_limited(req, timeout):
            timeouts.append(timeout)
            raise fakes.market.urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        with mock.patch.object(fakes.market.urllib.request, "urlopen", side_effect=rate_limited):
            t0 = time.time()
            with self.assertRaises(Exception):
                fakes.market._gt("/robinhood/tokens/x", budget=0.8)
            elapsed = time.time() - t0
        self.assertLess(elapsed, 1.0)                                    # повторы на 429 — в пределах бюджета
        self.assertTrue(timeouts and all(t <= 0.8 for t in timeouts))
        with mock.patch.object(fakes.market.urllib.request, "urlopen", side_effect=rate_limited):
            self.assertEqual(fakes.market.fetch_market(fakes.TOKEN), {})  # сбой шапки не фатален


if __name__ == "__main__":
    unittest.main()
