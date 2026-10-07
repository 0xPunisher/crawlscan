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
    return {"token": token, "chain": chain, "band": band, "score": score, "rug": rug,
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
        self.assertEqual(s, {"token": T, "chain": "robinhood", "ts": 5, "band": "DANGER", "score": 12,
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


if __name__ == "__main__":
    unittest.main()
