"""Тесты памяти операторов (opmem): разбор результата, выключатель, скан не ждёт записи, дедупликация,
переполнение очереди, сбой базы не ломает скан. Без сети."""
import io, os, sqlite3, tempfile, threading, time, unittest
from contextlib import redirect_stdout
from unittest import mock

import fakes
import opmem
import server

T1, T2 = "0x" + "11" * 20, "0x" + "22" * 20
A, B, C, D, DEV, SRC = ("0x" + c * 40 for c in "abcdef")


def sig(**kw):
    return {"kind": "buy", "virgin": False, "sniper": False, "launch_bundle": False, "is_deployer": False} | kw


def result(token=T1, band="RISKY", score=50, **kw):
    return {"token": token, "chain": "robinhood", "band": band, "score": score, "holders_total": 120,
            "launch": {"deployer": DEV, "block": 1},
            "holders": [
                {"wallet": A, "share": 0.2, "share_supply": 0.05, "signals": sig(virgin=True, launch_bundle=True)},
                {"wallet": B, "share": 0.1, "share_supply": 0.025, "signals": sig(kind="transfer")},
                {"wallet": C, "share": 0.05, "share_supply": 0.0125, "signals": sig(sniper=True)},
                {"wallet": D, "share": 0.01, "share_supply": 0.0025, "signals": sig()},
            ],
            "links": [{"a": A, "b": B, "kind": "distributor", "level": "proven", "via": SRC},
                      {"a": A, "b": C, "kind": "same_tx", "level": "proven", "via": "0xtx"}],
            "operators": [{"wallets": [A, B, C], "share": 0.35, "share_supply": 0.0875, "level": "proven"},
                          {"wallets": [D], "share": 0.01, "share_supply": 0.0025, "level": None}],
            "unread": [D]} | kw


def wait(w, timeout=5):
    t0 = time.time()
    while w.q.unfinished_tasks and time.time() - t0 < timeout:
        time.sleep(0.01)


class Base(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "memory.db")
        opmem._writer["obj"] = None

    def tearDown(self):
        opmem._writer["obj"] = None
        self.tmp.cleanup()

    def table(self, name):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(f"SELECT * FROM {name}")]
        finally:
            con.close()


class TestRows(unittest.TestCase):

    def test_rows(self):
        scan, ops, wal = opmem.rows(result(limited=True), 100)
        self.assertEqual(scan | {}, {"token": T1, "chain": "robinhood", "launchpad": None, "ts": 100, "band": "RISKY",
                                     "score": 50, "limited": 1, "partial": 0, "holders_total": 120, "unread": 1,
                                     "deployer": DEV})
        self.assertEqual(len(ops), 1)                      # одиночный кошелёк — не оператор
        self.assertEqual(ops[0]["operator_id"], opmem.operator_id([C, A, B]))
        self.assertEqual(ops[0]["kinds"], "distributor,same_tx")
        self.assertEqual((ops[0]["n_wallets"], ops[0]["share"], ops[0]["share_supply"]), (3, 0.35, 0.0875))
        by = {w["wallet"]: w for w in wal}
        self.assertEqual(by[A]["roles"], "top_holder,early_buyer,virgin,linked")
        self.assertEqual(by[B]["roles"], "top_holder,transfer_received,linked")
        self.assertEqual(by[C]["roles"], "top_holder,sniper,linked")
        self.assertEqual(by[D]["roles"], "top_holder")
        self.assertIsNone(by[D]["operator_id"])
        self.assertEqual((by[DEV]["roles"], by[DEV]["share"]), ("dev", None))
        self.assertEqual((by[SRC]["roles"], by[SRC]["operator_id"]), ("linked", ops[0]["operator_id"]))
        self.assertEqual(by[A]["share"], 0.2)

    def test_partial_and_worth(self):
        self.assertEqual(opmem.rows(result(partial_scan={"state": "building"}), 1)[0]["partial"], 1)
        self.assertFalse(opmem.worth(result(band="TOO_ESTABLISHED", score=None)))
        self.assertFalse(opmem.worth({"token": T1}))
        self.assertTrue(opmem.worth(result(band="TOO_EARLY_OR_LATE", score=None)))

    def test_default_path_next_to_draw_db(self):
        with mock.patch.dict(os.environ, {"DRAW_DB_PATH": "/data/draw.db", "OPMEM_DB_PATH": ""}):
            self.assertEqual(opmem.db_path(), "/data/memory.db")
        with mock.patch.dict(os.environ, {"OPMEM_DB_PATH": "/x/m.db"}):
            self.assertEqual(opmem.db_path(), "/x/m.db")


