"""Тесты функций премиума: история дева (PREMIUM_DEVCHECK), Memory (PREMIUM_MEMORY_INSIGHTS), Trending
(PREMIUM_TRENDING), Fresh scan (PREMIUM_FRESH), лимит сканов бота на Telegram ID (PREMIUM_BOT_RATE), строки меню.
Выключено → как раньше; путь скана не меняется. Без сети."""
import os, sqlite3, tempfile, time, unittest
from unittest import mock

import fakes  # noqa: F401
import opmem
import premium
import premium_memory
import premium_trending
import replay
import server
from bot import main as bm, text as T
from bot.api import TooFast
from test_premium import E18, SECRET, TOKENS, U1, U2, W1, W2, Site, TGStub, private
from test_premium_extras import FeatAPI, callback

NOW = int(time.time())
DEV, OTHER_DEV = "0x" + "de" * 20, "0x" + "df" * 20
A, B, C, D, E = ("0x" + f"{i:02x}" * 20 for i in (0x61, 0x62, 0x63, 0x64, 0x65))
H = ["0x" + f"{i:02x}" * 20 for i in range(0x70, 0x7a)]          # кошельки-холдеры
ALL_NEW = {"PREMIUM_DEVCHECK": "true", "PREMIUM_MEMORY_INSIGHTS": "true", "PREMIUM_TRENDING": "true",
           "PREMIUM_FRESH": "true", "PREMIUM_BOT_RATE": "true"}


def result(token, dev=DEV, holders=(), snipers=(), ops=(), band="OK", score=70, ticker=None, mcap=50_000.0):
    """Результат скана в форме engine.scan (то, что пишет opmem.rows)."""
    return {"token": token, "chain": "robinhood", "launchpad": "pons", "band": band, "score": score,
            "launch": {"deployer": dev}, "header": {"ticker": ticker, "mcap_usd": mcap},
            "holders": [{"wallet": w, "share": 0.05, "share_supply": 0.04,
                         "signals": {"sniper": w in snipers}} for w in holders],
            "operators": [{"wallets": list(o), "share": 0.1, "share_supply": 0.12} for o in ops]}


def memory_db(path, scans):
    """memory.db: [(результат, ts)] через настоящий opmem.Store; таблица replay — replay.migrate."""
    st = opmem.Store(path)
    for res, ts in scans:
        st.write(res, ts, 0)
    replay.migrate(st.db)
    st.db.commit()
    st.close()


def confirm_replay(path, token, drop, hours):
    con = sqlite3.connect(path)
    con.execute("INSERT INTO replay(token, chain, grp, scan_ts, mcap_scan, confirmed_ts, confirmed_drop, hours_after) "
                "VALUES (?, 'robinhood', 'danger', ?, 50000, ?, ?, ?)", (token, NOW - 86400, NOW - 3600, drop, hours))
    con.commit()
    con.close()


class MemoryDB(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "memory.db")
        memory_db(self.path, [
            (result(B, ticker="BBB", band="DANGER", score=20, holders=H[:3], snipers=H[:1], ops=[H[3:5]]), NOW - 5 * 86400),
            (result(B, ticker="BBB", band="RISKY", score=40), NOW - 4 * 86400),           # не первый скан
            (result(C, ticker="CCC", band="CLEAN", score=90, holders=[H[0]], snipers=[H[0]]), NOW - 3600),
            (result(D, ticker="DDD", holders=H[:1] + H[3:5], snipers=H[:1]), NOW - 7200),
            (result(E, dev=OTHER_DEV, ticker="EEE", holders=[H[0]], snipers=[H[0]]), NOW - 600),
            (result("0x" + "e6" * 20, dev=OTHER_DEV, holders=[H[0]], snipers=[H[0]]), NOW - 500),
            (result(A, ticker="AAA", holders=H[:6], snipers=H[:1], ops=[H[3:5]]), NOW - 60),
        ])
        confirm_replay(self.path, B, 0.94, 5.6)
        self.con = premium_memory.connect(self.path)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()


