"""Тесты Rug Replay (replay.py): миграция memory.db, рынок в scans, правила попадания (DANGER, порог капы, 90%,
7 дней), статистика и порог 20, поток проверки (пачки, потолок clean, ошибки), выключатель. Без сети."""
import io, json, os, sqlite3, tempfile, threading, unittest, urllib.error, urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from unittest import mock

import fakes  # noqa: F401  (до server: никаких настоящих RPC)
import opmem
import replay
import server

DAY, H = 86400, 3600
NOW = 1_800_000_000
T = ["0x" + f"{i:040x}" for i in range(1, 200)]

OLD_SCHEMA = """
CREATE TABLE scans (
  token TEXT NOT NULL, chain TEXT NOT NULL, launchpad TEXT, ts INTEGER NOT NULL, band TEXT NOT NULL, score INTEGER,
  limited INTEGER NOT NULL, partial INTEGER NOT NULL, holders_total INTEGER, unread INTEGER, deployer TEXT);
CREATE INDEX scans_token ON scans(token, ts);
CREATE TABLE operators (
  token TEXT NOT NULL, ts INTEGER NOT NULL, operator_id TEXT NOT NULL, wallets TEXT NOT NULL, n_wallets INTEGER NOT NULL,
  level TEXT, kinds TEXT, share REAL, share_supply REAL);
CREATE TABLE wallets (
  wallet TEXT NOT NULL, token TEXT NOT NULL, ts INTEGER NOT NULL, roles TEXT NOT NULL, operator_id TEXT,
  share REAL, share_supply REAL);
"""


def scan_result(token, band="DANGER", score=12, mcap=100_000.0, rug=0.72, **kw):
    return {"token": token, "chain": "robinhood", "band": band, "score": score, "holders_total": 50,
            "launch": {"deployer": "0x" + "de" * 20, "block": 1}, "holders": [], "links": [], "operators": [],
            "unread": [], "market_source": "gt", "rug": {"drop": rug} if rug else None,
            "header": {"name": "Rug Coin", "ticker": "RUG", "price_usd": 0.0001, "mcap_usd": mcap,
                       "fdv_usd": mcap * 1.1 if mcap else None, "liquidity_usd": 20_000.0}} | kw


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "memory.db")
        self.mem = opmem.Store(self.path)

    def tearDown(self):
        self.mem.close()
        self.tmp.cleanup()

    def add(self, token, ts, **kw):
        self.assertTrue(self.mem.write(scan_result(token, **kw), ts, 0))

    def store(self):
        st = replay.Store(self.path)
        self.addCleanup(st.close)
        return st

    def row(self, token):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute("SELECT * FROM replay WHERE token = ?", (token,)).fetchone()
            return dict(r) if r else None
        finally:
            con.close()


class TestMigration(unittest.TestCase):

    def test_old_db_gets_market_columns_without_data_loss(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "memory.db")
            con = sqlite3.connect(path)
            con.executescript(OLD_SCHEMA)
            con.execute("INSERT INTO scans VALUES (?, 'robinhood', NULL, 100, 'DANGER', 10, 0, 0, 40, 2, NULL)", (T[0],))
            con.execute("INSERT INTO wallets VALUES ('0xw', ?, 100, 'top_holder', NULL, 0.1, 0.05)", (T[0],))
            con.commit()
            con.close()

            st = opmem.Store(path)        # открытие = миграция
            st.write(scan_result(T[1]), 200, 0)
            st.close()
            st = opmem.Store(path)        # повторное открытие ничего не ломает
            st.close()
            rs = replay.Store(path)       # и поток Rug Replay мигрирует ту же базу
            rs.close()

            con = sqlite3.connect(path)
            con.row_factory = sqlite3.Row
            cols = [r[1] for r in con.execute("PRAGMA table_info(scans)")]
            self.assertEqual(cols, list(opmem.SCAN_COLUMNS))
            rows = [dict(r) for r in con.execute("SELECT * FROM scans ORDER BY ts")]
            self.assertEqual(len(rows), 2)
            self.assertEqual((rows[0]["token"], rows[0]["band"], rows[0]["score"], rows[0]["holders_total"]),
                             (T[0], "DANGER", 10, 40))
            self.assertIsNone(rows[0]["mcap_usd"])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM wallets WHERE token = ?", (T[0],)).fetchone()[0], 1)
            r = rows[1]
            self.assertEqual((r["mcap_usd"], r["fdv_usd"], r["price_usd"], r["liquidity_usd"], r["market_source"],
                              r["rug_drop"], r["name"], r["ticker"]),
                             (100_000.0, 110_000.00000000001, 0.0001, 20_000.0, "gt", 0.72, "Rug Coin", "RUG"))
            con.close()

    def test_rows_without_market(self):
        res = scan_result(T[0], band="OK", rug=None)
        res["header"] = {}
        res.pop("market_source")
        scan, _, _ = opmem.rows(res, 1)
        self.assertEqual({c: scan[c] for c, _ in opmem.MARKET_COLUMNS}, {c: None for c, _ in opmem.MARKET_COLUMNS})

    def test_header_has_fdv(self):
        import engine
        self.assertEqual(engine._header({"fdv_usd": 5.0, "mcap_usd": 4.0})["fdv_usd"], 5.0)


