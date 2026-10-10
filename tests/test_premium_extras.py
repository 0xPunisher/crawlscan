"""Тесты трёх функций премиума: приоритет в очереди сканов (PREMIUM_PRIORITY), импорт из кошелька (PREMIUM_IMPORT),
утренняя сводка (PREMIUM_DIGEST). Выключено → как раньше. Без сети."""
import os, threading, time, unittest
from datetime import datetime, timezone
from unittest import mock

import fakes
import alerts
import engine
import memguard
import premium
import premium_digest
import premium_import
import scan_gate
import server
from alerts_store import AlertsStore
from premium_store import PremiumStore
from bot import main as bm, text as T
from bot.api import CrawlScan
from test_premium import E18, NOW, SECRET, U1, U2, W1, W2, PremiumAPI, Site, TGStub, private

U3 = 333333
TOK = ["0x" + f"{i:02x}" * 20 for i in range(40, 60)]
CRAWL = premium.DEFAULT_TOKEN
ALL_ON = {"PREMIUM_PRIORITY": "true", "PREMIUM_IMPORT": "true", "PREMIUM_DIGEST": "true"}


# ---------- 1. приоритет в очереди ----------

class TestFairGate(unittest.TestCase):

    def run_queue(self, arrivals, slots=1, streak=2):
        """Слоты заняты; в очередь по порядку встают arrivals [(имя, премиум ли)]; слоты освобождаются →
        в каком порядке ждущие получили слот."""
        gate = scan_gate.FairGate(slots, streak_max=streak)
        for _ in range(slots):
            gate.acquire(False)
        got, threads = [], []
        for i, (name, prio) in enumerate(arrivals):
            def run(name=name, prio=prio):
                gate.acquire(prio)
                got.append(name)
                gate.release()
            t = threading.Thread(target=run, daemon=True)
            t.start()
            threads.append(t)
            deadline = time.time() + 5
            while sum(gate.waiting_count()) < i + 1 and time.time() < deadline:   # встал в очередь — следующий
                time.sleep(0.001)
        for _ in range(slots):
            gate.release()
        for t in threads:
            t.join(5)
        self.assertEqual(gate.waiting_count(), (0, 0))
        return got

    def test_premium_first(self):
        self.assertEqual(self.run_queue([("n1", False), ("p1", True)]), ["p1", "n1"])

    def test_fairness_after_two_premium(self):
        order = self.run_queue([("n1", False), ("p1", True), ("p2", True), ("p3", True), ("n2", False), ("p4", True)])
        self.assertEqual(order, ["p1", "p2", "n1", "p3", "p4", "n2"])

    def test_only_premium_waiting_goes_on(self):
        self.assertEqual(self.run_queue([("p1", True), ("p2", True), ("p3", True), ("p4", True)]),
                         ["p1", "p2", "p3", "p4"])

    def test_only_regular_is_fifo(self):
        self.assertEqual(self.run_queue([(f"n{i}", False) for i in range(5)]), [f"n{i}" for i in range(5)])

    def test_slot_count_unchanged(self):
        gate = scan_gate.FairGate(3)
        live, peak, lock = [0], [0], threading.Lock()

        def run(prio):
            with gate.slot(prio):
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                time.sleep(0.01)
                with lock:
                    live[0] -= 1
        ts = [threading.Thread(target=run, args=(i % 2 == 0,)) for i in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        self.assertEqual((peak[0], gate.free), (3, 3))


class TestPriorityServer(Site):
    ENV = {"PREMIUM_ENABLED": "true", "ALERTS_ENABLED": "true", "PREMIUM_PRIORITY": "true"}

    def link(self, user, wallet, balance):
        self.verify(user, wallet, balance)

    def test_slot_choice(self):
        self.assertIsInstance(server._slot(True), type(server._gate.slot(True)))
        for env in ({"PREMIUM_PRIORITY": "false"}, {"PREMIUM_ENABLED": "false"}):
            with mock.patch.dict(os.environ, env):
                self.assertIs(server._slot(True), server._sem)                              # выключено — как раньше

    def test_who_is_premium(self):
        self.link(U1, W1, 600_000 * E18)
        self.link(U2, W2, 1 * E18)                                                          # привязан, но ниже порога
        ok = {"X-Alerts-Secret": SECRET}
        self.assertTrue(server.scan_priority(ok, {"token": TOK[0], "user_id": U1}))
        self.assertTrue(server.scan_priority(ok, {"token": TOK[0], "user_id": str(U1)}))
        self.assertFalse(server.scan_priority(ok, {"token": TOK[0], "user_id": U2}))
        self.assertFalse(server.scan_priority(ok, {"token": TOK[0], "user_id": U3}))
        self.assertFalse(server.scan_priority(ok, {"token": TOK[0]}))
        self.assertFalse(server.scan_priority({}, {"token": TOK[0], "user_id": U1}))        # сайт: без секрета
        self.assertFalse(server.scan_priority({"X-Alerts-Secret": "x"}, {"token": TOK[0], "user_id": U1}))
        self.assertFalse(server.scan_priority(ok, {"token": TOK[0], "user_id": "abc"}))
        with mock.patch.object(PremiumStore, "link_of", side_effect=RuntimeError("locked")):
            self.assertFalse(server.scan_priority(ok, {"token": TOK[0], "user_id": U1}))   # сбой базы — обычный

    def test_off_does_not_open_db(self):
        with mock.patch.dict(os.environ, {"PREMIUM_PRIORITY": "false"}), \
                mock.patch.object(server, "premium_store", side_effect=AssertionError("premium db read")):
            self.assertFalse(server.scan_priority({"X-Alerts-Secret": SECRET}, {"token": TOK[0], "user_id": U1}))

    def test_premium_scan_from_bot_takes_next_slot(self):
        """Через настоящий /api/scan: слот занят, ждут обычный (сайт) и премиум (бот) → первым идёт премиум."""
        self.link(U1, W1, 600_000 * E18)
        started, gates = [], {t: threading.Event() for t in TOK[:3]}

        def scan(token, emit=None):
            started.append(token)
            gates[token].wait(10)
            raise engine.ScanError("test stop")
        gate = scan_gate.FairGate(1)
        with mock.patch.object(server, "_gate", gate), mock.patch.object(server.engine, "scan", side_effect=scan), \
                mock.patch.object(memguard, "over", return_value=False):
            self.assertEqual(self.req("/api/scan", {"token": TOK[0]}, secret=None)[0], 200)
            self.wait(lambda: started == [TOK[0]])
            self.req("/api/scan", {"token": TOK[1], "user_id": U1}, secret=None)            # сайт: обычный, хоть и U1
            self.wait(lambda: gate.waiting_count() == (0, 1))
            self.req("/api/scan", {"token": TOK[2], "user_id": U1})                         # бот: премиум
            self.wait(lambda: gate.waiting_count() == (1, 1))
            gates[TOK[0]].set()
            self.wait(lambda: len(started) == 2)
            self.assertEqual(started[1], TOK[2])
            gates[TOK[2]].set()
            self.wait(lambda: len(started) == 3)
            gates[TOK[1]].set()
            self.wait(lambda: gate.free == 1)
        self.assertEqual(started, [TOK[0], TOK[2], TOK[1]])

    def wait(self, cond, timeout=5):
        deadline = time.time() + timeout
        while not cond():
            self.assertLess(time.time(), deadline, "не дождались")
            time.sleep(0.005)


class TestPriorityBot(unittest.TestCase):

    def make(self, feats=()):
        api = PremiumAPI(premium_users=[U1])
        api.scans = []
        api.config = lambda: {"alerts": True, "premium": True} | {f: True for f in feats}
        api.scan = lambda token, **kw: api.scans.append((token, kw)) or "job"
        tg = TGStub()
        return bm.Bot(tg, api, username="b", spawn=lambda f: f(), sleep=lambda s: None, log=lambda m: None,
                      admin_ids=set()), tg, api

    def test_user_id_only_with_priority(self):
        bot, tg, api = self.make()
        bot.run_scan(U1, 5, TOK[0], False, True, U1)
        self.assertEqual(api.scans[-1], (TOK[0], {}))                                     # выключено — как раньше
        bot, tg, api = self.make(["premium_priority"])
        bot.run_scan(U1, 5, TOK[0], False, True, U1)
        self.assertEqual(api.scans[-1], (TOK[0], {"user_id": U1}))

    def test_client_sends_user_id_only_with_secret(self):
        sent = []
        api = CrawlScan("http://x", alerts_secret="")
        api._req = lambda path, body=None, headers=None, ok=(): sent.append(body) or {"job": "j"}
        api.scan(TOK[0], user_id=U1)
        api.alerts_secret = SECRET
        api.scan(TOK[0], user_id=U1)
        api.scan(TOK[0])
        self.assertEqual(sent, [{"token": TOK[0]}, {"token": TOK[0], "user_id": U1}, {"token": TOK[0]}])


# ---------- 2. импорт из кошелька ----------

PONS = [TOK[i] for i in range(0, 8)]
OTHER = TOK[10]                      # не мемкоин лаунчпадов (стейбл, акция)
NOPRICE = TOK[11]                    # мемкоин без пары на DexScreener


class WalletChain:
    """Адаптер для импорта: балансы кошелька (alchemy_getTokenBalances) и батч eth_call лаунчпадов."""

    def __init__(self, balances, pons):
        self.balances, self.pons, self.calls = balances, set(pons), []
        self.REQUESTS = [0]

    def wallet_token_balances(self, wallet):
        self.calls.append(("balances", wallet))
        self.REQUESTS[0] += 1
        return dict(self.balances), True

    def launched_call(self, token):
        return ("eth_call", [{"to": "factory", "data": token}, "latest"])

    def is_launched(self, r):
        return r == "yes"

    def rpc_batch(self, calls):
        self.calls.append(("batch", len(calls)))
        self.REQUESTS[0] += 1
        return ["yes" if c[1][0]["data"] in self.pons else "no" for c in calls]


def prices(table):
    seen = []

    def ds(tokens):
        seen.append(list(tokens))
        return {t: table[t] for t in tokens if t in table}
    ds.seen = seen
    return ds


class TestImportLookup(unittest.TestCase):

    def setUp(self):
        premium_import._PAD.clear()
        self.bal = {t: (i + 1) * 1000 * E18 for i, t in enumerate(PONS)} | {OTHER: 10 ** 30, NOPRICE: 10 ** 24,
                                                                           CRAWL: 600_000 * E18}
        self.mkt = {t: {"ticker": f"T{i}", "price_usd": 1.0, "change_24h": 1.0} for i, t in enumerate(PONS)} | {
            OTHER: {"ticker": "USD", "price_usd": 1.0}, CRAWL: {"ticker": "CRAWL", "price_usd": 1.0}}

    def test_top5_supported_by_value(self):
        ch = WalletChain(self.bal, PONS + [NOPRICE, CRAWL])
        ds = prices(self.mkt)
        r = premium_import.lookup(ch, W1, ds, lambda t, m: t == PONS[6], lambda t: t == PONS[5], skip=[CRAWL])
        self.assertEqual([it["token"] for it in r["items"]], [PONS[7], PONS[6], PONS[5], PONS[4], PONS[3]])
        self.assertEqual([it["status"] for it in r["items"]], ["ok", "established", "active", "ok", "ok"])
        self.assertEqual(r["items"][0] | {}, {"token": PONS[7], "ticker": "T7", "launchpad": "pons", "balance": 8000.0,
                                             "price_usd": 1.0, "value_usd": 8000.0, "status": "ok"})
        self.assertNotIn(CRAWL, ds.seen[0])                                                 # свой токен не оценивается
        self.assertEqual(len(ds.seen), 1)                                                   # DexScreener — одной пачкой
        self.assertEqual(r["stats"], {"rpc": 2, "eth_calls": 9, "ds": 1})                  # 1 balances + 1 батч eth_call
        self.assertEqual(ch.calls, [("balances", W1), ("batch", 9)])
        ch.calls.clear()
        premium_import.lookup(ch, W1, ds, lambda t, m: False, lambda t: False, skip=[CRAWL])
        self.assertEqual(ch.calls, [("balances", W1)])                                     # лаунчпад — из кэша

    def test_empty_wallet(self):
        ch = WalletChain({}, [])
        ds = prices({})
        r = premium_import.lookup(ch, W1, ds, lambda t, m: False, lambda t: False)
        self.assertEqual((r["items"], ds.seen, ch.calls), ([], [], [("balances", W1)]))

    def test_add_respects_limit_and_skips(self):
        hit = {"items": [{"token": t, "status": s} for t, s in
                         zip(PONS[:5], ["ok", "established", "ok", "active", "ok"])]}
        watching, added = {PONS[0]}, []

        def watch(t):
            if len(watching) >= 2:
                return "limit"
            added.append(t)
            return "ok"
        out = premium_import.add(hit, "all", watch, watching, 2)
        self.assertEqual(out["already"], [PONS[0]])
        self.assertEqual(out["added"], [PONS[2]])
        self.assertEqual(out["full"], [PONS[4]])
        self.assertEqual([s["reason"] for s in out["skipped"]], ["established", "active"])
        out = premium_import.add(hit, [TOK[19]], watch, watching, 2)
        self.assertEqual(out["skipped"], [{"token": TOK[19], "reason": "unknown"}])


class TestWalletBalances(unittest.TestCase):
    """chains.robinhood.wallet_token_balances: сколько RPC и что из ответа берётся."""

    def run_rpc(self, replies):
        calls = []

        def rpc(method, params):
            calls.append((method, params))
            return replies[len(calls) - 1]
        with mock.patch.object(fakes.ch, "rpc", side_effect=rpc):
            out = fakes.ch.wallet_token_balances(W1.upper().replace("0X", "0x"))
        return out, calls

    def test_one_page_is_one_rpc(self):
        page = {"tokenBalances": [{"contractAddress": TOK[0].upper().replace("0X", "0x"), "tokenBalance": hex(5)},
                                  {"contractAddress": TOK[1], "tokenBalance": "0x" + "0" * 64},
                                  {"contractAddress": TOK[2], "tokenBalance": None, "error": "x"}]}
        (bal, complete), calls = self.run_rpc([page])
        self.assertEqual((bal, complete), ({TOK[0]: 5}, True))
        self.assertEqual(calls, [("alchemy_getTokenBalances", [W1, "erc20", {"maxCount": 100}])])

    def test_long_history_is_three_rpc(self):
        page = {"tokenBalances": [{"contractAddress": TOK[0], "tokenBalance": hex(5)}], "pageKey": "k"}
        transfers = {"transfers": [{"rawContract": {"address": a}} for a in (TOK[3], TOK[0], TOK[3], TOK[4])]}
        listed = {"tokenBalances": [{"contractAddress": TOK[3], "tokenBalance": hex(7)},
                                    {"contractAddress": TOK[4], "tokenBalance": "0x0"}]}
        (bal, complete), calls = self.run_rpc([page, transfers, listed])
        self.assertEqual((bal, complete), ({TOK[0]: 5, TOK[3]: 7}, False))
        self.assertEqual([c[0] for c in calls], ["alchemy_getTokenBalances", "alchemy_getAssetTransfers",
                                                 "alchemy_getTokenBalances"])
        self.assertEqual(calls[1][1][0]["order"], "desc")
        self.assertEqual(calls[2][1], [W1, [TOK[3], TOK[4]]])                              # уже виденные не повторяем


class TestImportAPI(Site):
    ENV = {"PREMIUM_ENABLED": "true", "ALERTS_ENABLED": "true", "PREMIUM_IMPORT": "true"}

    def setUp(self):
        super().setUp()
        premium_import._PAD.clear()
        self.imports = mock.patch.object(server, "PREMIUM_IMPORTS", premium_import.Imports())
        self.imports.__enter__()
        self.items = [{"token": t, "ticker": f"T{i}", "launchpad": "pons", "balance": 1.0, "price_usd": 1.0,
                       "value_usd": 100.0 - i, "status": "ok"} for i, t in enumerate(PONS[:5])]
        self.items[1]["status"] = "established"
        self.lookups = []
        self.lookup = mock.patch.object(premium_import, "lookup", side_effect=lambda ch, w, *a, **k: self.lookups.append(w)
                                        or {"items": [dict(x) for x in self.items], "tokens": 9, "complete": True,
                                            "stats": {"rpc": 2, "eth_calls": 5, "ds": 1}})
        self.lookup.__enter__()

    def tearDown(self):
        self.lookup.__exit__(None, None, None)
        self.imports.__exit__(None, None, None)
        super().tearDown()

    def test_premium_only(self):
        self.assertEqual(self.req("/api/premium/import", {"user_id": U3})[0], 403)          # не привязан
        self.link_premium(U2, W2, 1 * E18)
        code, r = self.req("/api/premium/import", {"user_id": U2})
        self.assertEqual((code, r["error"]), (403, "not premium"))
        self.assertEqual(self.lookups, [])

    def link_premium(self, user, wallet, balance):
        self.verify(user, wallet, balance)

    def test_once_per_10_minutes(self):
        self.link_premium(U1, W1, 600_000 * E18)
        code, r = self.req("/api/premium/import", {"user_id": U1})
        self.assertEqual((code, r["cached"], len(r["items"]), r["limit"]), (200, False, 5, 10))
        code, r = self.req("/api/premium/import", {"user_id": U1})
        self.assertEqual((code, r["cached"]), (200, True))                                 # тот же список, кошелёк не смотрим
        self.assertEqual(self.lookups, [W1])
        hit = server.PREMIUM_IMPORTS.by_user[U1]
        hit["at"] -= premium.IMPORT_EVERY                                                   # прошло 10 минут
        self.assertFalse(self.req("/api/premium/import", {"user_id": U1})[1]["cached"])
        self.assertEqual(self.lookups, [W1, W1])

    def test_add_all_respects_watchlist_limit(self):
        self.link_premium(U1, W1, 600_000 * E18)
        for t in TOK[12:20]:                                                                # уже 8 из 10
            self.assertEqual(self.watch(t)[0], 200)
        self.watch(PONS[0])                                                                  # 9 из 10, один из списка
        self.assertEqual(self.req("/api/premium/import_add", {"user_id": U1, "tokens": "all"})[0], 410)   # нет просмотра
        code, r = self.req("/api/premium/import", {"user_id": U1})
        self.assertEqual([it["watching"] for it in r["items"]], [True, False, False, False, False])
        code, r = self.req("/api/premium/import_add", {"user_id": U1, "tokens": "all"})
        self.assertEqual(code, 200)
        self.assertEqual((r["already"], r["added"], r["full"]), ([PONS[0]], [PONS[2]], [PONS[3], PONS[4]]))
        self.assertEqual(r["skipped"], [{"token": PONS[1], "reason": "established", "ticker": "T1"}])
        self.assertEqual((r["watch_count"], len(r["items"]), r["limit"]), (10, 10, 10))
        code, lst = self.req(f"/api/alerts/list?chat_id={U1}")
        self.assertEqual(len(lst["items"]), 10)
        self.assertTrue(all(w["expires_at"] == alerts.NO_EXPIRY for w in lst["items"]))   # премиум — без срока
        text = T.import_added(r)
        self.assertIn("Your watchlist is full (10 tokens)", text)
        self.assertIn("too established for CrawlScan, can't be watched", text)

    def test_add_one(self):
        self.link_premium(U1, W1, 600_000 * E18)
        self.req("/api/premium/import", {"user_id": U1})
        code, r = self.req("/api/premium/import_add", {"user_id": U1, "tokens": [PONS[3].upper().replace("0X", "0x")]})
        self.assertEqual((code, r["added"]), (200, [PONS[3]]))
        code, r = self.req("/api/premium/import_add", {"user_id": U1, "tokens": [PONS[1]]})
        self.assertEqual((r["added"], r["skipped"][0]["reason"]), ([], "established"))


class TestImportOff(Site):
    ENV = {"PREMIUM_ENABLED": "true", "ALERTS_ENABLED": "true"}

    def test_404(self):
        with mock.patch.object(premium_import, "lookup", side_effect=AssertionError("wallet read")):
            self.assertEqual(self.req("/api/premium/import", {"user_id": U1})[0], 404)
            self.assertEqual(self.req("/api/premium/import_add", {"user_id": U1, "tokens": "all"})[0], 404)
            self.assertEqual(self.req("/api/premium/digest", {"user_id": U1, "on": False})[0], 404)


# ---------- бот: импорт и /digest ----------

class FeatAPI(PremiumAPI):
    def __init__(self, feats=(), **kw):
        super().__init__(**kw)
        self.feats, self.digest_calls, self.adds = set(feats), [], []
        self.import_reply = {"ok": True, "wallet": W1, "watch_count": 9, "limit": 10, "cached": False, "items": [
            {"token": PONS[0], "ticker": "AAA", "launchpad": "pons", "value_usd": 1234.5, "status": "ok", "watching": False},
            {"token": PONS[1], "ticker": "BBB", "launchpad": "flap", "value_usd": 99.0, "status": "established",
             "watching": False},
            {"token": PONS[2], "ticker": "CCC", "launchpad": "bankr", "value_usd": 50.0, "status": "ok", "watching": True},
            {"token": PONS[3], "ticker": "DDD", "launchpad": "pons", "value_usd": 10.0, "status": "ok", "watching": False}]}

    def config(self):
        self.calls.append("config")
        return {"alerts": True, "premium": True} | {f: True for f in self.feats}

    def premium_status(self, user_id):
        return super().premium_status(user_id) | {f: True for f in self.feats} | (
            {"digest": True, "digest_hour": 8} if "premium_digest" in self.feats else {})

    def premium_import(self, user_id):
        if user_id not in self.premium_users:
            return {"status": 403, "error": "not premium"}
        return self.import_reply

    def premium_import_add(self, user_id, tokens):
        self.adds.append(tokens)
        return {"ok": True, "added": [PONS[0]], "already": [], "full": [PONS[3]], "skipped": [], "watch_count": 10,
                "limit": 10}

    def premium_digest(self, user_id, on=None):
        self.digest_calls.append(on)
        return {"ok": True, "digest": on is not False, "hour": 8, "premium": user_id in self.premium_users}

    def watch_list(self, chat_id):
        return {"items": [], "limit": 10}


def callback(data, user=U1):
    return {"callback_query": {"id": "1", "data": data, "from": {"id": user},
                               "message": {"message_id": 3, "chat": {"id": user, "type": "private"}}}}


class TestBotExtras(unittest.TestCase):

    def make(self, feats=(), **kw):
        api = FeatAPI(feats, **kw)
        tg = TGStub()
        return bm.Bot(tg, api, username="b", spawn=lambda f: f(), sleep=lambda s: None, log=lambda m: None,
                      admin_ids=set()), tg, api

    def test_import_button_and_flow(self):
        bot, tg, api = self.make(["premium_import"], premium_users=[U1])
        text, markup = bot.premium_text(U1, U1, "premium", "", now=NOW)
        self.assertEqual(markup["inline_keyboard"][0], [T.PICK_BUTTON])
        bot.handle_update(private("/watchlist"))
        self.assertEqual(tg.markups[-1]["inline_keyboard"][-1], [T.IMPORT_BUTTON])
        bot.handle_update(callback("pick"))
        self.assertIn(f"<b>Pick tokens</b> from <code>{W1}</code>: your top memecoins by value", tg.texts[-1])
        self.assertIn("1. $AAA · Pons · $1.2K", tg.texts[-1])
        self.assertIn("🏛 too established for CrawlScan, can't be watched", tg.texts[-1])
        self.assertIn("🔔 already watching", tg.texts[-1])
        self.assertIn("Only 1 more fits: remove a token to add more.", tg.texts[-1])
        buttons = [b["callback_data"] for row in tg.markups[-1]["inline_keyboard"] for b in row]
        self.assertEqual(buttons, [f"ia:{PONS[0]}", f"ia:{PONS[3]}", "ia:all"])
        bot.handle_update(callback("ia:all"))
        bot.handle_update(callback(f"ia:{PONS[0]}"))
        self.assertEqual(api.adds, ["all", [PONS[0]]])
        self.assertIn("Your watchlist is full (10 tokens), not added", tg.texts[-1])
        bot.handle_update(callback("imp", user=U2))
        self.assertEqual(tg.texts[-1], T.IMPORT_NOT_PREMIUM)

    def test_import_off_as_before(self):
        bot, tg, api = self.make(premium_users=[U1])
        text, markup = bot.premium_text(U1, U1, "premium", "", now=NOW)
        self.assertEqual(markup, {"inline_keyboard": [[{"text": "Unlink", "callback_data": "unlink"}]]})
        self.assertNotIn("digest", text.lower())
        self.assertNotIn("/picktokens", text)
        bot.handle_update(private("/watchlist"))
        self.assertNotIn([T.IMPORT_BUTTON], tg.markups[-1]["inline_keyboard"])
        bot.handle_update(callback("imp"))
        self.assertEqual(tg.texts[-1], T.HINT)

    def test_rate_limited_text(self):
        bot, tg, api = self.make(["premium_import"], premium_users=[U1])
        api.import_reply = {"status": 429, "error": "too soon", "retry_in": 61}
        bot.handle_update(callback("imp"))
        self.assertEqual(tg.texts[-1], "You can pick tokens once every 10 minutes. Try again in 2 min.")

    def test_digest_command(self):
        bot, tg, api = self.make(["premium_digest"], premium_users=[U1])
        bot.handle_update(private("/digest off"))
        self.assertEqual(tg.texts[-1], "Morning digest is off. /digest on turns it back on.")
        bot.handle_update(private("/digest on"))
        self.assertTrue(tg.texts[-1].startswith("☀️ Morning digest is on: every day at 08:00 UTC"))
        bot.handle_update(private("/digest maybe"))
        self.assertEqual(tg.texts[-1], T.DIGEST_USAGE)
        bot.handle_update(private("/digest on", user=U2))
        self.assertIn("It's for Premium holders", tg.texts[-1])
        self.assertEqual(api.digest_calls, [False, True, True])
        text, markup = bot.premium_text(U1, U1, "premium", "", now=NOW)
        self.assertIn("/digest — a daily morning summary of your Watchlist (on/off)", text)
        self.assertEqual(markup["inline_keyboard"][0], [{"text": "Daily digest: on", "callback_data": "digest:off"}])

    def test_digest_off_as_before(self):
        bot, tg, api = self.make(premium_users=[U1])
        bot.handle_update(private("/digest on"))
        self.assertEqual((tg.texts[-1], api.digest_calls), (T.HINT, []))
        bot, tg, api = self.make(["premium_digest"], on=False)
        api.config = lambda: {"alerts": True, "premium_digest": True}                       # без PREMIUM_ENABLED
        bot.handle_update(private("/digest on"))
        self.assertEqual((tg.texts[-1], api.digest_calls), (T.HINT, []))

    def test_commands_menu(self):
        bot, tg, api = self.make(["premium_digest"])
        sent = []
        bot.call = lambda method, upload=None, **p: sent.append((method, p))
        bot.start_workers = lambda: None
        bot.tg.call = lambda method, **p: (_ for _ in ()).throw(KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            bot.run()
        cmds = [c["command"] for c in dict(sent)["setMyCommands"]["commands"]]
        self.assertEqual(cmds[-1], "digest")
        bot, tg, api = self.make()
        sent.clear()
        bot.call = lambda method, upload=None, **p: sent.append((method, p))
        bot.start_workers = lambda: None
        bot.tg.call = lambda method, **p: (_ for _ in ()).throw(KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            bot.run()
        self.assertNotIn("digest", [c["command"] for c in dict(sent)["setMyCommands"]["commands"]])


# ---------- 3. утренняя сводка ----------

def snap(token, band="OK", score=70, rug=False, op_share=0.10, top=None, ticker=None, ts=NOW):
    return {"token": token, "chain": "robinhood", "ticker": ticker, "ts": ts, "band": band, "score": score, "rug": rug,
            "rug_drop": 0.72 if rug else None, "operator": {"wallets": [W1], "share_supply": op_share},
            "top": top if top is not None else {W1: op_share}, "early_share": None}


def at(hour, day=10):
    return int(datetime(2026, 10, day, hour, 5, tzinfo=timezone.utc).timestamp())


class TestDigest(Site):
    ENV = {"PREMIUM_ENABLED": "true", "ALERTS_ENABLED": "true", "PREMIUM_DIGEST": "true"}

    def setUp(self):
        super().setUp()
        self.ds_calls, self.queued = [], []
        self.scans = mock.patch.object(server.engine, "scan", side_effect=AssertionError("digest started a scan"))
        self.scans.__enter__()
        self.starts = mock.patch.object(server, "start_scan", side_effect=AssertionError("digest started a scan"))
        self.starts.__enter__()

    def tearDown(self):
        self.starts.__exit__(None, None, None)
        self.scans.__exit__(None, None, None)
        super().tearDown()

    def prices(self, chain, tokens):
        self.ds_calls.append((chain, list(tokens)))
        return {TOK[0]: {"change_24h": 12.34, "ticker": "AAA"}, TOK[1]: {"change_24h": -45.0}}

    def digest(self, clock):
        return premium_digest.Digest(server.premium_store(), server.alerts_store,
                                     lambda u, text, markup=None: self.queued.append((u, text, markup)),
                                     self.prices, clock=clock, log=lambda m: None, hour=8)

    def test_daily_digest(self):
        self.verify(U1, W1, 600_000 * E18)                                                  # премиум, 2 токена
        self.verify(U2, W2, 1 * E18)                                                        # не премиум
        for t in TOK[:2]:
            self.watch(t, chat=U1)
        self.watch(TOK[0], chat=U2)
        ast = server.alerts_store()
        ast.record(snap(TOK[0], "OK", 70, ticker="AAA"))
        ast.record(snap(TOK[1], "CLEAN", 85))
        clock = [at(7)]
        d = self.digest(lambda: clock[0])
        self.assertIsNone(d.tick())                                                         # 07:05 — рано
        clock[0] = at(8)
        self.assertEqual(d.tick(), 1)                                                       # только U1
        self.assertIsNone(d.tick())                                                         # второй раз за день — нет
        u, text, markup = self.queued[-1]
        self.assertEqual(u, U1)
        self.assertIn("☀️ <b>Morning digest</b> · 2026-10-10 · 2 tokens", text)
        self.assertIn("🟡 <b>$AAA</b> · OK 70/100 · 24h price +12.3%", text)
        self.assertIn("• New in your digest: changes show from tomorrow", text)
        self.assertEqual(markup, premium_digest.WATCHLIST_BUTTON)
        # за сутки: вердикт хуже, probably rug, оператор продал; цена −45%
        ast.record(snap(TOK[1], "DANGER", 20, rug=True, top={W1: 0.02}, ts=at(8, 11) - 600))
        ast.record(snap(TOK[0], "OK", 72, ticker="AAA", ts=at(8, 11) - 600))
        clock[0] = at(9, 11)
        self.assertEqual(d.tick(), 1)
        u, text, _ = self.queued[-1]
        self.assertIn("🔴 <b>0x2929…2929</b> · DANGER 20/100 · 24h price −45.0%", text)
        self.assertIn("⚠️ probably rug −72%", text)
        self.assertIn("• Verdict worsened: CLEAN → DANGER (score 85 → 20)", text)
        self.assertIn("• Probably rug: −72% if suspicious holders sell", text)
        self.assertIn("• Biggest operator sold 80% of their holdings (10.0% → 2.0% of supply)", text)
        self.assertIn("<b>$AAA</b> · OK 72/100 · 24h price +12.3%\n• No important changes in 24h", text)
        self.assertEqual(len(self.ds_calls), 2)                                            # одна пачка DexScreener в день
        self.assertEqual(self.ds_calls[-1], ("robinhood", sorted(TOK[:2])))
        clock[0] = at(12, 12)
        self.assertIsNone(d.tick())                                                         # 12:05 — окно прошло

    def test_digest_off_and_empty(self):
        self.verify(U1, W1, 600_000 * E18)
        d = self.digest(lambda: at(8))
        self.assertEqual(d.tick(), 0)                                                       # пустой Watchlist
        self.watch(TOK[0], chat=U1)
        code, r = self.req("/api/premium/digest", {"user_id": U1, "on": False})
        self.assertEqual((code, r["digest"], r["hour"], r["premium"]), (200, False, 8, True))
        server.premium_store().set_meta(premium_digest.DAY_KEY, "")
        self.assertEqual(d.tick(), 0)
        self.assertEqual(self.req("/api/premium/digest", {"user_id": U1, "on": True})[1]["digest"], True)
        server.premium_store().set_meta(premium_digest.DAY_KEY, "")
        self.assertEqual(d.tick(), 1)
        self.assertIn("⚪️ <b>$AAA</b> · not scanned yet · 24h price +12.3%", self.queued[-1][1])   # снимка нет — без скана
        code, st = self.req(f"/api/premium/status?user_id={U1}")
        self.assertEqual((st["digest"], st["digest_hour"], st["premium_digest"]), (True, 8, True))

    def test_through_notify_queue(self):
        """start_premium поднимает поток сводки с server.notify_message (очередь отправщика alerts) и ds_batch."""
        with mock.patch.object(premium_digest.Digest, "start", lambda d: d), \
                mock.patch.object(server, "_digest", {"obj": None}), \
                mock.patch.object(server.premium_service.Service, "start", lambda s: s):
            d = server.start_premium()
            obj = server._digest["obj"]
        self.assertIsNotNone(d)
        self.assertIs(obj.notify, server.notify_message)
        with mock.patch.object(server.market, "ds_batch", return_value={}) as ds:
            obj.prices("robinhood", [TOK[0]])
        ds.assert_called_once_with([TOK[0]], "robinhood")


class TestExtrasDisabled(Site):
    """Функции выключены (или выключен сам премиум) — всё как раньше."""
    ENV = {"PREMIUM_ENABLED": "true", "ALERTS_ENABLED": "true"}

    def test_config_status_and_threads(self):
        code, cfg = self.req("/api/config")
        self.assertEqual({k for k in cfg if k.startswith("premium")}, {"premium"})
        code, st = self.req(f"/api/premium/status?user_id={U1}")
        self.assertFalse({"premium_priority", "premium_import", "premium_digest", "digest"} & set(st))
        self.assertIsNone(server.start_premium_digest())
        self.assertIs(server._slot(True), server._sem)

    def test_premium_off_ignores_feature_flags(self):
        with mock.patch.dict(os.environ, {"PREMIUM_ENABLED": "false"} | ALL_ON), \
                mock.patch.object(server, "PremiumStore", side_effect=AssertionError("premium db opened")):
            self.assertEqual(premium.features(), {})
            code, cfg = self.req("/api/config")
            self.assertFalse([k for k in cfg if k.startswith("premium")])
            self.assertIs(server._slot(True), server._sem)
            self.assertFalse(server.scan_priority({"X-Alerts-Secret": SECRET}, {"user_id": U1}))
            self.assertIsNone(server.start_premium_digest())
            self.assertEqual(self.req("/api/premium/import", {"user_id": U1})[0], 404)

    def test_all_on_config(self):
        with mock.patch.dict(os.environ, ALL_ON):
            code, cfg = self.req("/api/config")
        self.assertEqual({k for k in cfg if k.startswith("premium")},
                         {"premium", "premium_priority", "premium_import", "premium_digest"})


class TestFlags(unittest.TestCase):

    def test_flags(self):
        with mock.patch.dict(os.environ, {"PREMIUM_ENABLED": "true", "PREMIUM_PRIORITY": "", "PREMIUM_IMPORT": "",
                                          "PREMIUM_DIGEST": "", "PREMIUM_DIGEST_HOUR_UTC": ""}):
            self.assertEqual((premium.priority_enabled(), premium.import_enabled(), premium.digest_enabled()),
                             (False, False, False))
            self.assertEqual(premium.digest_hour(), 8)
        for v, want in (("6", 6), ("0", 0), ("24", 8), ("x", 8)):
            with mock.patch.dict(os.environ, {"PREMIUM_DIGEST_HOUR_UTC": v}):
                self.assertEqual(premium.digest_hour(), want)


if __name__ == "__main__":
    unittest.main()