class TestDevHistory(MemoryDB):

    def test_other_tokens_of_dev(self):
        r = premium_memory.dev_history(self.con, A)
        self.assertEqual((r["known"], r["dev"], r["ticker"], r["total"], r["in_replay"]), (True, DEV, "AAA", 3, 1))
        self.assertEqual([t["token"] for t in r["tokens"]], [C, D, B])                     # новые первыми
        b = r["tokens"][2]
        self.assertEqual((b["ticker"], b["band"], b["score"], b["mcap_usd"], b["ts"]),
                         ("BBB", "DANGER", 20, 50_000.0, NOW - 5 * 86400))                 # первый скан
        self.assertEqual(b["replay"], {"drop": 0.94, "hours": 5.6})
        self.assertIsNone(r["tokens"][0]["replay"])
        text = T.dev_view(r | {"token": A})
        self.assertIn("This dev launched 3 other tokens we've seen, 1 of them is in Rug Replay.", text)
        self.assertIn("3. $BBB · ", text)
        self.assertIn("🔴 DANGER 20 · mcap $50.0K at scan\n   🔴 in Rug Replay (fell 94% within 6 hours)", text)

    def test_no_other_tokens_and_unknown(self):
        r = premium_memory.dev_history(self.con, E)                                         # у OTHER_DEV — один другой
        self.assertEqual(r["total"], 1)
        con = sqlite3.connect(self.path)
        con.execute("DELETE FROM wallets WHERE token = ?", ("0x" + "e6" * 20,))
        con.commit()
        con.close()
        r = premium_memory.dev_history(self.con, E)
        self.assertEqual(r["total"], 0)
        self.assertIn("No other tokens from this dev in our records yet.", T.dev_view(r | {"token": E}))
        self.assertEqual(premium_memory.dev_history(self.con, "0x" + "99" * 20), {"known": False})
        self.assertEqual(T.dev_view({"known": False}), T.DEV_UNKNOWN)

    def test_no_db_is_not_created(self):
        p = os.path.join(self.tmp.name, "nope.db")
        self.assertIsNone(premium_memory.connect(p))
        self.assertEqual(premium_memory.dev_history(None, A), {"known": False})
        self.assertFalse(os.path.exists(p))

    def test_read_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.con.execute("DELETE FROM scans")

    def test_queries_use_indexes(self):
        plans = {
            "dev": ("SELECT token, roles FROM wallets WHERE wallet = ? AND token != ?", (DEV, A)),
            "deployer": ("SELECT deployer, ticker FROM scans WHERE token = ? AND deployer IS NOT NULL "
                         "ORDER BY ts DESC LIMIT 1", (A,)),
            "holders": ("SELECT wallet, token, ts, roles FROM wallets WHERE wallet IN (?, ?) AND token != ?",
                        (H[0], H[1], A)),
            "first": ("SELECT token, chain, ts FROM scans WHERE token IN (?, ?) ORDER BY ts", (B, C)),
            "replay": ("SELECT token FROM replay WHERE token IN (?) AND confirmed_ts IS NOT NULL", (B,)),
        }
        for name, (sql, args) in plans.items():
            plan = " ".join(r[-1] for r in self.con.execute("EXPLAIN QUERY PLAN " + sql, args))
            self.assertIn("USING", plan, f"{name}: {plan}")                                 # индекс, не полный проход