class TestWriter(Base):

    def test_write_and_dedup(self):
        clock = [1000.0]
        w = opmem.Writer(self.path, interval=6 * 3600, clock=lambda: clock[0]).start()
        self.assertTrue(w.push(result()))
        self.assertFalse(w.push(result()))                 # тот же токен в окне — не в очередь
        self.assertTrue(w.push(result(T2)))
        self.assertFalse(w.push(result(band="TOO_ESTABLISHED", score=None, token="0x" + "33" * 20)))
        clock[0] += 6 * 3600
        self.assertTrue(w.push(result(score=40)))          # окно прошло
        wait(w)
        scans = self.table("scans")
        self.assertEqual([(s["token"], s["score"]) for s in scans], [(T1, 50), (T2, 50), (T1, 40)])
        self.assertEqual(len(self.table("operators")), 3)
        self.assertEqual(len(self.table("wallets")), 3 * 6)

    def test_dedup_survives_restart(self):
        first = opmem.Writer(self.path, interval=3600, clock=lambda: 1000.0)
        first.write(result(), 1000)
        first.store.close()
        w = opmem.Writer(self.path, interval=3600, clock=lambda: 1500.0).start()   # новый процесс, память пустая
        self.assertTrue(w.push(result()))
        wait(w)
        self.assertEqual(len(self.table("scans")), 1)      # база помнит запись 500 с назад

    def test_push_does_not_wait_for_blocked_write(self):
        gate = threading.Event()
        w = opmem.Writer(self.path, interval=3600)
        with mock.patch.object(opmem.Writer, "write", lambda self, r, ts: gate.wait()), redirect_stdout(io.StringIO()):
            w.start()
            t0 = time.perf_counter()
            for i in range(2000):
                w.push(result("0x" + f"{i:040x}"))
            took = time.perf_counter() - t0
            gate.set()
            wait(w)
        self.assertLess(took, 0.5)

    def test_queue_overflow(self):
        gate = threading.Event()
        clock = [1000.0]
        w = opmem.Writer(self.path, interval=3600, clock=lambda: clock[0], qmax=5)
        out = io.StringIO()
        with mock.patch.object(opmem.Writer, "write", lambda self, r, ts: gate.wait()), redirect_stdout(out):
            pushed = [w.push(result("0x" + f"{i:040x}")) for i in range(20)]   # поток не запущен: никто не берёт
            clock[0] += 30
            w.push(result("0x" + "f" * 40))
            clock[0] += 31
            w.push(result("0x" + "e" * 40))
            gate.set()
        self.assertEqual(pushed.count(True), 5)
        self.assertEqual(w.dropped, 17)
        self.assertEqual(out.getvalue().count("opmem: queue full"), 2)   # не чаще раза в минуту

    def test_write_error_only_logs(self):
        w = opmem.Writer(self.path, interval=0).start()
        out = io.StringIO()
        with redirect_stdout(out):
            with mock.patch.object(opmem.Store, "write", side_effect=sqlite3.OperationalError("disk I/O error")):
                w.push(result())
                wait(w)
            w.push(result(T2))                             # поток жив, следующая запись проходит
            wait(w)
        self.assertIn("opmem: not saved", out.getvalue())
        self.assertEqual([s["token"] for s in self.table("scans")], [T2])


class TestBankr(Base):
    """Bankr: launchpad, вестинг дева как роль dev, частичный скан не закрывает интервал для полного."""
    VEST = "0x" + "77" * 20

    def bankr(self, **kw):
        return result(launchpad="bankr", bankr={
            "dev": DEV, "vesting": {"total": 150, "beneficiaries": {DEV: 100, self.VEST: 50}},
            "dev_holding": {"wallet": DEV, "share_supply": 0.293}}, **kw)

    def test_rows(self):
        scan, _, wal = opmem.rows(self.bankr(partial_scan={"state": "building"}), 1)
        self.assertEqual((scan["launchpad"], scan["partial"], scan["deployer"]), ("bankr", 1, DEV))
        by = {w["wallet"]: w for w in wal}
        self.assertEqual((by[DEV]["roles"], by[DEV]["share_supply"]), ("dev", 0.293))
        self.assertEqual((by[self.VEST]["roles"], by[self.VEST]["share_supply"]), ("dev", None))

    def test_dev_in_top_keeps_holder_share(self):
        r = self.bankr()
        r["holders"][0]["signals"]["is_deployer"] = True
        r["bankr"]["dev_holding"]["wallet"] = A
        by = {w["wallet"]: w for w in opmem.rows(r, 1)[2]}
        self.assertEqual((by[A]["roles"], by[A]["share_supply"]), ("top_holder,dev,early_buyer,virgin,linked", 0.05))

    def test_vesting_entry_is_not_a_buy(self):
        r = self.bankr()
        r["holders"][0]["signals"] = sig(kind="vesting", is_deployer=True, sniper=True, launch_bundle=True, virgin=True)
        by = {w["wallet"]: w for w in opmem.rows(r, 1)[2]}
        self.assertEqual(by[A]["roles"], "top_holder,dev,virgin,linked")

    def test_partial_then_full(self):
        clock = [1000.0]
        w = opmem.Writer(self.path, interval=6 * 3600, clock=lambda: clock[0]).start()
        self.assertTrue(w.push(self.bankr(partial_scan={"state": "building"}, score=59)))
        self.assertFalse(w.push(self.bankr(partial_scan={"state": "building"})))   # частичный после частичного
        clock[0] += 60
        self.assertTrue(w.push(self.bankr(score=44)))                               # полный после частичного
        self.assertFalse(w.push(self.bankr(score=40)))                              # после полного — окно
        self.assertFalse(w.push(self.bankr(partial_scan={"state": "building"})))
        wait(w)
        self.assertEqual([(s["score"], s["partial"]) for s in self.table("scans")], [(59, 1), (44, 0)])

    def test_partial_then_full_after_restart(self):
        first = opmem.Writer(self.path, interval=3600, clock=lambda: 1000.0)
        first.write(self.bankr(partial_scan={"state": "building"}), 1000)
        first.store.close()
        w = opmem.Writer(self.path, interval=3600, clock=lambda: 1100.0).start()
        self.assertTrue(w.push(self.bankr(partial_scan={"state": "building"})))     # память пуста, база отсечёт
        wait(w)
        w._seen.clear()
        self.assertTrue(w.push(self.bankr(score=44)))
        wait(w)
        self.assertEqual([s["partial"] for s in self.table("scans")], [1, 0])