class TestRules(Base):

    def test_who_is_tracked(self):
        self.add(T[0], NOW - 2 * DAY)                          # DANGER, $100k — да
        self.add(T[1], NOW - 2 * DAY, mcap=24_999.0)           # ниже порога — нет
        self.add(T[2], NOW - 2 * DAY, mcap=None)               # без рынка — нет
        self.add(T[3], NOW - 8 * DAY)                          # старше 7 дней — нет
        self.add(T[4], NOW - 2 * DAY, band="RISKY", score=45)  # RISKY — нет
        self.add(T[5], NOW - 2 * DAY, band="CLEAN", score=85, rug=None)   # clean — для статистики
        self.add(T[6], NOW - 2 * DAY, band="OK", score=65, rug=None)
        self.add(T[7], NOW - 2 * DAY, mcap=25_000.0)           # ровно порог — да
        st = self.store()
        st.sync(NOW, 25_000)
        self.assertEqual(self.row(T[0])["grp"], "danger")
        self.assertEqual(self.row(T[7])["grp"], "danger")
        for t in (T[1], T[2], T[3], T[4]):
            self.assertIsNone(self.row(t), t)
        self.assertEqual((self.row(T[5])["grp"], self.row(T[6])["grp"]), ("clean", "clean"))
        r = self.row(T[0])
        self.assertEqual((r["mcap_scan"], r["rug_drop"], r["name"], r["ticker"], r["score"]),
                         (100_000.0, 0.72, "Rug Coin", "RUG", 12))

    def test_first_danger_scan_is_reference_and_danger_beats_clean(self):
        self.add(T[0], NOW - 3 * DAY, band="CLEAN", score=85, mcap=50_000.0, rug=None)
        st = self.store()
        st.sync(NOW, 25_000)
        self.assertEqual(self.row(T[0])["grp"], "clean")
        self.add(T[0], NOW - 2 * DAY, mcap=80_000.0)
        self.add(T[0], NOW - 1 * DAY, mcap=30_000.0)
        st.sync(NOW, 25_000)
        r = self.row(T[0])
        self.assertEqual((r["grp"], r["mcap_scan"], r["scan_ts"]), ("danger", 80_000.0, NOW - 2 * DAY))

    def test_confirmed_at_90_percent(self):
        self.add(T[0], NOW - 2 * DAY)
        self.add(T[1], NOW - 2 * DAY)
        st = self.store()
        st.sync(NOW, 25_000)
        self.assertFalse(st.update(T[0], 10_001.0, NOW - DAY))   # −89.999% — ещё нет
        self.assertFalse(st.update(T[1], 50_000.0, NOW - DAY))
        self.assertTrue(st.update(T[0], 10_000.0, NOW))          # −90% — да
        self.assertTrue(st.update(T[1], 2_000.0, NOW))
        r = self.row(T[0])
        self.assertEqual((r["confirmed_ts"], r["confirmed_mcap"], r["confirmed_drop"], r["hours_after"]),
                         (NOW, 10_000.0, 0.9, 48.0))
        self.assertFalse(st.update(T[0], 1_000.0, NOW + H))       # уже подтверждён — второй раз нет
        r = self.row(T[0])
        self.assertEqual((r["confirmed_ts"], r["min_mcap"], r["last_mcap"], r["checks"]), (NOW, 1_000.0, 1_000.0, 3))

    def test_minimum_is_kept(self):
        self.add(T[0], NOW - 2 * DAY)
        st = self.store()
        st.sync(NOW, 25_000)
        st.update(T[0], 40_000.0, NOW - 30 * H)
        st.update(T[0], 90_000.0, NOW - 20 * H)
        r = self.row(T[0])
        self.assertEqual((r["min_mcap"], r["min_ts"], r["last_mcap"]), (40_000.0, NOW - 30 * H, 90_000.0))

    def test_no_confirm_after_7_days_and_not_due(self):
        self.add(T[0], NOW - 2 * DAY)
        st = self.store()
        st.sync(NOW - DAY, 25_000)
        later = NOW - 2 * DAY + 7 * DAY + 60
        self.assertEqual(st.due("danger", NOW, 3600, 100), [(T[0], "robinhood")])
        self.assertEqual(st.due("danger", later, 3600, 100), [])
        self.assertFalse(st.update(T[0], 1.0, later))
        self.assertIsNone(self.row(T[0])["confirmed_ts"])

    def test_clean_is_never_confirmed(self):
        self.add(T[0], NOW - 2 * DAY, band="CLEAN", score=90, rug=None)
        st = self.store()
        st.sync(NOW, 25_000)
        self.assertFalse(st.update(T[0], 1.0, NOW))
        self.assertIsNone(self.row(T[0])["confirmed_ts"])
        self.assertEqual(self.row(T[0])["min_mcap"], 1.0)

    def test_due_respects_interval(self):
        self.add(T[0], NOW - 2 * DAY)
        self.add(T[1], NOW - 2 * DAY)
        st = self.store()
        st.sync(NOW, 25_000)
        st.update(T[0], 50_000.0, NOW - 10 * 60)
        self.assertEqual(st.due("danger", NOW, 3600, 100), [(T[1], "robinhood")])
        self.assertEqual(sorted(st.due("danger", NOW + 3600, 3600, 100)), [(T[0], "robinhood"), (T[1], "robinhood")])

    def test_api_items(self):
        for i in range(3):
            self.add(T[i], NOW - 2 * DAY)
        st = self.store()
        st.sync(NOW, 25_000)
        st.update(T[0], 5_000.0, NOW - 10 * H)
        st.update(T[1], 3_000.0, NOW - 5 * H)
        st.update(T[1], 4_000.0, NOW - 4 * H)
        st.update(T[2], 60_000.0, NOW - 4 * H)
        body = replay.api(self.path, NOW)
        self.assertEqual([x["token"] for x in body["items"]], [T[1], T[0]])   # новые сверху
        x = body["items"][0]
        self.assertEqual((x["mcap_scan"], x["mcap_now"], x["mcap_min"], x["drop"], x["drop_now"], x["hours_after"],
                          x["band"], x["score"], x["rug_drop"], x["ticker"]),
                         (100_000.0, 4_000.0, 3_000.0, 0.97, 0.96, 43.0, "DANGER", 12, 0.72, "RUG"))
        self.assertEqual(body["since"], NOW - 2 * DAY)
        json.dumps(body)

    def test_api_without_db(self):
        path = os.path.join(self.tmp.name, "none", "memory.db")
        body = replay.api(path, NOW)
        self.assertEqual((body["items"], body["since"], body["stats"]["ready"]), ([], None, False))
        self.assertFalse(os.path.exists(path))