class TestInsights(MemoryDB):

    def test_snipers_repeat_clusters(self):
        res = result(A, ticker="AAA", holders=H[:6], snipers=H[:1], ops=[H[3:5]])
        hs, sn, ops = premium_memory.from_result(res)
        r = premium_memory.insights(self.con, A, hs, sn, ops, NOW)
        self.assertTrue(r["notable"])
        self.assertEqual((r["holders"], r["repeat"]), (6, 5))                               # H0..H4 были в других
        self.assertEqual(r["snipers"], [{"wallet": H[0], "tokens": 5}])                     # C, D, E, e6 за сутки + этот
        self.assertEqual(len(r["clusters"]), 1)
        c = r["clusters"][0]
        self.assertEqual((c["wallets"], c["share_supply"]), (2, 0.12))
        self.assertEqual([(t["token"], t["ticker"], bool(t["replay"])) for t in c["tokens"]],
                         [(B, "BBB", True), (D, "DDD", False)])
        block = T.memory_block(r)
        self.assertIn("🧠 <b>Memory</b>", block)
        self.assertIn("• 5 of the top 6 holders were in other tokens we scanned", block)
        self.assertIn(f"• <code>{T.short(H[0])}</code> — sniper bot, seen in 5 tokens today", block)
        self.assertIn("• 2 linked wallets (12.0% of supply) were together in 2 other tokens: $BBB, $DDD "
                      "— 🔴 1 in Rug Replay", block)

    def test_nothing_notable(self):
        fresh = ["0x" + f"{i:02x}" * 20 for i in range(0xa0, 0xa5)]
        r = premium_memory.insights(self.con, A, fresh + [H[1]], set(), [], NOW)
        self.assertEqual((r["notable"], r["repeat"], r["snipers"], r["clusters"]), (False, 1, [], []))
        self.assertIsNone(T.memory_block(r))
        self.assertFalse(premium_memory.insights(None, A, H, set(), [], NOW)["notable"])

    def test_old_sniping_is_not_today(self):
        r = premium_memory.insights(self.con, A, [H[0]], set(), [], NOW + 2 * 86400)        # всё старше суток
        self.assertEqual(r["snipers"], [])

    def test_holders_from_db(self):
        hs, sn, ops = premium_memory.holders_from_db(self.con, A)
        self.assertEqual((sorted(hs), sn, [sorted(o["wallets"]) for o in ops]), (sorted(H[:6]), {H[0]}, [H[3:5]]))


class TestTrendingCounter(unittest.TestCase):

    def test_top_and_window(self):
        t = [1_000_000.0]
        c = premium_trending.Counter(clock=lambda: t[0])
        for tok, n in ((A, 3), (B, 5), (C, 1)):
            for _ in range(n):
                c.hit("robinhood", tok)
        self.assertEqual(c.top(2), [("robinhood", B, 5), ("robinhood", A, 3)])
        t[0] += 1800
        c.hit("robinhood", C)
        t[0] += 1900                                                                        # A и B старше часа
        self.assertEqual(c.top(), [("robinhood", C, 1)])


# ---------- сервер ----------

class Server(Site):
    ENV = {"PREMIUM_ENABLED": "true", "ALERTS_ENABLED": "true"} | ALL_NEW

    def setUp(self):
        super().setUp()
        self.mem = os.path.join(self.tmp.name, "memory.db")
        os.environ["OPMEM_DB_PATH"] = self.mem
        self.patches = [mock.patch.object(server, "TRENDING", premium_trending.Counter()),
                        mock.patch.object(server, "FRESH", premium.FreshLimits()),
                        mock.patch.dict(server.JOBS, clear=True), mock.patch.dict(server.BY_TOKEN, clear=True),
                        mock.patch.object(server, "_run", side_effect=self.fake_run)]
        for p in self.patches:
            p.start()
        self.runs = []

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        os.environ.pop("OPMEM_DB_PATH", None)
        super().tearDown()

    def fake_run(self, jid, token, prio=False):
        """Вместо скана: задача сразу готова с результатом (без потоков движка и сети)."""
        self.runs.append((token, prio))
        with server._lock:
            server.JOBS[jid] |= {"done": True, "result": result(token, ticker="T" + token[2:4], band="DANGER",
                                                                 score=15)}
            server._ACTIVE[0] -= 1

    def cached(self, token, band="OK", score=80, ticker="CCH"):
        jid = "j" + token[2:8]
        server.JOBS[jid] = {"token": token, "chain": "robinhood", "events": [], "done": True, "error": None,
                            "ts": time.time(), "result": result(token, band=band, score=score, ticker=ticker)}
        server.BY_TOKEN[token] = jid

    def scan(self, token, user=None, fresh=None, secret=SECRET):
        body = {"token": token} | ({"user_id": user} if user else {}) | ({"fresh": fresh} if fresh is not None else {})
        return self.req("/api/scan", body, secret=secret)


