"""Тесты alerts A1: снимки результата скана, diff, хранение и запись после скана. Без сети."""
import json, os, tempfile, threading, time, unittest, urllib.error, urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer

import fakes
import alerts
import early
import server
from alerts_store import AlertsStore

T = "0x" + "11" * 20
A, B, C, D = ("0x" + c * 40 for c in "abcd")


def res(band="OK", score=70, rug=None, ops=None, holders=None, token=T, chain="robinhood"):
    return {"token": token, "chain": chain, "band": band, "score": score, "rug": rug, "header": {"ticker": "AAA"},
            "operators": ops if ops is not None else [{"wallets": [A, B], "share_supply": 0.10}],
            "holders": holders if holders is not None else [{"wallet": A, "share_supply": 0.06},
                                                            {"wallet": B, "share_supply": 0.04},
                                                            {"wallet": C, "share_supply": 0.02}]}


def snap(early_share=None, **kw):
    return alerts.snapshot(res(**kw), early_share, ts=100)


def kinds(changes):
    return [c["kind"] for c in changes]


class TestSnapshot(unittest.TestCase):

    def test_fields(self):
        s = alerts.snapshot(res(band="DANGER", score=12, rug={"drop": 0.7234567891}), 0.123456789, ts=5)
        self.assertEqual(s, {"token": T, "chain": "robinhood", "ticker": "AAA", "ts": 5, "band": "DANGER", "score": 12,
                             "rug": True, "rug_drop": 0.723457,
                             "operator": {"wallets": [A, B], "share_supply": 0.1},
                             "top": {A: 0.06, B: 0.04, C: 0.02}, "early_share": 0.123457})

    def test_no_operators_and_too_established(self):
        s = alerts.snapshot({"token": T, "chain": "solana", "band": "TOO_ESTABLISHED", "score": None, "rug": None,
                             "operators": [], "holders": []})
        self.assertEqual((s["operator"], s["top"], s["score"], s["rug"], s["early_share"]), (None, {}, None, False, None))

    def test_no_verdict(self):
        for bad in (None, {}, {"token": T}, {"band": "OK"}, "error"):
            self.assertIsNone(alerts.snapshot(bad))