class TestStats(Base):

    def fill(self, grp, n, fell, age=2 * DAY):
        band = "DANGER" if grp == "danger" else "CLEAN"
        base = len(self._toks) if hasattr(self, "_toks") else 0
        self._toks = getattr(self, "_toks", []) + T[base:base + n]
        for i, t in enumerate(T[base:base + n]):
            self.add(t, NOW - age, band=band)
        return T[base:base + n], fell

    def run_stats(self, sets):
        st = self.store()
        st.sync(NOW, 25_000)
        for toks, fell in sets:
            for i, t in enumerate(toks):
                st.update(t, 5_000.0 if i < fell else 70_000.0, NOW - H)
        return st.stats(NOW)

    def test_ready_at_20_each(self):
        s = self.run_stats([self.fill("danger", 20, 8), self.fill("clean", 20, 1)])
        self.assertTrue(s["ready"])
        self.assertEqual(s["danger"], {"tokens": 20, "fell": 8, "share": 0.4})
        self.assertEqual(s["clean"], {"tokens": 20, "fell": 1, "share": 0.05})

    def test_collecting_below_20(self):
        s = self.run_stats([self.fill("danger", 25, 8), self.fill("clean", 19, 1)])
        self.assertFalse(s["ready"])
        self.assertEqual((s["danger"]["tokens"], s["clean"]["tokens"]), (25, 19))

    def test_only_older_than_24h_and_checked(self):
        young = self.fill("danger", 5, 5, age=20 * H)       # моложе 24 ч — не в статистике
        old = self.fill("danger", 4, 1)
        unchecked = self.fill("danger", 3, 0)              # ни разу не проверены — не в статистике
        st = self.store()
        st.sync(NOW, 25_000)
        for toks, fell in (young, old):
            for i, t in enumerate(toks):
                st.update(t, 5_000.0 if i < fell else 70_000.0, NOW - H)
        s = st.stats(NOW)
        self.assertEqual(s["danger"], {"tokens": 4, "fell": 1, "share": 0.25})
        self.assertEqual(s["clean"], {"tokens": 0, "fell": 0, "share": None})
        self.assertEqual(len(unchecked[0]), 3)