class TestScanPathUnchanged(Server):
    ENV = {"PREMIUM_ENABLED": "true", "ALERTS_ENABLED": "true"}

    def test_all_off_is_the_old_call(self):
        calls = []
        with mock.patch.object(server, "start_scan", side_effect=lambda *a, **k: calls.append((a, k)) or "jid"), \
                mock.patch.object(server, "premium_store", side_effect=AssertionError("premium db read")):
            self.assertEqual(self.scan(A, user=U1, fresh=True), (200, {"job": "jid"}))
            self.assertEqual(self.scan(A, secret=None), (200, {"job": "jid"}))
        self.assertEqual(calls[0], ((A, None), {"prio": 0}))                               # бот: без лимита, как было
        self.assertEqual(calls[1][1], {"prio": 0})
        self.assertEqual(calls[1][0][1][1], server.SCAN_RATE_PER_MIN)                       # сайт: лимит по IP
        self.assertEqual(server.TRENDING.top(), [])                                          # счётчик не идёт
        for path in ("/api/premium/dev", "/api/premium/memory", "/api/premium/trending"):
            self.assertEqual(self.req(path, {"user_id": U1, "token": A})[0], 404)
        code, cfg = self.req("/api/config")
        self.assertFalse({k for k in cfg if k.startswith("premium_")})

    def test_premium_off_ignores_new_flags(self):
        with mock.patch.dict(os.environ, {"PREMIUM_ENABLED": "false"} | ALL_NEW):
            self.assertEqual(premium.features(), {})
            self.assertIsNone(server.bot_user({"X-Alerts-Secret": SECRET}, {"user_id": U1}))

    def test_reuse_fresh(self):
        self.cached(A)
        with server._lock:
            self.assertEqual(server._reuse(A, time.time()), server.BY_TOKEN[A])
            self.assertIsNone(server._reuse(A, time.time(), fresh=True))                    # готовый кэш — мимо
            server.JOBS[server.BY_TOKEN[A]]["done"] = False
            self.assertEqual(server._reuse(A, time.time(), fresh=True), server.BY_TOKEN[A])  # идущий — подключиться


class TestTrendingAPI(Server):

    def test_counts_cached_and_new(self):
        self.verify(U1, W1, 600_000 * E18)
        self.cached(A, band="CLEAN", score=91, ticker="AAA")
        for _ in range(3):
            self.assertEqual(self.scan(A, secret=None)[0], 200)                             # из кэша — тоже считается
        self.scan(B, secret=None)                                                            # новый скан
        code, r = self.req("/api/premium/trending", {"user_id": U1})
        self.assertEqual(code, 200)
        self.assertEqual([(i["token"], i["scans"], i["band"], i["score"]) for i in r["items"]],
                         [(A, 3, "CLEAN", 91), (B, 1, "DANGER", 15)])
        self.assertEqual(self.runs, [(B, 0)])                                                # один настоящий новый скан
        self.scan(B, secret=None)
        self.scan(B, secret=None)
        self.scan(B, secret=None)
        self.assertEqual(self.req("/api/premium/trending", {"user_id": U1})[1], r)          # кэш 60 с
        server._RESP.clear()
        self.assertEqual(self.req("/api/premium/trending", {"user_id": U1})[1]["items"][0]["token"], B)
        self.assertEqual(self.req("/api/premium/trending", {"user_id": U2})[0], 403)       # не премиум
        text, markup = T.trending_view(r)
        self.assertIn("1. $AAA · 🟢 CLEAN 91/100 · 3 scans", text)
        self.assertEqual(markup["inline_keyboard"][0][0], {"text": "Scan $AAA", "callback_data": f"sc:{A}"})


class TestFreshAPI(Server):
    ENV = Server.ENV | {"PREMIUM_BOT_RATE": "false"}       # лимит Fresh отдельно от лимита новых сканов

    def test_fresh_skips_cache_with_limits(self):
        self.verify(U1, W1, 600_000 * E18)
        self.cached(A)
        old = server.BY_TOKEN[A]
        self.assertEqual(self.scan(A, user=U1)[1]["job"], old)                              # обычный — из кэша
        code, r = self.scan(A, user=U1, fresh=True)
        self.assertEqual(code, 200)
        self.assertNotEqual(r["job"], old)                                                   # новый скан мимо кэша
        self.assertEqual([t for t, _ in self.runs], [A])
        code, r = self.scan(A, user=U1, fresh=True)
        self.assertEqual((code, r["error"]), (429, "fresh_limited"))
        self.assertIn("once every 2 minutes", r["message"])
        for i in range(19):                                                                  # ещё 19 разных — 20 в час
            self.assertEqual(self.scan("0x" + f"{0x10 + i:02x}" * 20, user=U1, fresh=True)[0], 200)
        code, r = self.scan(B, user=U1, fresh=True)
        self.assertEqual((code, r["error"]), (429, "fresh_limited"))
        self.assertIn("20 fresh scans this hour", r["message"])

    def test_fresh_premium_only(self):
        self.verify(U2, W2, 1 * E18)
        self.assertEqual(self.scan(A, user=U2, fresh=True)[0], 403)
        self.assertEqual(self.scan(A, fresh=True, secret=None)[0], 403)                      # сайт — fresh нет


