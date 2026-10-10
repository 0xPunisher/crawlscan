"""Тесты Premium: брони и привязки (premium_store), поиск покупки и балансы (premium_service) на подставной сети,
API сайта (/api/premium/*, лимиты Watchlist) и команды бота. Без сети."""
import json, os, tempfile, threading, time, unittest, urllib.error, urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer

import fakes
import alerts
import premium
import premium_service
import server
from alerts_store import AlertsStore
from premium_store import PremiumStore
from bot import main as bm, text as T
from bot.api import CrawlScan

SECRET = "s3cret"
CURVE, PM, ROUTER = "0x" + "c0" * 20, "0x" + "e1" * 20, "0x" + "e2" * 20
W1, W2, W3 = "0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "a3" * 20
FRIEND = "0x" + "f0" * 20                    # обычный кошелёк: его перевод — не покупка
AGG = "0x" + "a9" * 20                       # приложение / агрегатор, которого нет в списках роутеров
SWAP, CURVE_BUY = fakes.ch.V4_SWAP_TOPIC, fakes.ch.CURVE_BUY
MODIFY_LIQUIDITY = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"   # V4 ModifyLiquidity
U1, U2 = 111111, 222222
E18 = 10 ** 18
NOW = 1_800_000_000
TOKENS = ["0x" + f"{i:02x}" * 20 for i in range(20, 32)]


class FakeChain:
    """Адаптер Robinhood для сервиса: блоки, переводы токена, балансы. Пишет, что у него спрашивали."""
    ROUTERS = {ROUTER}
    V4_POOL_MGR = PM
    V4_SWAP_TOPIC, CURVE_BUY = SWAP, CURVE_BUY

    def __init__(self):
        self.head, self.transfers, self.balance, self.calls, self.missing = 1000, [], {}, [], set()
        self.events = {}      # tx -> [(адрес, topic0)]: Swap, CurveBuy, ModifyLiquidity в чеке

    def get_launch(self, token):
        return {"curve": CURVE}

    def market_addresses(self, curve):
        return {curve, ROUTER, PM}

    def token_decimals(self, token):
        return 18

    def block_number(self):
        self.calls.append("eth_blockNumber")
        return self.head

    def get_token_transfers(self, token, lo, hi, to=None):
        self.calls.append(("eth_getLogs", lo, hi, tuple(to)))
        return [dict(t) for t in self.transfers if lo <= t["block"] <= hi and t["to"] in to]

    def block_timestamps(self, blocks):
        return {}

    def receipt_logs(self, txs):
        """Чек: Transfer-логи токена всех переводов этой транзакции и события из events (missing — чека ещё нет)."""
        self.calls.append(("receipts", tuple(txs)))
        topic = lambda a: "0x" + "0" * 24 + a[2:]
        return {h: [] if h in self.missing else
                [{"address": fakes.TOKEN, "topics": [premium.TRANSFER_TOPIC, topic(t["frm"]), topic(t["to"])],
                  "data": hex(t["amount"])} for t in self.transfers if t["tx"] == h]
                + [{"address": a, "topics": [t0, "0x" + "11" * 32], "data": "0x"} for a, t0 in self.events.get(h, [])]
                for h in txs}

    def token_balance(self, token, wallet):
        self.calls.append("balanceOf")
        return self.balance.get(wallet, 0)

    def rpc_batch(self, calls):
        self.calls.append(("batch", len(calls)))
        return [hex(self.balance.get("0x" + c[1][0]["data"][-40:], 0)) for c in calls]

    def buy(self, to, frm=PM, ts=NOW + 60, block=None, tx=None, amount=E18):
        self.head += 1
        self.transfers.append({"frm": frm, "to": to, "amount": amount, "ts": ts, "block": block or self.head,
                               "tx": tx or f"0xtx{len(self.transfers)}", "log_index": 0})


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def service(store, chain, clock, notes, upgrades=None, downgrade=None):
    return premium_service.Service(
        store, lambda u, text: notes.append((u, text)), (upgrades if upgrades is not None else []).append,
        downgrade or (lambda u: []), chain, clock=clock, log=lambda m: None, threshold=500_000, token=fakes.TOKEN)