class TestFetch(unittest.TestCase):

    def test_batch_parse(self):
        a, b, c = T[0], T[1], T[2]
        pairs = [
            {"chainId": "robinhood", "baseToken": {"address": a.upper().replace("0X", "0x")}, "marketCap": 5000,
             "fdv": 6000, "liquidity": {"usd": 100}},
            {"chainId": "robinhood", "baseToken": {"address": a}, "marketCap": 7000, "liquidity": {"usd": 900}},
            {"chainId": "robinhood", "baseToken": {"address": b}, "fdv": "1234.5", "liquidity": {"usd": 10}},
            {"chainId": "robinhood", "baseToken": {"address": "0xweth"}, "quoteToken": {"address": c},
             "marketCap": 9e9, "liquidity": {"usd": 1e6}},                       # c только quote — нет капы
            {"chainId": "ethereum", "baseToken": {"address": c}, "marketCap": 1, "liquidity": {"usd": 1}},
        ]
        with mock.patch.object(replay.market, "_ds", return_value=pairs) as ds:
            out = replay.fetch_mcaps("robinhood", [a, b, c])
        self.assertEqual(out, {a: 7000.0, b: 1234.5})
        url = ds.call_args[0][0]
        self.assertEqual(url, f"https://api.dexscreener.com/tokens/v1/robinhood/{a},{b},{c}")

    def test_solana_case_sensitive_and_unknown_chain(self):
        mint = "fjKUqPWK9m331Y5TZZNFismtqoP2MGWAMHEkB62pump"
        pairs = [{"chainId": "solana", "baseToken": {"address": mint}, "marketCap": 42, "liquidity": {"usd": 1}},
                 {"chainId": "solana", "baseToken": {"address": mint.lower()}, "marketCap": 1, "liquidity": {"usd": 9}}]
        with mock.patch.object(replay.market, "_ds", return_value=pairs):
            self.assertEqual(replay.fetch_mcaps("solana", [mint]), {mint: 42.0})
        with mock.patch.object(replay.market, "_ds") as ds:
            self.assertEqual(replay.fetch_mcaps("base", [T[0]]), {})
            ds.assert_not_called()