class TestBotRateAPI(Server):

    def test_user_limits_only_new_scans(self):
        self.verify(U1, W1, 600_000 * E18)
        self.cached(A)
        for i in range(3):
            self.assertEqual(self.scan("0x" + f"{0x20 + i:02x}" * 20, user=U2)[0], 200)
        for _ in range(5):
            self.assertEqual(self.scan(A, user=U2)[0], 200)                                 # из кэша — не тратит
        code, r = self.scan(B, user=U2)
        self.assertEqual((code, r["error"], r["upsell"]), (429, "user_rate_limited", True))
        self.assertEqual(r["message"], "You've reached the limit of 3 scans per minute.\n\n"
                                       "Premium holders can scan up to 15 tokens per minute.")
        for i in range(15):
            self.assertEqual(self.scan("0x" + f"{0x30 + i:02x}" * 20, user=U1)[0], 200)     # премиум — 15
        code, r = self.scan(B, user=U1)
        self.assertEqual((code, r["upsell"]), (429, False))
        self.assertEqual(r["message"], "You've reached the limit of 15 scans per minute, try again in a moment.")
        from bot.api import CrawlScan
        api = CrawlScan(self.base, alerts_secret=SECRET)                                    # клиент бота против сервера
        with self.assertRaises(TooFast) as cm:
            api.scan(C, user_id=U2)
        self.assertEqual((str(cm.exception).split("\n")[0], cm.exception.upsell),
                         ("You've reached the limit of 3 scans per minute.", True))

    def test_limit_texts(self):
        with mock.patch.dict(os.environ, {"BOT_SCAN_RATE_PER_MIN": "4", "BOT_SCAN_RATE_PREMIUM_PER_MIN": "25"}):
            self.assertEqual(server.too_fast(False), ("You've reached the limit of 4 scans per minute.\n\n"
                                                      "Premium holders can scan up to 25 tokens per minute.", True))
            self.assertEqual(server.too_fast(True),
                             ("You've reached the limit of 25 scans per minute, try again in a moment.", False))
            with mock.patch.dict(os.environ, {"PREMIUM_ENABLED": "false"}):              # без премиума — одна строка
                self.assertEqual(server.too_fast(False), ("You've reached the limit of 4 scans per minute.", False))

    def test_env_limits_and_ip_limit_unchanged(self):
        with mock.patch.dict(os.environ, {"BOT_SCAN_RATE_PER_MIN": "1", "BOT_SCAN_RATE_PREMIUM_PER_MIN": "2"}):
            self.assertEqual((premium.bot_rate(False), premium.bot_rate(True)), (1, 2))
            self.assertEqual(self.scan(A, user=U2)[0], 200)
            self.assertEqual(self.scan(B, user=U2)[0], 429)
        for i in range(server.SCAN_RATE_PER_MIN):                                            # сайт — прежний лимит IP
            self.assertEqual(self.scan("0x" + f"{0x40 + i:02x}" * 20, secret=None)[0], 200)
        code, r = self.scan(C, secret=None)
        self.assertEqual((code, r["error"]), (429, "rate_limited"))