class TestServer(Base):
    """Через server._run_job на подставном адаптере, как живой скан."""

    def setUp(self):
        super().setUp()
        self.env = mock.patch.dict(os.environ, {"DRAW_DB_PATH": os.path.join(self.tmp.name, "draw.db"),
                                                "OPMEM_DB_PATH": self.path, "ALERTS_ENABLED": "false"})
        self.env.__enter__()
        self.fakes = fakes.patched()
        self.fakes.__enter__()

    def tearDown(self):
        self.fakes.__exit__(None, None, None)
        with server._stores_lock:
            for st in server._recent_stores.values():
                st.close()
            server._recent_stores.clear()
        self.env.__exit__(None, None, None)
        super().tearDown()

    def run_scan(self, token=fakes.TOKEN):
        jid = os.urandom(6).hex()
        server.JOBS[jid] = {"token": token, "chain": "robinhood", "events": [], "done": False, "result": None,
                            "error": None, "ts": time.time()}
        t0 = time.perf_counter()
        server._run_job(jid, token)
        return server.JOBS.pop(jid), time.perf_counter() - t0

    def test_disabled_writes_nothing(self):
        with mock.patch.dict(os.environ, {"OPMEM_ENABLED": "false"}):
            job, _ = self.run_scan()
        self.assertIsNotNone(job["result"])
        self.assertIsNone(opmem._writer["obj"])
        self.assertFalse(os.path.exists(self.path))

    def test_enabled_writes_scan(self):
        with mock.patch.dict(os.environ, {"OPMEM_ENABLED": "true"}), redirect_stdout(io.StringIO()):
            job, _ = self.run_scan()
            self.run_scan()                                # повтор в окне — не пишется
            wait(opmem._writer["obj"])
        scans = self.table("scans")
        self.assertEqual(len(scans), 1)
        self.assertEqual((scans[0]["token"], scans[0]["band"]), (fakes.TOKEN, job["result"]["band"]))
        self.assertEqual(len(self.table("wallets")) >= len(job["result"]["holders"]), True)

    def test_recheck_goes_through_same_dedup(self):
        with mock.patch.dict(os.environ, {"OPMEM_ENABLED": "true"}), redirect_stdout(io.StringIO()):
            job, _ = self.run_scan()
            server.recheck_done(job["result"])
            wait(opmem._writer["obj"])
        self.assertEqual(len(self.table("scans")), 1)

    def test_scan_does_not_wait_for_blocked_write(self):
        gate = threading.Event()
        with mock.patch.dict(os.environ, {"OPMEM_ENABLED": "true"}), redirect_stdout(io.StringIO()), \
                mock.patch.object(opmem.Writer, "write", lambda self, r, ts: gate.wait(30)):
            _, base = self.run_scan(fakes.TOKEN)           # прогрев кэшей движка
            opmem._writer["obj"].interval = 0
            t0 = time.perf_counter()
            job, took = self.run_scan(fakes.TOKEN)
            gate.set()
            wait(opmem._writer["obj"])
        self.assertIsNotNone(job["result"])
        self.assertLess(took, 5)                           # запись висит, скан закончился
        self.assertTrue(job["done"])

    def test_db_failure_does_not_break_scan(self):
        bad = os.path.join(self.tmp.name, "file")
        open(bad, "w").close()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"OPMEM_ENABLED": "true", "OPMEM_DB_PATH": os.path.join(bad, "m.db")}), \
                redirect_stdout(out):
            job, _ = self.run_scan()
            wait(opmem._writer["obj"])
        self.assertIsNotNone(job["result"])
        self.assertIsNone(job["error"])
        self.assertIn("opmem: not saved", out.getvalue())

    def test_record_never_raises(self):
        with mock.patch.dict(os.environ, {"OPMEM_ENABLED": "true"}), redirect_stdout(io.StringIO()), \
                mock.patch.object(opmem, "writer", side_effect=RuntimeError("boom")):
            self.assertFalse(opmem.record(result()))


if __name__ == "__main__":
    unittest.main()