class TestDiff(unittest.TestCase):

    def test_same_or_missing(self):
        self.assertEqual(alerts.diff(snap(), snap()), [])
        self.assertEqual(alerts.diff(None, snap()), [])
        self.assertEqual(alerts.diff(snap(), None), [])

    def test_verdict(self):
        ch = alerts.diff(snap(band="OK", score=70), snap(band="DANGER", score=20))
        self.assertEqual(kinds(ch), ["verdict_worse"])
        self.assertEqual(ch[0]["text"], "Verdict worsened: OK → DANGER (score 70 → 20)")
        ch = alerts.diff(snap(band="RISKY", score=45), snap(band="CLEAN", score=85))
        self.assertEqual(ch[0]["text"], "Verdict improved: RISKY → CLEAN (score 45 → 85)")
        self.assertEqual(alerts.diff(snap(band="OK", score=61), snap(band="OK", score=79)), [])   # скор без смены полосы

    def test_border_noise_is_silent(self):
        self.assertEqual(alerts.diff(snap(band="CLEAN", score=80), snap(band="OK", score=78)), [])
        self.assertEqual(alerts.diff(snap(band="OK", score=78), snap(band="CLEAN", score=81)), [])
        self.assertEqual(alerts.diff(snap(band="OK", score=62), snap(band="RISKY", score=55)), [])      # 7 баллов
        self.assertEqual(kinds(alerts.diff(snap(band="OK", score=62), snap(band="RISKY", score=54))),
                         ["verdict_worse"])                                                           # 8 баллов
        self.assertEqual(kinds(alerts.diff(snap(band="CLEAN", score=85), snap(band="OK", score=77))),
                         ["verdict_worse"])

    def test_danger_always(self):
        ch = alerts.diff(snap(band="OK", score=61), snap(band="DANGER", score=59))   # жёсткое правило, скор почти тот же
        self.assertEqual(ch[0]["text"], "Verdict worsened: OK → DANGER (score 61 → 59)")
        ch = alerts.diff(snap(band="DANGER", score=38), snap(band="RISKY", score=41))
        self.assertEqual(ch[0]["text"], "Verdict improved: DANGER → RISKY (score 38 → 41)")
        ch = alerts.diff(snap(band="OK", score=None), snap(band="RISKY", score=None))   # скора нет — сообщаем
        self.assertEqual(kinds(ch), ["verdict_worse"])

    def test_unranked_bands_not_compared(self):
        for a, b in (("TOO_EARLY_OR_LATE", "DANGER"), ("OK", "TOO_ESTABLISHED"), ("TOO_ESTABLISHED", "CLEAN")):
            self.assertNotIn("verdict_worse", kinds(alerts.diff(snap(band=a, score=None), snap(band=b, score=None))))
            self.assertNotIn("verdict_better", kinds(alerts.diff(snap(band=a, score=None), snap(band=b, score=None))))

    def test_rug(self):
        ch = alerts.diff(snap(band="DANGER", score=20), snap(band="DANGER", score=20, rug={"drop": 0.72}))
        self.assertEqual(ch, [{"kind": "rug_appeared", "drop": 0.72,
                               "text": "Probably rug: −72% if suspicious holders sell"}])
        ch = alerts.diff(snap(band="DANGER", score=20, rug={"drop": 0.72}), snap(band="DANGER", score=20))
        self.assertEqual(ch, [{"kind": "rug_gone", "text": "Probably rug is gone (was −72%)"}])
        self.assertEqual(alerts.diff(snap(rug={"drop": 0.5}), snap(rug={"drop": 0.9})), [])   # rug остался

    def test_operator_sold(self):
        # прежний оператор A+B (10%): теперь A 3%, B выпал из топа → 3% = продал 70%
        new = snap(ops=[{"wallets": [C], "share_supply": 0.05}],
                   holders=[{"wallet": C, "share_supply": 0.05}, {"wallet": A, "share_supply": 0.03}])
        ch = alerts.diff(snap(), new)
        self.assertEqual(kinds(ch), ["operator_sold"])
        self.assertEqual(ch[0]["text"], "Biggest operator sold 70% of their holdings (10.0% → 3.0% of supply)")

    def test_operator_threshold(self):
        just = snap(holders=[{"wallet": A, "share_supply": 0.04}, {"wallet": B, "share_supply": 0.03}])   # −30%
        self.assertEqual(kinds(alerts.diff(snap(), just)), ["operator_sold"])
        less = snap(holders=[{"wallet": A, "share_supply": 0.05}, {"wallet": B, "share_supply": 0.03}])   # −20%
        self.assertEqual(alerts.diff(snap(), less), [])
        more = snap(holders=[{"wallet": A, "share_supply": 0.08}, {"wallet": B, "share_supply": 0.04}])   # докупил
        self.assertEqual(alerts.diff(snap(), more), [])

    def test_operator_tiny_or_no_top(self):
        tiny = snap(ops=[{"wallets": [A], "share_supply": 0.0005}], holders=[{"wallet": A, "share_supply": 0.0005}])
        self.assertEqual(alerts.diff(tiny, snap(holders=[{"wallet": B, "share_supply": 0.04}])), [])
        # новый скан без холдеров (TOO_ESTABLISHED) — не «продал всё»
        est = snap(band="TOO_ESTABLISHED", score=None, ops=[], holders=[])
        self.assertNotIn("operator_sold", kinds(alerts.diff(snap(), est)))

    def test_early_dropped(self):
        ch = alerts.diff(snap(early_share=0.20), snap(early_share=0.12))
        self.assertEqual(ch, [{"kind": "early_dropped", "fell": 0.4, "from": 0.2, "to": 0.12,
                               "text": "Early buyers' share fell 40% (20.0% → 12.0% of supply)"}])
        self.assertEqual(alerts.diff(snap(early_share=0.20), snap(early_share=0.15)), [])      # −25%
        self.assertEqual(alerts.diff(snap(early_share=0.20), snap(early_share=None)), [])      # не известна
        self.assertEqual(alerts.diff(snap(early_share=None), snap(early_share=0.01)), [])
        self.assertEqual(alerts.diff(snap(early_share=0.004), snap(early_share=0.0)), [])      # была < 0.5%

    def test_several_at_once(self):
        new = snap(band="DANGER", score=10, rug={"drop": 0.8}, early_share=0.05,
                   holders=[{"wallet": C, "share_supply": 0.02}])
        self.assertEqual(kinds(alerts.diff(snap(early_share=0.2), new)),
                         ["verdict_worse", "rug_appeared", "operator_sold", "early_dropped"])

    def test_json_roundtrip(self):
        old, new = snap(early_share=0.2), snap(band="DANGER", score=10, early_share=0.1)
        self.assertEqual(alerts.diff(json.loads(json.dumps(old)), json.loads(json.dumps(new))), alerts.diff(old, new))