class TestDevMemoryAPI(Server):

    def test_dev_and_memory(self):
        memory_db(self.mem, [(result(B, ticker="BBB", holders=H[:3], snipers=H[:1], ops=[H[3:5]]), NOW - 3600),
                             (result(A, ticker="AAA", holders=H[:5]), NOW - 60)])
        self.verify(U1, W1, 600_000 * E18)
        code, r = self.req("/api/premium/dev", {"user_id": U1, "token": A.upper().replace("0X", "0x")})
        self.assertEqual((code, r["known"], r["total"], r["tokens"][0]["token"]), (200, True, 1, B))
        self.cached(A)
        server.JOBS[server.BY_TOKEN[A]]["result"] = result(A, holders=H[:5], ops=[H[3:5]])
        code, r = self.req("/api/premium/memory", {"user_id": U1, "token": A})
        self.assertEqual((code, r["repeat"], len(r["clusters"])), (200, 5, 1))              # из памяти сервера
        self.assertEqual(self.req("/api/premium/dev", {"user_id": U2, "token": A})[0], 403)
        self.assertEqual(self.req("/api/premium/dev", {"user_id": U1, "token": "nope"})[0], 400)

    def test_no_memory_db(self):
        self.verify(U1, W1, 600_000 * E18)
        code, r = self.req("/api/premium/dev", {"user_id": U1, "token": A})
        self.assertEqual((code, r["known"]), (200, False))
        code, r = self.req("/api/premium/memory", {"user_id": U1, "token": A})
        self.assertEqual((code, r["notable"]), (200, False))
        self.assertFalse(os.path.exists(self.mem))                                           # файл не создан


# ---------- бот ----------

class MoreAPI(FeatAPI):
    def __init__(self, feats=(), **kw):
        super().__init__(feats, premium_users=[U1], **kw)
        self.scans, self.memory_reply = [], {"notable": True, "holders": 20, "repeat": 4, "snipers": [], "clusters": []}

    def scan(self, token, **kw):
        self.scans.append((token, kw))
        return "job"

    def premium_dev(self, user_id, token):
        if user_id != U1:
            return {"status": 403, "error": "not premium"}
        return {"known": True, "token": token, "dev": DEV, "ticker": "AAA", "total": 0, "in_replay": 0, "tokens": []}

    def premium_memory(self, user_id, token):
        return self.memory_reply

    def premium_trending(self, user_id):
        return {"items": [{"token": A, "chain": "robinhood", "scans": 2, "band": "OK", "score": 70, "ticker": "AAA"}],
                "window_min": 60}