class TestChecker(Base):

    def checker(self, fetch, **cfg):
        c = replay.Checker(self.path, cfg={"check_min": 60, "min_mcap": 25_000, "max_clean_per_hour": 300} | cfg,
                           fetch=fetch, clock=lambda: self.now, sleep=lambda s: self.sleeps.append(s))
        self.addCleanup(lambda: c.store and c.store.close())
        return c

    def setUp(self):
        super().setUp()
        self.now, self.sleeps, self.calls = NOW, [], []

    def test_batches_cap_and_confirm(self):
        for t in T[:45]:
            self.add(t, NOW - 2 * DAY)
        for t in T[45:65]:
            self.add(t, NOW - 2 * DAY, band="CLEAN", rug=None)

        def fetch(chain, toks):
            self.calls.append(list(toks))
            return {t: (1_000.0 if t == T[0] else 80_000.0) for t in toks if t != T[1]}

        c = self.checker(fetch, max_clean_per_hour=12)
        with redirect_stdout(io.StringIO()) as out:
            c.tick()
        self.assertTrue(all(len(x) <= replay.BATCH for x in self.calls))
        checked = [t for x in self.calls for t in x]
        self.assertEqual(len(checked), 45 + 12)               # все danger + потолок clean
        self.assertEqual(len(self.sleeps), len(self.calls) - 1)   # пауза между запросами
        self.assertIsNotNone(self.row(T[0])["confirmed_ts"])
        self.assertEqual((self.row(T[1])["last_check"], self.row(T[1])["checks"]), (NOW, 0))   # нет капы — не ноль
        self.assertIn("1 confirmed", out.getvalue())

        self.calls.clear()                                     # через 10 минут: никого не пора, потолок исчерпан
        self.now = NOW + 600
        with redirect_stdout(io.StringIO()):
            c.tick()
        self.assertEqual(self.calls, [])

        self.calls.clear()                                     # через час: снова все danger и следующие clean
        self.now = NOW + 3600 + 1
        with redirect_stdout(io.StringIO()):
            c.tick()
        checked = [t for x in self.calls for t in x]
        self.assertEqual(sum(t in T[45:65] for t in checked), 12)
        self.assertEqual(sum(t in T[:45] for t in checked), 45)

    def test_errors_only_logged(self):
        self.add(T[0], NOW - 2 * DAY)

        def fetch(chain, toks):
            raise urllib.error.HTTPError("u", 503, "x", {}, None)

        c = self.checker(fetch)
        with redirect_stdout(io.StringIO()) as out:
            c.tick()
        self.assertIn("replay: dexscreener robinhood (1 tokens) failed: http 503", out.getvalue())
        self.assertIsNone(self.row(T[0])["last_check"])        # не проверен — в следующий раз снова

    def test_run_survives_tick_exception(self):
        c = self.checker(lambda ch, t: {})
        n = {"ticks": 0}

        def boom():
            n["ticks"] += 1
            if n["ticks"] >= 2:
                c.stop()
            raise RuntimeError("db gone")

        c.tick = boom
        with redirect_stdout(io.StringIO()) as out:
            c.run(first_delay=0)
        self.assertEqual(n["ticks"], 2)
        self.assertIn("replay: check failed: RuntimeError: db gone", out.getvalue())


class TestServer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "memory.db")
        server._RESP.clear()

    def tearDown(self):
        server._RESP.clear()
        self.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read(), dict(e.headers)

    def test_disabled_no_thread_no_page(self):
        with mock.patch.dict(os.environ, {"REPLAY_ENABLED": "", "OPMEM_DB_PATH": self.path}), \
                mock.patch.object(replay, "Checker") as checker:
            self.assertIsNone(server.start_replay_checker())
            checker.assert_not_called()
            self.assertEqual(self.get("/replay")[0], 404)
            self.assertEqual(self.get("/api/replay")[0], 404)
            self.assertFalse(json.loads(self.get("/api/config")[1])["replay"])
        self.assertFalse(any(t.name == "replay-check" for t in threading.enumerate()))
        self.assertFalse(os.path.exists(self.path))

    def test_enabled(self):
        st = opmem.Store(self.path)
        st.write(scan_result(T[0]), NOW - 2 * DAY, 0)
        st.close()
        rs = replay.Store(self.path)
        rs.sync(NOW, 25_000)
        rs.update(T[0], 1_000.0, NOW)
        rs.close()
        with mock.patch.dict(os.environ, {"REPLAY_ENABLED": "true", "OPMEM_DB_PATH": self.path}):
            code, body, headers = self.get("/api/replay")
            self.assertEqual(code, 200)
            self.assertEqual(headers["cache-control"], "public, max-age=60")
            d = json.loads(body)
            self.assertEqual([x["token"] for x in d["items"]], [T[0]])
            self.assertFalse(d["stats"]["ready"])
            code, body, headers = self.get("/replay")
            self.assertEqual(code, 200)
            self.assertIn(b"Rug Replay", body)
            self.assertIn("text/html", headers["content-type"])
            self.assertTrue(json.loads(self.get("/api/config")[1])["replay"])

    def test_enabled_starts_checker(self):
        with mock.patch.dict(os.environ, {"REPLAY_ENABLED": "1", "OPMEM_DB_PATH": self.path}), \
                mock.patch.object(replay.Checker, "start", lambda self: self), redirect_stdout(io.StringIO()) as out:
            c = server.start_replay_checker()
        self.assertIsInstance(c, replay.Checker)
        self.assertIn("replay: checks every 60 min", out.getvalue())

    def test_page_has_no_banned_words(self):
        with open(os.path.join(server.ROOT, "replay.html"), encoding="utf-8") as f:
            page = f.read().lower()
        for w in ("scam", "scammer"):
            self.assertNotIn(w, page)
        self.assertIn("tracking started today. confirmed cases appear here as they happen.", page)
        self.assertIn("facts only: market cap at the time of the scan vs now. not financial advice.", page)


if __name__ == "__main__":
    unittest.main()