class TestRules(unittest.TestCase):

    def test_counts_as_buy(self):
        r = {"wallet": W1, "created_at": NOW, "expires_at": NOW + 900}
        t = {"frm": PM, "to": W1, "amount": 1, "ts": NOW + 10}
        senders = {PM, ROUTER, CURVE}
        self.assertTrue(premium.counts_as_buy(t, r, senders))
        self.assertTrue(premium.counts_as_buy(t | {"frm": ROUTER}, r, senders))          # через роутер
        self.assertFalse(premium.counts_as_buy(t | {"frm": FRIEND}, r, senders))         # с обычного кошелька
        self.assertFalse(premium.counts_as_buy(t | {"ts": NOW - 1}, r, senders))         # до брони
        self.assertFalse(premium.counts_as_buy(t | {"ts": NOW + 901}, r, senders))       # после брони
        self.assertFalse(premium.counts_as_buy(t | {"amount": 0}, r, senders))
        self.assertFalse(premium.counts_as_buy(t | {"to": W2}, r, senders))

    def test_pool_out(self):
        topic = lambda a: "0x" + "0" * 24 + a[2:]
        log = lambda frm, to, address=fakes.TOKEN: {"address": address, "data": "0x1",
                                                     "topics": [premium.TRANSFER_TOPIC, topic(frm), topic(to)]}
        ev = lambda address, t0: {"address": address, "data": "0x", "topics": [t0, "0x" + "11" * 32]}
        pools, swaps = {PM, CURVE}, {(PM, SWAP), (CURVE, CURVE_BUY)}
        out = lambda logs: premium.pool_out(logs, fakes.TOKEN, pools, swaps)
        self.assertTrue(out([ev(PM, SWAP), log(PM, AGG), log(AGG, W1)]))                 # своп, вышли из пула
        self.assertTrue(out([ev(CURVE, CURVE_BUY), log(CURVE, W1)]))                     # покупка на кривой
        self.assertFalse(out([ev(PM, MODIFY_LIQUIDITY), log(PM, AGG), log(AGG, W1)]))    # вывод ликвидности
        self.assertFalse(out([log(PM, AGG), log(AGG, W1)]))                              # из пула, но без свопа
        self.assertFalse(out([ev(AGG, SWAP), log(PM, W1)]))                              # «Swap» не от PoolManager
        self.assertFalse(out([ev(PM, SWAP), log(FRIEND, W1)]))                           # кошелёк → кошелёк
        self.assertFalse(out([ev(PM, SWAP), log(W1, PM), log(AGG, W1)]))                 # в пул (продажа), не из
        self.assertFalse(out([ev(PM, SWAP), log(PM, W1, address=W3)]))                   # другой токен из пула
        pools = {PM, CURVE}
        r = {"wallet": W1, "created_at": NOW, "expires_at": NOW + 900}
        t = {"frm": AGG, "to": W1, "amount": 1, "ts": NOW + 10}
        self.assertFalse(premium.counts_as_buy(t, r, pools))
        self.assertTrue(premium.counts_as_buy(t, r, pools, from_pool=True))
        self.assertFalse(premium.counts_as_buy(t | {"ts": NOW - 1}, r, pools, from_pool=True))

    def test_known_routers_of_the_network(self):
        svc = service(None, FakeChain(), Clock(), [])
        senders = svc.senders()
        self.assertEqual((svc.pools(), svc.swaps()), ({PM, CURVE}, {(PM, SWAP), (CURVE, CURVE_BUY)}))
        self.assertTrue({CURVE, ROUTER, PM} <= senders)
        self.assertIn("0x6aa80dbbed9ae5ab45fbf61f9644fada3b29326e", senders)   # проходной роутер (bankr.ROUTERS)
        self.assertNotIn(FRIEND, senders)

    def test_config(self):
        with mock.patch.dict(os.environ, {"PREMIUM_ENABLED": "", "PREMIUM_MIN_TOKENS": ""}):
            self.assertFalse(premium.enabled())
            self.assertEqual(premium.min_tokens(), 500_000)
        with mock.patch.dict(os.environ, {"PREMIUM_ENABLED": "true", "PREMIUM_MIN_TOKENS": "1000000"}):
            self.assertTrue(premium.enabled())
            self.assertEqual(premium.min_tokens(), 1_000_000)
        with mock.patch.dict(os.environ, {"PREMIUM_MIN_TOKENS": "abc", "PREMIUM_DB_PATH": "",
                                          "DRAW_DB_PATH": "/data/draw.db"}):
            self.assertEqual(premium.min_tokens(), 500_000)
            self.assertEqual(premium.db_path(), "/data/premium.db")                      # не draw.db

    def test_attempts(self):
        a = premium.Attempts(n=2, window=3600)
        self.assertTrue(a.take(U1, 0))
        self.assertTrue(a.take(U1, 1))
        self.assertFalse(a.take(U1, 2))
        self.assertTrue(a.take(U2, 2))
        self.assertTrue(a.take(U1, 3601))


class TestStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = PremiumStore(os.path.join(self.tmp.name, "premium.db"))

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def link(self, user, wallet, balance=600_000 * E18, now=NOW):
        self.assertEqual(self.st.reserve(user, wallet, now)[0], "reserved")
        return self.st.link(self.st.reservation_of(user), "0xtx" + wallet[-4:], balance, 500_000 * E18, now)

    def test_reserve(self):
        state, r = self.st.reserve(U1, W1, NOW)
        self.assertEqual((state, r["user_id"], r["expires_at"]), ("reserved", U1, NOW + 900))
        self.assertEqual(self.st.reserve(U1, W1, NOW + 5)[0], "pending")                # своя бронь
        self.assertEqual(self.st.reserve(U2, W1, NOW + 5), ("taken", None))              # кто первый, того и кошелёк
        self.assertEqual(self.st.reserve(U1, W2, NOW + 10)[0], "reserved")               # новая бронь заменяет прежнюю
        self.assertEqual([r["wallet"] for r in self.st.reservations()], [W2])
        self.assertEqual(self.st.reserve(U2, W1, NOW + 10)[0], "reserved")               # W1 освободился

    def test_linked_wallet_is_taken(self):
        link, old = self.link(U1, W1)
        self.assertEqual((link["user_id"], link["premium"], old), (U1, 1, None))
        self.assertEqual(self.st.reserve(U2, W1, NOW)[0], "taken")                       # один кошелёк — один аккаунт
        self.assertEqual(self.st.reserve(U1, W1, NOW)[0], "yours")
        self.assertIsNone(self.st.reservation_of(U1))

    def test_one_wallet_per_account(self):
        self.link(U1, W1)
        link, old = self.link(U1, W2)
        self.assertEqual((link["wallet"], old), (W2, W1))
        self.assertIsNone(self.st.link_by_wallet(W1))
        self.assertEqual(self.st.reserve(U2, W1, NOW)[0], "reserved")

    def test_below_threshold(self):
        link, _ = self.link(U1, W1, balance=100 * E18)
        self.assertEqual((link["premium"], link["below_since"], link["downgraded"]), (0, None, 1))
        self.assertEqual(premium.watch_terms(link, NOW), (3, 7))                         # не было премиума — нет и 7 дней

    def test_unlink(self):
        self.link(U1, W1)
        self.assertEqual(self.st.unlink(U1)["wallet"], W1)
        self.assertIsNone(self.st.unlink(U1))
        self.link(U2, W1)
        self.assertEqual(self.st.unlink_wallet(W1)["user_id"], U2)
        self.assertIsNone(self.st.link_of(U2))

    def test_only_ids_and_addresses_stored(self):
        self.link(U1, W1)
        cols = [r[1] for r in self.st.db.execute("PRAGMA table_info(premium_links)")]
        self.assertEqual(cols, ["wallet", "user_id", "linked_at", "verify_tx", "balance", "checked_at",
                                "next_check_at", "premium", "below_since", "downgraded"])