class TestBot(unittest.TestCase):

    def make(self, feats=tuple(k.lower() for k in ()), **kw):
        api = MoreAPI(feats, **kw)
        tg = TGStub()
        return bm.Bot(tg, api, username="b", spawn=lambda f: f(), sleep=lambda s: None, log=lambda m: None,
                      admin_ids=set(), banner=None), tg, api

    FEATS = ("premium_devcheck", "premium_memory", "premium_trending", "premium_fresh", "premium_bot_rate")

    def test_menu_lines_only_enabled(self):
        st = {"linked": False, "min_tokens": 500_000}
        text = T.premium_view(st | {f: True for f in self.FEATS}, NOW)[0]
        for line in ("Fresh scan — rescan any token instantly, skipping the cache",
                     "Memory insights — see when top holders are known sniper bots or repeat wallets",
                     "/dev — see what else this token's dev launched, and which of those ended up in Rug Replay",
                     "/trending — the most scanned tokens on CrawlScan right now"):
            self.assertIn(line, text)
        text = T.premium_view(st, NOW)[0]
        for word in ("Fresh scan", "Memory insights", "/dev", "/trending"):
            self.assertNotIn(word, text)
        prem = {"linked": True, "premium": True, "wallet": W1, "premium_trending": True}
        self.assertEqual(T.premium_view(prem, NOW)[1]["inline_keyboard"][-1][0], T.TRENDING_BUTTON)

    def test_verdict_buttons_and_memory_for_premium(self):
        bot, tg, api = self.make(self.FEATS)
        bot.run_scan(U1, 5, A, False, True, U1)
        rows, tok = tg.markups[-1]["inline_keyboard"], TOKENS[0]                            # адрес — из результата скана
        self.assertEqual(rows[-1], [{"text": "Dev history", "callback_data": f"dev:{tok}"},
                                    {"text": "Fresh scan", "callback_data": f"fresh:{tok}"}])
        self.assertEqual(tg.n, 2)                                                            # вердикт, затем + Memory
        self.assertIn("⭐ Premium", tg.texts[-2])
        self.assertTrue(tg.texts[-1].endswith("🧠 <b>Memory</b>\n• 4 of the top 20 holders were in other tokens we "
                                              "scanned"))
        api.memory_reply = {"notable": False}
        bot.run_scan(U1, 5, A, False, True, U1)
        self.assertEqual(tg.n, 3)                                                            # нечего показать — без правки
        bot.run_scan(U2, 5, A, False, True, U2)
        self.assertNotIn("Dev history", str(tg.markups[-1]))                                 # не премиум
        self.assertNotIn("Memory", tg.texts[-1])

    def test_all_off_verdict_as_before(self):
        bot, tg, api = self.make()
        bot.run_scan(U1, 5, A, False, True, U1)
        self.assertNotIn("Dev history", str(tg.markups[-1]))
        self.assertEqual(tg.n, 1)
        self.assertEqual(api.scans[-1], (A, {}))
        for cmd in (f"/dev {A}", "/trending"):
            bot.handle_update(private(cmd))
            self.assertEqual(tg.texts[-1], T.HINT)

    def test_dev_trending_commands(self):
        bot, tg, api = self.make(self.FEATS)
        bot.handle_update(private(f"/dev {A}"))
        self.assertIn("No other tokens from this dev in our records yet.", tg.texts[-1])
        bot.handle_update(private("/dev"))
        self.assertEqual(tg.texts[-1], T.DEV_USAGE)
        bot.handle_update(private(f"/dev {A}", user=U2))
        self.assertEqual(tg.texts[-1], T.PREMIUM_ONLY)
        bot.handle_update(callback(f"dev:{A}"))
        self.assertIn("👤 <b>Dev history</b> · $AAA", tg.texts[-1])
        bot.handle_update(private("/trending"))
        self.assertIn("1. $AAA · 🟡 OK 70/100 · 2 scans", tg.texts[-1])
        bot.handle_update(callback("trending"))
        self.assertIn("Trending on CrawlScan", tg.texts[-1])
        bot.handle_update(callback(f"sc:{A}"))
        self.assertEqual(bot.jobs.get()[2], A)

    def test_fresh_button(self):
        bot, tg, api = self.make(self.FEATS)
        bot.handle_update(callback(f"fresh:{A}"))
        job = bot.jobs.get()
        self.assertEqual(job[-1], True)
        bot.run_scan(*job)
        self.assertEqual(api.scans[-1], (A, {"user_id": U1, "fresh": True}))

    def test_too_fast_text_and_no_bot_cooldown(self):
        bot, tg, api = self.make(self.FEATS)
        bot.alerts_on()
        for i in range(3):                                                                   # свой лимит 20 с — выключен
            bot.request_scan(U2, U2, "0x" + f"{0x50 + i:02x}" * 20, None, private=True)
        self.assertEqual(bot.jobs.qsize(), 3)

        msg = "You've reached the limit of 3 scans per minute.\n\nPremium holders can scan up to 15 tokens per minute."

        def limited(token, **kw):
            raise TooFast(msg, upsell=True)
        api.scan = limited
        bot.run_scan(U2, 5, A, False, True, U2)
        self.assertEqual(tg.texts[-1], msg)
        self.assertEqual(tg.markups[-1], {"inline_keyboard": [[{"text": "⭐ Premium features", "callback_data": "premium"}]]})
        bot.handle_update(callback("premium", user=U2))                                     # кнопка открывает меню
        self.assertTrue(tg.texts[-1].startswith("⭐ <b>Premium features for $CrawlScan holders</b>"))
        api.scan = lambda token, **kw: (_ for _ in ()).throw(
            TooFast("You've reached the limit of 15 scans per minute, try again in a moment."))
        bot.run_scan(U1, 5, A, False, True, U1)
        self.assertEqual(tg.texts[-1], "You've reached the limit of 15 scans per minute, try again in a moment.")
        self.assertIsNone(tg.markups[-1])                                                    # премиум — без кнопки и значка
        bot, tg, api = self.make()
        bot.request_scan(U2, U2, A, None, private=True)
        bot.request_scan(U2, U2, B, None, private=True)
        self.assertEqual(tg.texts[-1], T.wait(20))                                           # выключено — как раньше

    def test_commands_menu(self):
        bot, tg, api = self.make(self.FEATS)
        sent = []
        bot.call = lambda method, upload=None, **p: sent.append((method, p))
        bot.start_workers = lambda: None
        bot.tg.call = lambda method, **p: (_ for _ in ()).throw(KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            bot.run()
        cmds = [c["command"] for c in dict(sent)["setMyCommands"]["commands"]]
        self.assertEqual(cmds[-2:], ["dev", "trending"])


if __name__ == "__main__":
    unittest.main()