class TestStore(unittest.TestCase):

    NOW = 1_800_000_000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AlertsStore(os.path.join(self.tmp.name, "a.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_keeps_last_and_previous(self):
        self.assertEqual(self.store.get(T), (None, None))
        s1, s2, s3 = (alerts.snapshot(res(score=x), ts=x) for x in (61, 62, 63))
        self.assertEqual(self.store.record(s1), (None, s1))
        self.assertEqual(self.store.record(s2), (s1, s2))
        self.store.record(s3)
        self.assertEqual(self.store.get(T), (s2, s3))
        other = alerts.snapshot(res(token="0x" + "22" * 20), ts=1)
        self.store.record(other)
        self.assertEqual(self.store.get("0x" + "22" * 20), (None, other))
        self.assertEqual(self.store.get(T), (s2, s3))

    def test_set_early(self):
        self.assertFalse(self.store.set_early(T, 0.1))                 # снимка нет — ничего не создаём
        self.assertEqual(self.store.get(T), (None, None))
        s1 = alerts.snapshot(res(), ts=1)
        self.store.record(s1)
        self.assertTrue(self.store.set_early(T, 0.123456789))
        prev, cur = self.store.get(T)
        self.assertEqual((prev, cur), (None, s1 | {"early_share": 0.123457}))

    def test_watch_limit_and_renew(self):
        tok = ["0x" + c * 40 for c in "123456"]
        for i, t in enumerate(tok[:3]):
            st, w, items = self.store.watch(1, t, "robinhood", now=self.NOW + i)
            self.assertEqual((st, w["expires_at"], len(items)), ("ok", self.NOW + i + 7 * 86400, i + 1))
        st, w, items = self.store.watch(1, tok[3], "robinhood", now=self.NOW + 10)
        self.assertEqual((st, w, [x["token"] for x in items]), ("limit", None, tok[:3]))
        st, w, items = self.store.watch(1, tok[0], "robinhood", now=self.NOW + 100)   # уже есть — продление
        self.assertEqual((st, w["created_at"], w["expires_at"], len(items)),
                         ("renewed", self.NOW, self.NOW + 100 + 7 * 86400, 3))
        self.assertEqual(self.store.watch(2, tok[3], "robinhood", now=self.NOW)[0], "ok")   # другой чат — свой лимит
        self.assertTrue(self.store.unwatch(1, tok[1]))
        self.assertFalse(self.store.unwatch(1, tok[1]))
        self.assertEqual(self.store.watch(1, tok[3], "robinhood", now=self.NOW + 200)[0], "ok")

    def test_watches_have_ticker_from_snapshot(self):
        t = "0x" + "77" * 20
        self.store.watch(1, t, "robinhood", now=self.NOW)
        self.store.watch(1, T, "robinhood", now=self.NOW + 1)
        self.store.record(alerts.snapshot(res(token=t), ts=1))             # тикер AAA из шапки скана
        items = self.store.watches(1, now=self.NOW + 2)
        self.assertEqual([(w["token"], w["chain"], w["ticker"]) for w in items],
                         [(t, "robinhood", "AAA"), (T, "robinhood", None)])

    def test_watch_expires_after_7_days(self):
        t = "0x" + "77" * 20
        self.store.watch(1, t, "robinhood", now=self.NOW)
        self.assertEqual(len(self.store.watches(1, now=self.NOW + 7 * 86400 - 1)), 1)
        self.assertEqual(self.store.watches(1, now=self.NOW + 7 * 86400), [])
        for i, c in enumerate("abc"):     # истёкшая не занимает место в лимите
            self.assertEqual(self.store.watch(1, "0x" + c * 40, "robinhood", now=self.NOW + 7 * 86400 + i)[0], "ok")
        self.assertEqual(len(self.store.watches(1, now=self.NOW + 7 * 86400 + 5)), 3)


class TestServerSnapshots(unittest.TestCase):

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
        with server._lock:
            server.JOBS.clear(); server.BY_TOKEN.clear(); server.RECENT.clear()
        early.clear_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "a.db")
        self.env = mock.patch.dict(os.environ, {"DRAW_DB_PATH": self.db, "ALERTS_ENABLED": "true"})
        self.env.__enter__()
        self.fakes = fakes.patched()
        self.fakes.__enter__()

    def tearDown(self):
        self.fakes.__exit__(None, None, None)
        with server._stores_lock:
            for st in list(server._recent_stores.values()) + list(server._alerts_stores.values()):
                st.close()
            server._recent_stores.clear()
            server._alerts_stores.clear()
        self.env.__exit__(None, None, None)
        early.clear_cache()
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read())

    def scan(self, token):
        req = urllib.request.Request(self.base + "/api/scan", data=json.dumps({"token": token}).encode(),
                                     method="POST", headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            job = json.loads(r.read())["job"]
        t0 = time.time()
        while time.time() - t0 < 10:
            d = self.get(f"/api/result?job={job}")
            if d["done"]:
                time.sleep(0.1)        # снимок пишется сразу после done
                return d
            time.sleep(0.05)
        self.fail("скан не завершился")

    def rescan(self, token):
        with server._lock:               # мимо 10-минутного кэша: новый скан
            server.JOBS.clear(); server.BY_TOKEN.clear()
        return self.scan(token)

    def test_snapshot_written(self):
        d = self.scan(fakes.TOKEN)
        prev, cur = server.alerts_store().get(fakes.TOKEN)
        self.assertIsNone(prev)
        r = d["result"]
        self.assertEqual((cur["band"], cur["score"], cur["rug"]), (r["band"], r["score"], bool(r["rug"])))
        self.assertEqual(cur["operator"]["wallets"], r["operators"][0]["wallets"])
        self.assertEqual(len(cur["top"]), len(r["holders"]))
        self.assertIsNone(cur["early_share"])
        self.rescan(fakes.TOKEN)
        prev2, cur2 = server.alerts_store().get(fakes.TOKEN)
        self.assertEqual(prev2, cur)
        self.assertEqual(alerts.diff(prev2, cur2), [])           # те же данные — изменений нет

    def test_cached_repeat_not_a_new_snapshot(self):
        self.scan(fakes.TOKEN)
        self.scan(fakes.TOKEN)                                    # из 10-минутного кэша
        self.assertEqual(server.alerts_store().get(fakes.TOKEN)[0], None)

    def put_early(self, ts, now=150):
        data = {"launch": {"block": 1, "ts": 0, "deployer": None},
                "buyers": [{"wallet": A, "block": 2, "ts": 1, "tx": "0x1", "bought": 200}]}
        status = {"supply": 1000, "wallets": {A: {"now": now, "sold": 50, "moved": {}, "burned": 0, "partial": False}}}
        early._put(early._STATUS, ("robinhood", fakes.TOKEN), (ts, data, status))

    def test_early_share_if_known(self):
        self.assertIsNone(early.known_share("robinhood", fakes.TOKEN))
        self.put_early(time.time() - 3600)                     # расчёт early час назад, без сети
        self.assertAlmostEqual(early.known_share("robinhood", fakes.TOKEN), 0.15)
        self.scan(fakes.TOKEN)
        _, cur = server.alerts_store().get(fakes.TOKEN)
        self.assertEqual(cur["early_share"], 0.15)

    def test_early_share_too_old(self):
        self.put_early(time.time() - early.STALE_TTL - 1)
        self.assertIsNone(early.known_share("robinhood", fakes.TOKEN))

    def early_body(self, **kw):
        return {"token": fakes.TOKEN, "chain": "robinhood", "available": True,
                "summary": {"now_share_supply": 0.0812}} | kw

    def test_early_written_into_last_snapshot(self):
        self.scan(fakes.TOKEN)
        with mock.patch.object(early, "get", return_value=self.early_body()):
            e = self.get(f"/api/early?token={fakes.TOKEN}")
        self.assertEqual(e["summary"]["now_share_supply"], 0.0812)
        time.sleep(0.05)
        prev, cur = server.alerts_store().get(fakes.TOKEN)
        self.assertEqual((prev, cur["early_share"]), (None, 0.0812))

    def test_early_stale_or_error_not_written(self):
        self.scan(fakes.TOKEN)
        for body in (self.early_body(stale_at=1), self.early_body(error="busy", summary=None),
                     {"token": fakes.TOKEN, "chain": "robinhood", "available": False, "reason": "too established"}):
            with mock.patch.object(early, "get", return_value=body):
                self.get(f"/api/early?token={fakes.TOKEN}")
        time.sleep(0.05)
        self.assertIsNone(server.alerts_store().get(fakes.TOKEN)[1]["early_share"])

    def test_early_db_failure_does_not_break_early(self):
        with mock.patch.object(early, "get", return_value=self.early_body()), \
                mock.patch.object(server, "alerts_store", side_effect=OSError("disk full")):
            e = self.get(f"/api/early?token={fakes.TOKEN}")
        self.assertEqual(e["summary"]["now_share_supply"], 0.0812)

    def test_early_disabled_writes_nothing(self):
        with mock.patch.dict(os.environ, {"ALERTS_ENABLED": "false"}), \
                mock.patch.object(early, "get", return_value=self.early_body()), \
                mock.patch.object(server, "AlertsStore", side_effect=AssertionError("store opened")):
            e = self.get(f"/api/early?token={fakes.TOKEN}")
        self.assertTrue(e["available"])
        self.assertEqual(server._alerts_stores, {})

    def test_disabled_writes_nothing(self):
        with mock.patch.dict(os.environ, {"ALERTS_ENABLED": "false"}), \
                mock.patch.object(server, "AlertsStore", side_effect=AssertionError("store opened")) as st, \
                mock.patch.object(early, "known_share", side_effect=AssertionError("early read")):
            d = self.scan(fakes.TOKEN)
        self.assertIn("result", d)
        st.assert_not_called()
        self.assertEqual(server._alerts_stores, {})
        import sqlite3
        with sqlite3.connect(self.db) as c:   # файл есть (лента), таблицы снимков нет
            self.assertEqual(c.execute("SELECT name FROM sqlite_master WHERE name = 'alert_snapshots'").fetchall(), [])

    def test_db_failure_does_not_break_scan(self):
        with mock.patch.object(server, "alerts_store", side_effect=OSError("disk full")):
            d = self.scan(fakes.TOKEN)
        self.assertIn("result", d)
        self.assertNotIn("error", d)
        self.assertEqual(self.get("/api/recent")["items"][0]["token"], fakes.TOKEN)   # лента записалась

    def test_store_record_failure(self):
        with mock.patch.object(AlertsStore, "record", side_effect=RuntimeError("locked")):
            d = self.scan(fakes.TOKEN)
        self.assertIn("result", d)



SECRET = "s3cret-for-tests"
RH2, RH3, RH4 = ("0x" + c * 40 for c in "234")


class TestWatchAPI(unittest.TestCase):

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
        self.env = mock.patch.dict(os.environ, {"DRAW_DB_PATH": os.path.join(self.tmp.name, "w.db"),
                                                "ALERTS_ENABLED": "true", "ALERTS_API_SECRET": SECRET})
        self.env.__enter__()
        self.fakes = fakes.patched()     # market.fetch_market → {} (рынок не ответил), сети нет
        self.fakes.__enter__()

    def tearDown(self):
        self.fakes.__exit__(None, None, None)
        with server._stores_lock:
            for st in server._alerts_stores.values():
                st.close()
            server._alerts_stores.clear()
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def req(self, path, body=None, secret=SECRET):
        headers = {"content-type": "application/json"}
        if secret is not None:
            headers["X-Alerts-Secret"] = secret
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   method="POST" if body is not None else "GET", headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def watch(self, token, chat=42, **kw):
        return self.req("/api/alerts/watch", {"chat_id": chat, "token": token}, **kw)

    def test_secret_required(self):
        self.assertFalse(server.alerts_secret_ok("ключ"))          # не-ASCII — просто «нет», без исключения
        self.assertFalse(server.alerts_secret_ok(None))
        self.assertTrue(server.alerts_secret_ok(SECRET))
        for secret in (None, "", "wrong", SECRET + "x"):
            self.assertEqual(self.watch(RH2, secret=secret)[0], 403, secret)
            self.assertEqual(self.req("/api/alerts/list?chat_id=42", secret=secret)[0], 403, secret)
            self.assertEqual(self.req("/api/alerts/unwatch", {"chat_id": 42, "token": RH2}, secret=secret)[0], 403)
        self.assertEqual(self.req("/api/alerts/list?chat_id=42")[1]["items"], [])     # ничего не записалось
        with mock.patch.dict(os.environ, {"ALERTS_API_SECRET": ""}):                 # секрет не задан — всегда 403
            self.assertEqual(self.watch(RH2, secret="")[0], 403)

    def test_secret_not_logged(self):
        import io, contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            self.watch(RH2); self.watch(RH2, secret="wrong")
            with mock.patch.object(server, "alerts_store", side_effect=OSError("disk full")):
                self.watch(RH2)
        self.assertNotIn(SECRET, out.getvalue())

    def test_disabled_404(self):
        with mock.patch.dict(os.environ, {"ALERTS_ENABLED": "false"}):
            self.assertEqual(self.watch(RH2)[0], 404)
            self.assertEqual(self.req("/api/alerts/list?chat_id=42")[0], 404)
            self.assertEqual(self.req("/api/alerts/unwatch", {"chat_id": 42, "token": RH2})[0], 404)
            self.assertEqual(self.req("/api/config", secret=None)[1]["alerts"], False)
        self.assertEqual(self.req("/api/config", secret=None)[1]["alerts"], True)

    def test_watch_list_unwatch(self):
        code, d = self.watch(RH2.upper().replace("0X", "0x"))
        self.assertEqual((code, d["ok"], d["renewed"], d["token"], d["chain"], d["days"], d["limit"]),
                         (200, True, False, RH2, "robinhood", 7, 3))
        self.assertAlmostEqual(d["expires_at"] - d["created_at"], 7 * 86400)
        self.assertAlmostEqual(d["created_at"], time.time(), delta=5)
        code, d = self.req("/api/alerts/list?chat_id=42")
        self.assertEqual((code, [w["token"] for w in d["items"]]), (200, [RH2]))
        self.assertEqual(self.req("/api/alerts/list?chat_id=43")[1]["items"], [])
        code, d = self.req("/api/alerts/unwatch", {"chat_id": 42, "token": RH2})
        self.assertEqual((code, d["removed"], d["items"]), (200, True, []))
        self.assertFalse(self.req("/api/alerts/unwatch", {"chat_id": 42, "token": RH2})[1]["removed"])

    def test_limit_3(self):
        for t in (RH2, RH3, T):
            self.assertEqual(self.watch(t)[0], 200)
        code, d = self.watch(RH4)
        self.assertEqual((code, d["error"], d["limit"]), (409, "watch limit", 3))
        self.assertEqual(d["message"], "You can watch up to 3 tokens. Unwatch one first.")
        self.assertEqual([w["token"] for w in d["items"]], [RH2, RH3, T])
        code, d = self.watch(RH2)                      # уже подписан — продление, не превышение
        self.assertEqual((code, d["renewed"]), (200, True))
        self.assertEqual(self.watch(RH4, chat=99)[0], 200)

    def test_expires_after_7_days(self):
        for t in (RH2, RH3, T):
            self.watch(t)
        later = time.time() + 7 * 86400 + 10
        with mock.patch.object(server.time, "time", return_value=later):
            self.assertEqual(self.req("/api/alerts/list?chat_id=42")[1]["items"], [])
            self.assertEqual(self.watch(RH4)[0], 200)

    def test_too_established(self):
        st = server.alerts_store()                     # последний скан токена — TOO_ESTABLISHED
        st.record(alerts.snapshot(res(token=RH3, band="TOO_ESTABLISHED", score=None, ops=[], holders=[])))
        code, d = self.watch(RH3)
        self.assertEqual((code, d["error"]), (422, "too established"))
        self.assertEqual(d["message"], "This token is too established for CrawlScan, so it can't be watched.")
        big = {"age_days": 700, "liquidity_usd": 8e6, "mcap_usd": 1.6e8, "source": "gt"}   # не сканировали: рынок
        with mock.patch.object(server.market, "fetch_market", return_value=big) as fm:
            self.assertEqual(self.watch(RH4)[0], 422)
            self.assertEqual(self.watch(RH4)[0], 422)  # второй раз — из кэша вердикта, без запроса
        self.assertEqual(fm.call_count, 1)
        self.assertEqual(self.req("/api/alerts/list?chat_id=42")[1]["items"], [])
        self.assertEqual(self.watch(RH2)[0], 200)      # рынок не ответил ({}) — подписываем

    def test_too_active(self):
        from chains import bankr
        with mock.patch.dict(bankr._ACTIVE, clear=True):
            bankr.mark_active(RH3)                    # скан дал TOO ACTIVE (Bankr, индекс выключен)
            code, d = self.watch(RH3)
            self.assertEqual((code, d["error"]), (422, "too active"))
            self.assertEqual(d["message"], "This token has too many trades for a full scan right now, so it can't be watched.")
            self.assertEqual(self.watch(RH2)[0], 200)
        st = server.alerts_store()
        server.record_snapshot(res(token=RH4, band="TOO_ACTIVE", score=None, ops=[], holders=[]))
        self.assertEqual(st.get(RH4), (None, None))   # снимка нет: вердикта нет

    def test_bad_input(self):
        self.assertEqual(self.watch("not an address"), (400, {"error": "not a token address"}))
        for chat in (None, "abc", True, 1.5, [1]):
            self.assertEqual(self.watch(RH2, chat=chat)[0], 400, chat)
        self.assertEqual(self.req("/api/alerts/list")[0], 400)
        self.assertEqual(self.req("/api/alerts/nope?chat_id=1")[0], 404)
        self.assertEqual(self.req("/api/alerts/watch?chat_id=1")[0], 404)     # GET на POST-путь
        r = urllib.request.Request(self.base + "/api/alerts/watch", data=b"[1,2", method="POST",
                                   headers={"X-Alerts-Secret": SECRET})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(r, timeout=10)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()

    def test_db_failure_500(self):
        with mock.patch.object(server, "alerts_store", side_effect=OSError("disk full")):
            self.assertEqual(self.watch(RH2)[0], 500)

    def test_bot_client(self):
        """Клиент бота против настоящего сервера: секрет в заголовке, 409/422 — ответом, 404 — AlertsOff."""
        from bot.api import AlertsOff, CrawlScan, Rejected
        api = CrawlScan(self.base, alerts_secret=SECRET)
        self.assertTrue(api.config()["alerts"])
        self.assertEqual(api.watch(42, RH2)["token"], RH2)
        api.watch(42, RH3); api.watch(42, T)
        r = api.watch(42, RH4)
        self.assertEqual((r["status"], len(r["items"])), (409, 3))
        self.assertEqual(len(api.watch_list(42)["items"]), 3)
        self.assertTrue(api.unwatch(42, RH2)["removed"])
        with self.assertRaises(Rejected):
            api.watch(42, "0x1234")
        with mock.patch.dict(os.environ, {"ALERTS_ENABLED": "false"}), self.assertRaises(AlertsOff):
            api.watch_list(42)


if __name__ == "__main__":
    unittest.main()