class TestService(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = PremiumStore(os.path.join(self.tmp.name, "premium.db"))
        self.ch, self.clock, self.notes, self.up = FakeChain(), Clock(), [], []
        self.removed = {}
        self.svc = service(self.st, self.ch, self.clock, self.notes, self.up,
                           downgrade=lambda u: self.removed.setdefault(u, ["0xgone"]))

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def test_no_reservations_no_rpc(self):
        self.assertEqual(self.svc.verify_tick(), 0)
        self.assertEqual(self.ch.calls, [])

    def test_buy_from_pool_links(self):
        self.st.reserve(U1, W1, NOW)
        self.ch.balance[W1] = 600_000 * E18
        self.ch.buy(W1)
        self.clock.t = NOW + 70
        self.assertEqual(self.svc.verify_tick(), 1)
        link = self.st.link_of(U1)
        self.assertEqual((link["wallet"], link["premium"], link["verify_tx"]), (W1, 1, "0xtx0"))
        self.assertIsNone(self.st.reservation_of(U1))
        self.assertEqual(self.up, [U1])                                                  # подписки — без срока
        u, text = self.notes[-1]
        self.assertEqual(u, U1)
        self.assertIn(f"✅ Wallet verified: <code>{W1}</code>", text)
        self.assertIn("Premium is on.</b> This wallet holds 600,000 $CrawlScan (minimum 500,000).", text)
        # одна верификация: blockNumber, getLogs, balanceOf
        self.assertEqual([c if isinstance(c, str) else c[0] for c in self.ch.calls],
                         ["eth_blockNumber", "eth_getLogs", "balanceOf"])

    def test_buy_through_router(self):
        self.st.reserve(U1, W1, NOW)
        self.ch.buy(W1, frm=ROUTER)
        self.assertEqual(self.svc.verify_tick(), 1)
        self.assertEqual(self.st.link_of(U1)["premium"], 0)                              # баланс 0: без премиума
        self.assertIn("Premium needs at least 500,000 $CrawlScan in this wallet; it holds 0 now.", self.notes[-1][1])
        self.assertEqual(self.up, [])

    def test_transfer_from_wallet_does_not_count(self):
        self.st.reserve(U1, W1, NOW)
        self.ch.balance[W1] = 10 ** 9 * E18
        self.ch.buy(W1, frm=FRIEND)                                                      # прислали с кошелька
        self.ch.buy(W1, ts=NOW - 5)                                                      # покупка до брони
        self.assertEqual(self.svc.verify_tick(), 0)
        self.assertIsNone(self.st.link_of(U1))
        self.assertEqual(self.notes, [])
        self.assertEqual(self.st.reservation_of(U1)["scanned_to"], self.ch.head)

    def test_buy_through_any_app(self):
        """Агрегатор не из списков: PM → AGG → кошелёк в одной транзакции — покупка (по чеку, +1 HTTP)."""
        self.st.reserve(U1, W1, NOW)
        self.ch.balance[W1] = 600_000 * E18
        self.ch.buy(AGG, frm=PM, tx="0xagg", block=1001)
        self.ch.buy(W1, frm=AGG, tx="0xagg", block=1001)
        self.ch.events["0xagg"] = [(PM, SWAP)]
        self.assertEqual(self.svc.verify_tick(), 1)
        self.assertEqual(self.st.link_of(U1)["verify_tx"], "0xagg")
        self.assertEqual([c if isinstance(c, str) else c[0] for c in self.ch.calls],
                         ["eth_blockNumber", "eth_getLogs", "receipts", "balanceOf"])

    def test_app_transfer_without_pool_out(self):
        """Тот же агрегатор, но токены не из пула (кошелёк → AGG → кошелёк) — не покупка."""
        self.st.reserve(U1, W1, NOW)
        self.ch.buy(AGG, frm=FRIEND, tx="0xgift", block=1001)
        self.ch.buy(W1, frm=AGG, tx="0xgift", block=1001)
        self.ch.buy(W1, frm=FRIEND, tx="0xplain")
        self.assertEqual(self.svc.verify_tick(), 0)
        self.assertIsNone(self.st.link_of(U1))
        self.assertEqual(self.ch.calls[2], ("receipts", ("0xgift", "0xplain")))           # один батч на проход

    def test_liquidity_withdrawal_does_not_count(self):
        """Вывод ликвидности: PM → менеджер позиций → кошелёк, в чеке ModifyLiquidity без свопа — не покупка."""
        self.st.reserve(U1, W1, NOW)
        self.ch.balance[W1] = 600_000 * E18
        self.ch.buy(AGG, frm=PM, tx="0xlp", block=1001)
        self.ch.buy(W1, frm=AGG, tx="0xlp", block=1001)
        self.ch.events["0xlp"] = [(PM, MODIFY_LIQUIDITY)]
        self.assertEqual(self.svc.verify_tick(), 0)
        self.assertIsNone(self.st.link_of(U1))
        self.assertEqual(self.notes, [])
        self.assertEqual(self.st.reservation_of(U1)["scanned_to"], self.ch.head)          # прочитано, не перечитывается

    def test_curve_buy_through_app(self):
        self.st.reserve(U1, W1, NOW)
        self.ch.buy(AGG, frm=CURVE, tx="0xc", block=1001)
        self.ch.buy(W1, frm=AGG, tx="0xc", block=1001)
        self.ch.events["0xc"] = [(CURVE, CURVE_BUY)]
        self.assertEqual(self.svc.verify_tick(), 1)

    def test_receipt_not_ready_is_retried(self):
        self.st.reserve(U1, W1, NOW)
        self.ch.buy(AGG, frm=PM, tx="0xagg", block=1001)
        self.ch.buy(W1, frm=AGG, tx="0xagg", block=1001)
        self.ch.events["0xagg"] = [(PM, SWAP)]
        self.ch.missing.add("0xagg")
        self.assertEqual(self.svc.verify_tick(), 0)
        self.assertIsNone(self.st.reservation_of(U1)["scanned_to"])                       # блоки не «прочитаны»
        self.ch.missing.clear()
        self.assertEqual(self.svc.verify_tick(), 1)

    def test_reads_from_scanned_block(self):
        self.st.reserve(U1, W1, NOW)
        self.ch.head = 5000
        self.svc.verify_tick()
        self.assertEqual(self.ch.calls[1][1:3], (5000 - premium_service.BACK_BLOCKS, 5000))
        self.ch.head = 5010
        self.svc.verify_tick()
        self.assertEqual(self.ch.calls[3][1:3], (5001, 5010))                            # продолжает, а не заново

    def test_expired(self):
        self.st.reserve(U1, W1, NOW)
        self.clock.t = NOW + 900 + premium_service.FINAL_S - 1
        self.svc.verify_tick()
        self.assertIsNotNone(self.st.reservation_of(U1))                                 # ждём, пока нода отдаст блоки
        self.ch.buy(W1, ts=NOW + 899)                                                    # поздняя покупка внутри брони
        self.clock.t = NOW + 900 + premium_service.FINAL_S
        self.assertEqual(self.svc.verify_tick(), 1)
        self.st.reserve(U2, W2, NOW + 1000)
        self.clock.t = NOW + 1000 + 900 + premium_service.FINAL_S
        self.svc.verify_tick()
        self.assertIsNone(self.st.reservation_of(U2))
        self.assertEqual(self.notes[-1], (U2, f"⌛ Verification expired: no $CrawlScan buy to <code>{W2}</code> in "
                                              "15 minutes. Send /verify to try again."))

    def test_balance_daily_paused_grace_trim(self):
        self.st.reserve(U1, W1, NOW)
        self.ch.balance[W1] = 600_000 * E18
        self.ch.buy(W1)
        self.svc.verify_tick()
        self.assertEqual(self.svc.balance_tick(), [])                                    # проверка раз в сутки
        self.ch.balance[W1] = 400_000 * E18
        day = NOW + premium.CHECK_EVERY
        self.clock.t = day
        self.assertEqual(self.svc.balance_tick(), [(W1, "paused")])
        self.assertIn("Premium is paused: <code>" + W1 + "</code> holds 400,000 $CrawlScan, below 500,000.",
                      self.notes[-1][1])
        link = self.st.link_of(U1)
        self.assertEqual(premium.watch_terms(link, day + 6 * 86400), (10, None))         # 7 дней всё остаётся
        self.assertEqual(premium.watch_terms(link, day + 7 * 86400), (3, 7))
        for d in range(1, 7):
            self.clock.t = day + d * premium.CHECK_EVERY
            self.assertEqual(self.svc.balance_tick(), [])
        self.clock.t = day + 7 * premium.CHECK_EVERY
        self.assertEqual(self.svc.balance_tick(), [(W1, "trim")])
        self.assertEqual(self.removed, {U1: ["0xgone"]})
        self.assertIn("Your watchlist is back to 3 tokens, 7 days each.\n\nStopped watching:\n<code>0xgone</code>",
                      self.notes[-1][1])
        self.clock.t += premium.CHECK_EVERY
        self.assertEqual(self.svc.balance_tick(), [])                                    # обрезка — один раз
        self.ch.balance[W1] = 700_000 * E18
        self.clock.t += premium.CHECK_EVERY
        self.assertEqual(self.svc.balance_tick(), [(W1, "on")])
        self.assertEqual(self.up, [U1, U1])
        self.assertTrue(self.st.link_of(U1)["premium"])

    def test_back_within_grace(self):
        self.st.reserve(U1, W1, NOW)
        self.ch.balance[W1] = 600_000 * E18
        self.ch.buy(W1)
        self.svc.verify_tick()
        self.ch.balance[W1] = 1
        self.clock.t = NOW + premium.CHECK_EVERY
        self.svc.balance_tick()
        self.ch.balance[W1] = 500_000 * E18                                              # ровно порог — премиум
        self.clock.t += premium.CHECK_EVERY
        self.assertEqual(self.svc.balance_tick(), [(W1, "on")])
        self.assertEqual(self.removed, {})

    def test_balance_in_background(self):
        seen = []
        self.svc.balance_tick = lambda: seen.append(premium_service.priority.is_background())
        self.svc.verify_tick = lambda: seen.append(premium_service.priority.is_background())
        with mock.patch.dict(os.environ, {"BACKGROUND_RPS": "2"}):
            self.svc.tick()
        self.assertEqual(seen, [False, True])                                            # брони — сразу, балансы — фоном


class Site(unittest.TestCase):
    """Настоящий сервер с подставной сетью: PREMIUM_ENABLED, ALERTS_ENABLED, свои базы во временной папке."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    ENV = {"PREMIUM_ENABLED": "true", "ALERTS_ENABLED": "true"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"DRAW_DB_PATH": os.path.join(self.tmp.name, "draw.db"),
                                                "PREMIUM_DB_PATH": "", "ALERTS_API_SECRET": SECRET,
                                                "PREMIUM_MIN_TOKENS": ""} | self.ENV)
        self.env.__enter__()
        self.fakes = fakes.patched()
        self.fakes.__enter__()
        self.sent = []
        self.notify = mock.patch.object(server, "notify_message", side_effect=lambda c, t, m=None: self.sent.append((c, t)))
        self.notify.__enter__()
        self.ch = FakeChain()
        self.attempts = mock.patch.object(server, "PREMIUM_ATTEMPTS", premium.Attempts())
        self.attempts.__enter__()

    def tearDown(self):
        self.attempts.__exit__(None, None, None)
        self.notify.__exit__(None, None, None)
        self.fakes.__exit__(None, None, None)
        with server._stores_lock:
            for st in list(server._alerts_stores.values()) + list(server._premium["store"].values()):
                st.close()
            server._alerts_stores.clear()
            server._premium["store"].clear()
            server._premium["svc"] = None
        self.env.__exit__(None, None, None)
        self.tmp.cleanup()

    def svc(self):
        s = premium_service.Service(server.premium_store(), server.notify_message, server.premium_upgrade,
                                    server.premium_downgrade, self.ch, log=lambda m: None)
        server._premium["svc"] = s
        return s

    def req(self, path, body=None, secret=SECRET):
        headers = {"content-type": "application/json"} | ({"X-Alerts-Secret": secret} if secret is not None else {})
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                   method="POST" if body is not None else "GET", headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def watch(self, token, chat=U1):
        return self.req("/api/alerts/watch", {"chat_id": chat, "token": token})

    def verify(self, user, wallet, balance):
        """Бронь через API и покупка из пула → привязка (поток сервиса — один проход)."""
        code, r = self.req("/api/premium/reserve", {"user_id": user, "wallet": wallet})
        self.assertEqual((code, r["state"]), (200, "reserved"))
        self.ch.balance[wallet] = balance
        self.ch.buy(wallet, ts=int(time.time()) + 1)
        self.assertEqual(self.svc().verify_tick(), 1)


class TestDisabled(Site):
    ENV = {"PREMIUM_ENABLED": "false", "ALERTS_ENABLED": "true"}

    def test_everything_as_before(self):
        with mock.patch.object(server, "PremiumStore", side_effect=AssertionError("premium db opened")):
            for path in ("/api/premium/status?user_id=1",):
                self.assertEqual(self.req(path)[0], 404)
            self.assertEqual(self.req("/api/premium/reserve", {"user_id": 1, "wallet": W1})[0], 404)
            self.assertEqual(self.req("/api/premium/admin_unlink", {"wallet": W1})[0], 404)
            code, cfg = self.req("/api/config")
            self.assertNotIn("premium", cfg)
            codes = [self.watch(t)[0] for t in TOKENS[:4]]
            self.assertEqual(codes, [200, 200, 200, 409])
            code, r = self.req(f"/api/alerts/list?chat_id={U1}")
            self.assertEqual((r["limit"], r["days"]), (3, 7))
            self.assertTrue(all(w["expires_at"] - w["created_at"] == 7 * 86400 for w in r["items"]))
            self.assertIsNone(server.start_premium())
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "premium.db")))


class TestPremiumAPI(Site):

    def test_secret_required(self):
        for secret in (None, "", "wrong"):
            self.assertEqual(self.req("/api/premium/status?user_id=1", secret=secret)[0], 403)
            self.assertEqual(self.req("/api/premium/reserve", {"user_id": 1, "wallet": W1}, secret=secret)[0], 403)

    def test_config_and_own_db(self):
        self.assertTrue(self.req("/api/config")[1]["premium"])
        self.req("/api/premium/status?user_id=1")
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "premium.db")))   # своя база рядом с draw.db

    def test_reserve(self):
        code, r = self.req("/api/premium/reserve", {"user_id": U1, "wallet": W1.upper().replace("0X", "0x")})
        self.assertEqual((code, r["state"], r["wallet"], r["minutes"]), (200, "reserved", W1, 15))
        self.assertEqual(self.req("/api/premium/reserve", {"user_id": U1, "wallet": W1})[1]["state"], "pending")
        code, r = self.req("/api/premium/reserve", {"user_id": U2, "wallet": W1})
        self.assertEqual((code, r["message"]), (409, "This wallet is already linked to another account."))
        for bad in ("0x123", "", None, "0x8366a39cc670b4001a1121b8f6a443a643e40951"):   # V4 PoolManager — не кошелёк
            self.assertEqual(self.req("/api/premium/reserve", {"user_id": U1, "wallet": bad})[0], 400, bad)
        self.assertEqual(self.req("/api/premium/reserve", {"wallet": W1})[0], 400)
        st = self.req(f"/api/premium/status?user_id={U1}")[1]
        self.assertEqual((st["linked"], st["reservation"]["wallet"]), (False, W1))

    def test_attempts_limit(self):
        for i in range(premium.ATTEMPTS_PER_HOUR):
            self.assertEqual(self.req("/api/premium/reserve", {"user_id": U1, "wallet": "0x" + f"{i + 1:02x}" * 20})[0], 200)
        code, r = self.req("/api/premium/reserve", {"user_id": U1, "wallet": W3})
        self.assertEqual((code, r["error"]), (429, "too many attempts"))

    def test_taken_after_link(self):
        self.verify(U1, W1, 600_000 * E18)
        code, r = self.req("/api/premium/reserve", {"user_id": U2, "wallet": W1})
        self.assertEqual(code, 409)
        self.assertEqual(self.req("/api/premium/reserve", {"user_id": U1, "wallet": W1})[1]["state"], "yours")
        st = self.req(f"/api/premium/status?user_id={U1}")[1]
        self.assertEqual((st["linked"], st["premium"], st["wallet"], st["balance_tokens"], st["min_tokens"],
                          st["watch_limit"], st["watch_days"]), (True, True, W1, 600_000, 500_000, 10, None))
        self.assertEqual(self.sent[-1][0], U1)

    def test_watchlist_regular_and_premium(self):
        codes = [self.watch(t, chat=U2)[0] for t in TOKENS[:4]]                           # обычный: 3 и 7 дней
        self.assertEqual(codes, [200, 200, 200, 409])
        self.assertEqual(self.watch(TOKENS[3], chat=U2)[1]["message"], "You can watch up to 3 tokens. Unwatch one first.")
        r = self.req(f"/api/alerts/list?chat_id={U2}")[1]
        self.assertEqual((r["limit"], r["days"]), (3, 7))
        self.watch(TOKENS[0])                                                             # U1 до премиума: 7 дней
        self.verify(U1, W1, 600_000 * E18)
        r = self.req(f"/api/alerts/list?chat_id={U1}")[1]
        self.assertEqual((r["limit"], r["days"]), (10, None))
        self.assertEqual(r["items"][0]["expires_at"], alerts.NO_EXPIRY)                   # стала без срока
        codes = [self.watch(t)[0] for t in TOKENS[1:11]]
        self.assertEqual(codes, [200] * 9 + [409])
        code, w = self.watch(TOKENS[1])
        self.assertEqual((w["days"], w["expires_at"]), (None, alerts.NO_EXPIRY))
        r = self.req(f"/api/alerts/list?chat_id={U2}")[1]                                 # у обычного всё как было
        self.assertEqual((r["limit"], len(r["items"])), (3, 3))

    def test_grace_then_trim(self):
        self.verify(U1, W1, 600_000 * E18)
        for t in TOKENS[:8]:
            self.assertEqual(self.watch(t)[0], 200)
        svc = server.premium_svc()
        st = server.premium_store()
        st.db.execute("UPDATE premium_links SET next_check_at = 0")
        self.ch.balance[W1] = 1
        self.assertEqual(svc.balance_tick(), [(W1, "paused")])
        self.assertEqual(self.req(f"/api/alerts/list?chat_id={U1}")[1]["limit"], 10)       # 7 дней всё остаётся
        self.assertIsNotNone(self.req(f"/api/premium/status?user_id={U1}")[1]["grace_until"])
        st.db.execute("UPDATE premium_links SET next_check_at = 0, below_since = ?", (int(time.time()) - 7 * 86400,))
        self.assertEqual(svc.balance_tick(), [(W1, "trim")])
        r = self.req(f"/api/alerts/list?chat_id={U1}")[1]
        self.assertEqual((r["limit"], r["days"]), (3, 7))
        self.assertEqual([w["token"] for w in r["items"]], TOKENS[:3])                     # остаются самые старые
        self.assertTrue(all(w["expires_at"] <= time.time() + 7 * 86400 + 5 for w in r["items"]))
        user, text = self.sent[-1]
        self.assertEqual(user, U1)
        self.assertIn("Stopped watching:\n" + "\n".join(f"<code>{t}</code>" for t in TOKENS[3:8]), text)

    def test_unlink(self):
        self.verify(U1, W1, 600_000 * E18)
        for t in TOKENS[:5]:
            self.watch(t)
        code, r = self.req("/api/premium/unlink", {"user_id": U1})
        self.assertEqual((code, r["wallet"], r["removed"]), (200, W1, TOKENS[3:5]))
        self.assertEqual(self.req("/api/premium/unlink", {"user_id": U1})[1]["wallet"], None)
        self.assertEqual(self.req("/api/premium/reserve", {"user_id": U2, "wallet": W1})[1]["state"], "reserved")

    def test_admin_unlink(self):
        self.verify(U1, W1, 600_000 * E18)
        for t in TOKENS[:4]:
            self.watch(t)
        code, r = self.req("/api/premium/admin_unlink", {"wallet": W1})
        self.assertEqual((code, r["found"], r["removed"]), (200, True, 1))
        self.assertEqual(self.sent[-1][0], U1)
        self.assertIn(f"🔓 Your wallet <code>{W1}</code> was unlinked by CrawlScan", self.sent[-1][1])
        self.assertFalse(self.req(f"/api/premium/status?user_id={U1}")[1]["linked"])
        self.assertFalse(self.req("/api/premium/admin_unlink", {"wallet": W1})[1]["found"])
        self.assertEqual(self.req("/api/premium/admin_unlink", {"wallet": "nope"})[0], 400)
        self.assertEqual(self.req("/api/premium/reserve", {"user_id": U2, "wallet": W1})[1]["state"], "reserved")

    def test_real_service_and_start_do_not_hang(self):
        """premium_svc() и start_premium() как на сервере (не подставной сервис): без взаимоблокировки замков."""
        out = {}
        with mock.patch.object(premium_service.Service, "start", lambda svc: svc):   # поток не запускаем
            t = threading.Thread(target=lambda: out.update(svc=server.premium_svc(), started=server.start_premium()),
                                 daemon=True)
            t.start()
            t.join(5)
        self.assertFalse(t.is_alive(), "premium_svc() / start_premium() зависли")
        self.assertIs(out["svc"], out["started"])
        self.assertIs(out["svc"].store, server.premium_store())

    def test_status_of_linked_user_uses_real_service(self):
        self.verify(U1, W1, 600_000 * E18)
        server._premium["svc"] = None                    # статус создаёт сервис сам (decimals)
        with mock.patch.object(premium_service.Service, "decimals", lambda svc: 18):
            code, st = self.req(f"/api/premium/status?user_id={U1}")
        self.assertEqual((code, st["balance_tokens"]), (200, 600_000))

    def test_db_failure_is_500(self):
        with mock.patch.object(PremiumStore, "link_of", side_effect=RuntimeError("locked")):
            self.assertEqual(self.req("/api/premium/status?user_id=1")[0], 500)
            self.assertEqual(self.watch(TOKENS[0])[0], 200)                                # alerts — как обычный

    def test_bot_client_against_server(self):
        api = CrawlScan(self.base, alerts_secret=SECRET)
        self.assertEqual(api.premium_reserve(U1, W1)["state"], "reserved")
        self.assertEqual(api.premium_reserve(U2, W1)["status"], 409)
        self.assertFalse(api.premium_status(U1)["linked"])
        self.assertIsNone(api.premium_unlink(U1)["wallet"])
        self.assertFalse(api.premium_admin_unlink(W1)["found"])


# ---------- бот ----------

class PremiumAPI:
    """Сайт для бота: /api/config → alerts и premium, ответы /api/premium/* — как server.premium_api."""
    base = "https://crawlscan.test"
    alerts_secret = "secret"

    def __init__(self, on=True, premium_users=()):
        self.on, self.premium_users, self.reserves, self.calls = on, set(premium_users), [], []

    def config(self):
        self.calls.append("config")
        return {"alerts": True} | ({"premium": True} if self.on else {})

    def premium_reserve(self, user_id, wallet):
        self.reserves.append((user_id, wallet))
        if wallet.lower() == W2:
            return {"status": 409, "error": "taken"}
        return {"ok": True, "state": "reserved", "wallet": wallet.lower(), "minutes": 15, "expires_at": NOW + 900}

    def premium_status(self, user_id):
        self.calls.append(("status", user_id))
        if user_id in self.premium_users:
            return {"linked": True, "premium": True, "wallet": W1, "balance_tokens": 1_234_567, "min_tokens": 500_000,
                    "next_check_at": NOW + 3600, "grace_until": None, "reservation": None}
        return {"linked": False, "premium": False, "min_tokens": 500_000, "reservation": None}

    def premium_unlink(self, user_id):
        return {"ok": True, "wallet": W1, "removed": [TOKENS[5]]}

    def premium_admin_unlink(self, wallet):
        return {"ok": True, "found": True, "wallet": wallet.lower(), "removed": 2}

    def scan(self, token):
        return "job"

    def result(self, job):
        return {"done": True, "result": {"token": token_lc(), "chain": "robinhood", "header": {"ticker": "CRAWL"},
                                         "holders": [{}] * 20, "operators": [{"wallets": ["a"]}], "score": 79,
                                         "band": "OK", "gates": [], "metrics": {"impact": 0.3}}}


def token_lc():
    return TOKENS[0]


def private(text, user=U1):
    return {"message": {"message_id": 7, "chat": {"id": user, "type": "private"}, "from": {"id": user}, "text": text}}


class TestBot(unittest.TestCase):

    def make(self, admins=(), **kw):
        api = PremiumAPI(**kw)
        tg = TGStub()
        bot = bm.Bot(tg, api, username="crawlscanbot", spawn=lambda f: f(), sleep=lambda s: None,
                     log=lambda m: None, admin_ids=set(admins))
        return bot, tg, api

    def test_verify_flow(self):
        bot, tg, api = self.make()
        bot.handle_update(private("/verify"))
        self.assertEqual(tg.texts[-1], T.ASK_WALLET)
        bot.handle_update(private("hello"))
        self.assertEqual(tg.texts[-1], T.ASK_WALLET_AGAIN)
        bot.handle_update(private(W1))                                                    # адрес — кошелёк, не скан
        self.assertEqual(api.reserves, [(U1, W1)])
        self.assertEqual(tg.texts[-1], f"⭐ <b>Verify your wallet</b>\n<code>{W1}</code>\n\n"
                                       "Buy any amount of $CrawlScan to this wallet within 15 minutes to verify it.\n\n"
                                       "Only buys count: tokens sent from another wallet don't. I'll message you as "
                                       "soon as I see the buy (until " + T._hm(NOW + 900) + ").")
        self.assertEqual(tg.markups[-1]["inline_keyboard"][0][0]["text"], "Buy $CrawlScan")
        bot.handle_update(private(f"/verify {W2}"))
        self.assertEqual(tg.texts[-1], "This wallet is already linked to another account.")

    def test_off_is_hint_as_before(self):
        bot, tg, api = self.make(on=False)
        for cmd in ("/verify", f"/verify {W1}", "/premium", "/unlink"):
            bot.handle_update(private(cmd))
            self.assertEqual(tg.texts[-1], T.HINT)
        self.assertEqual(api.reserves, [])
        bot.handle_update(private(W1))                                                    # адрес — снова скан
        self.assertTrue(tg.texts[-1].startswith("🕷 crawling"))

    def test_premium_status_view(self):
        bot, tg, api = self.make(premium_users=[U1])
        bot.premium_text(U1, U1, "premium", "")
        text, _ = bot.premium_text(U1, U1, "premium", "", now=NOW)
        self.assertIn("⭐ <b>Premium features for $CrawlScan holders</b>", text)
        self.assertIn(f"Wallet: <code>{W1}</code>\nBalance: 1,234,567 $CrawlScan\nPremium: ✅ active", text)
        self.assertIn("Next balance check: " + T._hm(NOW + 3600) + " (in 1h 0m)", text)
        text, _ = bot.premium_text(U2, U2, "premium", "", now=NOW)
        self.assertIn("Hold 500,000+ $CrawlScan in a linked wallet to unlock everything below.", text)
        self.assertIn("Your status: no wallet linked.", text)
        text, _ = bot.premium_text(U1, U1, "unlink", "")
        self.assertIn(f"🔓 Wallet <code>{W1}</code> unlinked, Premium is off.", text)

    def test_admin_unlink_only_admin(self):
        bot, tg, api = self.make(admins=[U2])
        bot.handle_update(private(f"/admin_unlink {W1}", user=U1))
        self.assertEqual(tg.texts[-1], T.HINT)
        bot.handle_update(private(f"/admin_unlink {W1}", user=U2))
        self.assertEqual(tg.texts[-1], f"🔓 Unlinked <code>{W1}</code>. The account was told, 2 watched tokens removed.")
        bot.handle_update(private("/admin_unlink nope", user=U2))
        self.assertEqual(tg.texts[-1], T.ADMIN_UNLINK_USAGE)

    def test_badge(self):
        bot, tg, api = self.make(premium_users=[U1])
        bot.run_scan(U1, 5, TOKENS[0], False, True, U1)
        self.assertTrue(tg.texts[-1].endswith("\n\n⭐ Premium"))
        bot.run_scan(U2, 5, TOKENS[0], False, True, U2)
        self.assertNotIn("Premium", tg.texts[-1])
        bot.run_scan(U1, 5, TOKENS[0], False, True, U1)
        self.assertEqual(api.calls.count(("status", U1)), 1)                              # статус помним
        self.assertEqual(api.calls.count("config"), 1)                                    # один /api/config на алерты и премиум
        bot, tg, api = self.make(on=False, premium_users=[U1])
        bot.run_scan(U1, 5, TOKENS[0], False, True, U1)
        self.assertNotIn("Premium", tg.texts[-1])
        self.assertNotIn(("status", U1), api.calls)

    def test_commands_menu(self):
        bot, tg, api = self.make()
        self.assertEqual([c["command"] for c in bm.PREMIUM_COMMANDS], ["verify", "premium", "unlink"])
        with mock.patch.dict(os.environ, {"PREMIUM_ADMIN_ID": "12, x, 34"}):
            self.assertEqual(bm.admin_ids_env(), {12, 34})


class TGStub:
    def __init__(self):
        self.texts, self.markups, self.n = [], [], 0

    def redact(self, s):
        return str(s)

    def call(self, method, **p):
        if method in ("sendMessage", "editMessageText"):
            self.texts.append(p["text"])
            self.markups.append(p.get("reply_markup"))
            self.n += 1
            return {"message_id": self.n}
        return True


if __name__ == "__main__":
    unittest.main()
