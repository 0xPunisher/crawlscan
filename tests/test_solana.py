"""Тесты Solana-ветки движка на подставных данных (без сети): сеть по адресу, флаг SOLANA_ENABLED,
окно стаи по сети, unread-входы, токен целиком в кривой."""
import os, time, unittest
from contextlib import ExitStack
from unittest import mock

import fakes
from fakes import ch, engine, market
from chains import solana as sol
import detect as d

SOL_TOKEN = sol.b58encode(bytes([7]) * 32)
SOL_CURVE = sol.b58encode(bytes([8]) * 32)
SOL_WALLETS = [sol.b58encode(bytes([20 + i]) * 32) for i in range(12)]
EVM_WALLETS = ["0x" + f"{20 + i:02x}" * 20 for i in range(12)]
LAUNCH_SLOT, LAUNCH_TS = 1000, int(time.time()) - 3600   # свежий запуск: час назад
SUPPLY = 10 ** 15
PACK = 3                                   # первые 3 кошелька — стая в соседних слотах


def enabled(on=True):
    return mock.patch.dict(os.environ, {"SOLANA_ENABLED": "true" if on else "false"})


def balances(wallets):
    return {w: SUPPLY // 100 * (30 - i) for i, w in enumerate(wallets)}


def patched_chain(mod, token, wallets, entries, history, bal=None):
    """Адаптер сети mod подменён: base задан напрямую, переводов нет, сеть запрещена."""
    stack = ExitStack()
    engine._HIST.clear()
    curve = SOL_CURVE if mod is sol else fakes.CURVE
    bal = balances(wallets) if bal is None else bal
    base = {"supply": SUPPLY, "circulating": sum(bal.values()), "holders_total": len(bal), "balances": bal}
    p = lambda name, **kw: stack.enter_context(mock.patch.object(mod, name, **kw))
    p("_post", side_effect=fakes._no_network)
    p("get_launch", side_effect=lambda t: {"block": LAUNCH_SLOT, "curve": curve, "deployer": "deployer",
                                           "tx": "launch"} if t == token else None)
    p("token_facts", side_effect=lambda t, l: {"supply": SUPPLY, "transfers": [], "excluded": {curve},
                                               "market": {curve}, "reserve": SUPPLY - base["circulating"],
                                               "base": base})
    p("classify_entries", side_effect=lambda t, trs, ws: {w: entries[w] for w in ws})
    p("block_timestamps", side_effect=lambda blocks: {b: LAUNCH_TS + (b - LAUNCH_SLOT) for b in blocks})
    p("wallet_distinct_tokens", side_effect=history)
    p("is_contract", side_effect=lambda addrs: {x: False for x in addrs})
    p("token_meta", return_value={"name": "Synthetic", "symbol": "SYN"})
    stack.enter_context(mock.patch.object(market, "fetch_market", return_value={}))
    return stack


def buy(slot, eth, tx):
    return {"kind": "buy", "tx": tx, "block": slot, "via": "pool", "eth_in": eth, "unread": False}


def pack_entries(wallets, eth):
    """Первые PACK кошельков купили в слотах +100, +101, +102 одинаковыми суммами; остальные — далеко друг от друга."""
    return {w: buy(LAUNCH_SLOT + (100 + i if i < PACK else 300 + 10 * i), eth, f"tx{i}") for i, w in enumerate(wallets)}


def pack_history(wallets):
    return lambda w, b, t, window=None, cap=4: 0 if wallets.index(w) < PACK else cap


class TestChainDetection(unittest.TestCase):

    def test_address_to_chain(self):
        with enabled():
            self.assertEqual(engine.chain_of("0x" + "AB" * 20), ("robinhood", "0x" + "ab" * 20))
            self.assertEqual(engine.chain_of(" " + SOL_TOKEN + " "), ("solana", SOL_TOKEN))   # регистр сохраняется
            self.assertEqual(engine.chain_of("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")[0], "solana")
            for bad in ("", None, "hello", "0x123", "0x" + "g" * 40, "ab" * 20, SOL_TOKEN + "1", "0" * 44):
                with self.assertRaises(engine.ScanError) as cm:
                    engine.chain_of(bad)
                self.assertEqual(str(cm.exception), "not a token address", bad)

    def test_solana_disabled(self):
        for value in ("false", "", None):                                            # None — переменной нет
            with mock.patch.dict(os.environ, {"SOLANA_ENABLED": value or ""}):
                if value is None:
                    del os.environ["SOLANA_ENABLED"]
                with self.assertRaises(engine.ScanError) as cm:
                    engine.chain_of(SOL_TOKEN)
                self.assertEqual(str(cm.exception), "Solana support is coming soon")
        with enabled(False), patched_chain(sol, SOL_TOKEN, SOL_WALLETS, pack_entries(SOL_WALLETS, 10 ** 8),
                                           pack_history(SOL_WALLETS)):
            events = []
            with self.assertRaises(engine.ScanError):
                engine.scan(SOL_TOKEN, events.append)
            self.assertEqual(events, [])
            self.assertFalse(sol.get_launch.called)
        self.assertEqual(engine.chain_of("0x" + "ab" * 20)[0], "robinhood")            # Robinhood флаг не трогает


class TestPackWindow(unittest.TestCase):

    def test_find_packs_window(self):
        sig = {f"w{i}": {"kind": "buy", "virgin": True, "eth_in": 100, "is_deployer": False, "block": 100 + i}
               for i in range(3)}
        self.assertEqual(d.find_packs(sig), [])                                       # тот же блок — стаи нет
        self.assertEqual(d.find_packs(sig, window=2)[0]["wallets"], ["w0", "w1", "w2"])
        sig["w2"]["block"] = 103                                                      # дальше 2 слотов от первого
        self.assertEqual(d.find_packs(sig, window=2), [])

    def scan(self, mod, token, wallets, eth):
        with enabled(), patched_chain(mod, token, wallets, pack_entries(wallets, eth), pack_history(wallets)):
            events = []
            return engine.scan(token, events.append), events

    def test_adjacent_slots_pack_on_solana_only(self):
        res, events = self.scan(sol, SOL_TOKEN, SOL_WALLETS, 10 ** 8)
        self.assertEqual(res["chain"], "solana")
        self.assertTrue(all(e["chain"] == "solana" for e in events))
        packs = [o for o in res["operators"] if o["level"] == "pack"]
        self.assertEqual(len(packs), 1)
        self.assertEqual(sorted(packs[0]["wallets"]), sorted(SOL_WALLETS[:PACK]))
        self.assertTrue(any("SOL" in e["detail"] for e in events if e["type"] == "wallet_flag"))

        res, events = self.scan(ch, fakes.TOKEN, EVM_WALLETS, 10 ** 16)
        self.assertEqual(res["chain"], "robinhood")
        self.assertTrue(all(e["chain"] == "robinhood" for e in events))
        self.assertEqual([o for o in res["operators"] if len(o["wallets"]) > 1], [])
        self.assertEqual(res["packs"], [])


class TestSolanaReserve(unittest.TestCase):
    """reserve в token_facts: на кривой — виртуальный резерв, после миграции — реальный баланс пула."""
    POOL = sol.b58encode(bytes([9]) * 32)

    def facts(self, complete, virtual):
        rows = [{"account": "a1", "owner": SOL_CURVE, "amount": 700, "pda": True, "label": "bonding curve"},
                {"account": "a2", "owner": self.POOL, "amount": 900, "pda": True, "label": "PumpSwap pool"},
                {"account": "a3", "owner": SOL_WALLETS[0], "amount": 50, "pda": False, "label": "wallet"}]
        launch = {"block": LAUNCH_SLOT, "curve": SOL_CURVE, "complete": complete, "virtual_token_reserves": virtual}
        base = {"supply": 1650, "circulating": 50, "holders_total": 1, "balances": {SOL_WALLETS[0]: 50}}
        with ExitStack() as st:
            p = lambda name, **kw: st.enter_context(mock.patch.object(sol, name, **kw))
            p("_post", side_effect=fakes._no_network)
            p("supply_base", return_value=base)
            p("get_token_transfers", return_value=[])
            p("top_accounts", return_value=rows)
            st.enter_context(mock.patch.dict(sol._OWNER_PROG, {self.POOL: sol.PUMP_SWAP, SOL_CURVE: sol.PUMP}))
            return sol.token_facts(SOL_TOKEN, launch)

    def test_curve_uses_virtual_reserves(self):
        self.assertEqual(self.facts(False, 1_073_000)["reserve"], 1_073_000)

    def test_curve_without_virtual_uses_real_balance(self):
        self.assertEqual(self.facts(False, None)["reserve"], 700)

    def test_migrated_uses_real_pool_balance(self):
        self.assertEqual(self.facts(True, 1_073_000)["reserve"], 900)

    def test_parse_curve_layout(self):
        data = (b"\0" * 8 + (1_073_000_000_000_000).to_bytes(8, "little") + b"\0" * 32 + b"\1"
                + sol.b58decode(SOL_WALLETS[1]))
        cs = sol._parse_curve(sol.base64.b64encode(data).decode())
        self.assertEqual(cs, {"virtual_token_reserves": 1_073_000_000_000_000, "complete": True,
                              "creator": SOL_WALLETS[1]})


class TestSolanaScan(unittest.TestCase):

    def test_thin_liquidity_from_header(self):
        """Ликвидность из шапки GeckoTerminal < $1,000 → мягкое правило; нет шапки — не применяется."""
        entries = {w: buy(LAUNCH_SLOT + 300 + 10 * i, 10 ** 8, f"tx{i}") for i, w in enumerate(SOL_WALLETS)}
        history = lambda w, b, t, window=None, cap=4: cap
        for liq, thin in ((114.0, True), (None, False), (5000.0, False)):
            with enabled(), patched_chain(sol, SOL_TOKEN, SOL_WALLETS, entries, history), \
                    mock.patch.object(market, "fetch_market", return_value={"liquidity_usd": liq}):
                res = engine.scan(SOL_TOKEN)
            gate = [g for g in res["gates"] if "liquidity too thin" in g]
            self.assertEqual(bool(gate), thin, liq)
            if thin:
                self.assertEqual(gate, ["soft: liquidity too thin ($114)"])
                self.assertNotIn(res["band"], ("CLEAN", "OK"))
                self.assertEqual(res["header"]["liquidity_usd"], 114.0)

    def test_unread_entries_not_virgin(self):
        entries = pack_entries(SOL_WALLETS, 10 ** 8)
        lost = SOL_WALLETS[5:8]
        for w in lost:
            entries[w] = {"kind": None, "tx": None, "block": None, "via": None, "eth_in": None, "unread": True}
        history = mock.Mock(side_effect=lambda w, b, t, window=None, cap=4: 0)    # прочитай мы их — были бы девственными
        with enabled(), patched_chain(sol, SOL_TOKEN, SOL_WALLETS, entries, history):
            events = []
            res = engine.scan(SOL_TOKEN, events.append)
        self.assertEqual(sorted(res["unread"]), sorted(lost))
        self.assertFalse({c.args[0] for c in history.call_args_list} & set(lost))   # историю не читали
        sig = {h["wallet"]: h["signals"] for h in res["holders"]}
        flags = {e["wallet"]: e for e in events if e["type"] == "wallet_flag"}
        for w in lost:
            self.assertTrue(sig[w]["unread"])
            self.assertFalse(sig[w]["virgin"])
            self.assertFalse(sig[w]["sniper"])
            self.assertIsNone(sig[w]["kind"])
            self.assertIn("unread", flags[w]["flags"])
            self.assertNotIn("virgin", flags[w]["flags"])
            self.assertIn("entry not found", flags[w]["detail"])
        self.assertEqual(sum(1 for o in res["operators"] if set(lost) & set(o["wallets"]) and len(o["wallets"]) > 1), 0)
        self.assertEqual(events[-1]["type"], "done")

    def test_all_in_curve_too_early(self):
        with enabled(), patched_chain(sol, SOL_TOKEN, [], {}, lambda *a, **k: 0, bal={}):
            events = []
            res = engine.scan(SOL_TOKEN, events.append)
        self.assertEqual(res["band"], d.TOO_EARLY)
        self.assertIsNone(res["score"])
        self.assertEqual(res["holders"], [])
        self.assertEqual(res["circulating"], 0)
        self.assertEqual(events[-1]["type"], "done")

    def test_not_pump_token(self):
        other = sol.b58encode(bytes([9]) * 32)
        with enabled(), patched_chain(sol, SOL_TOKEN, SOL_WALLETS, pack_entries(SOL_WALLETS, 10 ** 8),
                                      pack_history(SOL_WALLETS)):
            with self.assertRaises(engine.ScanError) as cm:
                engine.scan(other)
        self.assertEqual(str(cm.exception), "not a pump.fun token")


if __name__ == "__main__":
    unittest.main()
