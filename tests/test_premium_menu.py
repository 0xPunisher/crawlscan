"""Тесты меню Premium в боте: кнопка [⭐ Premium features] в /start, сообщение /premium (функции, статус) и кнопки
для каждого статуса (не привязан / привязан без премиума / премиум / пауза), Pick tokens (/picktokens, /import),
подтверждение Unlink, переключатель digest. Без сети."""
import unittest

import fakes  # noqa: F401  (фиктивный CRAWLER_RPC до импорта бота и сервера)
import trade
from bot import main as bm, text as T
from test_premium import NOW, U1, U2, W1, TGStub, private
from test_premium_extras import FeatAPI, callback

U3 = 333333
ALL = ("premium_priority", "premium_import", "premium_digest")
CRAWL_TRADE = trade.url("robinhood", T.OFFICIAL_CA.lower(), trade.templates())
FEATURES = ("/verify — link your wallet: buy any amount of $CrawlScan within 15 minutes\n"
            "/picktokens — choose tokens from your linked wallet to watch (we only read public balances, "
            "no keys or wallet connection)\n"
            "<b>Watchlist</b> — up to 10 tokens with no time limit (3 without Premium)\n"
            "/digest — a daily morning summary of your Watchlist (on/off)\n"
            "<b>Priority</b> — your scans skip the queue when the scanner is busy\n"
            "/unlink — unlink your wallet")
HEAD = ("⭐ <b>Premium features for $CrawlScan holders</b>\n\n"
        "Hold 500,000+ $CrawlScan in a linked wallet to unlock everything below.\n\n")


class MenuAPI(FeatAPI):
    """U1 — премиум, U2 — привязан без премиума, U3 — привязан, баланс упал (пауза), остальные — не привязаны."""

    def __init__(self, feats=ALL, **kw):
        super().__init__(feats, premium_users=[U1], **kw)
        self.digest_on, self.unlinked = True, []

    def config(self):
        self.calls.append("config")
        return {"alerts": True} | ({"premium": True} | {f: True for f in self.feats} if self.on else {})

    def premium_status(self, user_id):
        st = super().premium_status(user_id)
        if user_id in (U2, U3):
            st |= {"linked": True, "premium": False, "wallet": W1, "balance_tokens": 120_000,
                   "next_check_at": NOW + 7200, "grace_until": NOW + 3 * 86400 if user_id == U3 else None}
        if "premium_digest" in self.feats:
            st["digest"] = self.digest_on
        return st

    def premium_digest(self, user_id, on=None):
        if on is not None:
            self.digest_on = on
        return super().premium_digest(user_id, on)

    def premium_unlink(self, user_id):
        self.unlinked.append(user_id)
        return super().premium_unlink(user_id)


def buttons(markup):
    return [[b["text"] for b in row] for row in (markup or {}).get("inline_keyboard", [])]


class TestPremiumMenu(unittest.TestCase):

    def make(self, feats=ALL, **kw):
        api = MenuAPI(feats, **kw)
        tg = TGStub()
        bot = bm.Bot(tg, api, username="b", spawn=lambda f: f(), sleep=lambda s: None, log=lambda m: None,
                     admin_ids=set(), banner=None)
        return bot, tg, api

    def view(self, bot, user):
        return bot.premium_text(user, user, "premium", "", now=NOW)

    # --- /start --------------------------------------------------------------------------------------

    def test_start_has_premium_button(self):
        bot, tg, api = self.make()
        bot.handle_update(private("/start"))
        self.assertEqual(buttons(tg.markups[-1])[-1], ["⭐ Premium features"])
        self.assertEqual(tg.markups[-1]["inline_keyboard"][-1][0]["callback_data"], "premium")
        self.assertEqual(buttons(tg.markups[-1])[:2], [["Scan a token", "Help", "🔔 Watchlist"],
                                                       ["Website", "Buy $CrawlScan"]])   # обычные — как были

    def test_start_without_premium_as_before(self):
        bot, tg, api = self.make(on=False)
        bot.handle_update(private("/start"))
        self.assertEqual(tg.markups[-1], T.start_buttons(True))
        self.assertNotIn("⭐ Premium features", str(tg.markups[-1]))
        self.assertEqual(T.start_buttons(False), T.START_BUTTONS)

    def test_button_and_command_show_the_same_message(self):
        bot, tg, api = self.make()
        bot.handle_update(callback("premium"))
        by_button = (tg.texts[-1], tg.markups[-1])
        bot.handle_update(private("/premium"))
        self.assertEqual((tg.texts[-1], tg.markups[-1]), by_button)
        self.assertEqual(tg.n, 2)                                                           # одно сообщение на действие

    # --- статусы -------------------------------------------------------------------------------------

    def test_not_linked(self):
        bot, tg, api = self.make()
        text, markup = self.view(bot, 999)
        self.assertEqual(text, HEAD + FEATURES + "\n\nYour status: no wallet linked.")
        self.assertEqual(markup, {"inline_keyboard": [[{"text": "Verify wallet", "callback_data": "verify"}]]})
        bot.handle_update(callback("verify", user=999))
        self.assertEqual(tg.texts[-1], T.ASK_WALLET)
        bot.handle_update(private(W1, user=999))                                           # адрес — кошелёк, не скан
        self.assertEqual(api.reserves, [(999, W1)])

    def test_linked_without_premium(self):
        bot, tg, api = self.make()
        text, markup = self.view(bot, U2)
        self.assertEqual(text, HEAD + FEATURES + f"\n\nWallet: <code>{W1}</code>\nBalance: 120,000 $CrawlScan\n"
                         "Premium: ❌ not active\nNext balance check: " + T._hm(NOW + 7200) + " (in 2h 0m)")
        self.assertEqual(markup, {"inline_keyboard": [[{"text": "Buy $CrawlScan", "url": CRAWL_TRADE},
                                                       {"text": "Unlink", "callback_data": "unlink"}]]})
        self.assertIn("axiom.trade", CRAWL_TRADE)
        self.assertIn(T.OFFICIAL_CA.lower(), CRAWL_TRADE)

    def test_paused(self):
        bot, tg, api = self.make()
        text, markup = self.view(bot, U3)
        self.assertIn("Premium: ⏸ paused: your watchlist stays as it is until " + T._day(NOW + 3 * 86400), text)
        self.assertEqual(buttons(markup), [["Buy $CrawlScan", "Unlink"]])

    def test_premium(self):
        bot, tg, api = self.make()
        text, markup = self.view(bot, U1)
        self.assertEqual(text, HEAD + FEATURES + f"\n\nWallet: <code>{W1}</code>\nBalance: 1,234,567 $CrawlScan\n"
                         "Premium: ✅ active\nNext balance check: " + T._hm(NOW + 3600) + " (in 1h 0m)")
        self.assertEqual(markup, {"inline_keyboard": [
            [{"text": "Pick tokens", "callback_data": "pick"}, {"text": "Daily digest: on", "callback_data": "digest:off"}],
            [{"text": "Unlink", "callback_data": "unlink"}]]})
        api.digest_on = False
        self.assertEqual(buttons(self.view(bot, U1)[1])[0], ["Pick tokens", "Daily digest: off"])

    def test_minimum_from_site(self):
        st = {"linked": False, "min_tokens": 1_000_000}
        self.assertIn("Hold 1,000,000+ $CrawlScan in a linked wallet", T.premium_view(st, NOW)[0])

    def test_features_off_are_not_listed(self):
        bot, tg, api = self.make(feats=())
        text, markup = self.view(bot, U1)
        for line in ("/picktokens", "/digest", "Priority"):
            self.assertNotIn(line, text)
        self.assertIn("/verify — link your wallet", text)
        self.assertIn("/unlink — unlink your wallet", text)
        self.assertEqual(buttons(markup), [["Unlink"]])

    # --- кнопки премиума ----------------------------------------------------------------------------

    def test_digest_toggle_updates_the_message(self):
        bot, tg, api = self.make()
        bot.handle_update(callback("digest:off"))
        self.assertFalse(api.digest_on)
        self.assertTrue(tg.texts[-1].startswith("⭐ <b>Premium features"))                  # тот же вид, обновлён
        self.assertEqual(buttons(tg.markups[-1])[0], ["Pick tokens", "Daily digest: off"])
        bot.handle_update(callback("digest:on"))
        self.assertTrue(api.digest_on)
        self.assertEqual(api.digest_calls, [False, True])

    def test_unlink_asks_first(self):
        bot, tg, api = self.make()
        bot.handle_update(callback("unlink"))
        self.assertEqual(api.unlinked, [])                                                  # одно нажатие не отвязывает
        self.assertIn(f"Unlink <code>{W1}</code>?", tg.texts[-1])
        self.assertEqual(buttons(tg.markups[-1]), [["Yes, unlink", "Cancel"]])
        bot.handle_update(callback("premium:cancel"))
        self.assertEqual(api.unlinked, [])
        self.assertTrue(tg.texts[-1].startswith("⭐ <b>Premium features"))
        bot.handle_update(callback("unlink:yes"))
        self.assertEqual(api.unlinked, [U1])
        self.assertIn(f"🔓 Wallet <code>{W1}</code> unlinked, Premium is off.", tg.texts[-1])
        bot.handle_update(callback("unlink", user=999))
        self.assertEqual(tg.texts[-1], T.NO_WALLET)

    def test_picktokens_command_and_old_name(self):
        bot, tg, api = self.make()
        for how in (private("/picktokens"), private("/import"), callback("pick"), callback("imp")):
            bot.handle_update(how)
            self.assertIn("<b>Pick tokens</b> from", tg.texts[-1])
        bot.handle_update(private("/picktokens", user=U2))
        self.assertEqual(tg.texts[-1], "Pick tokens is a Premium feature. /premium shows your status.")

    def test_picktokens_off_is_hint(self):
        bot, tg, api = self.make(feats=("premium_digest",))
        bot.handle_update(private("/picktokens"))
        self.assertEqual(tg.texts[-1], T.HINT)
        bot, tg, api = self.make(on=False)
        for cmd in ("/picktokens", "/premium"):
            bot.handle_update(private(cmd))
            self.assertEqual(tg.texts[-1], T.HINT)

    def test_watchlist_button_renamed(self):
        bot, tg, api = self.make()
        bot.handle_update(private("/watchlist"))
        self.assertEqual(buttons(tg.markups[-1])[-1], ["Pick tokens"])
        self.assertNotIn("Import from wallet", str(tg.markups[-1]))

    def test_commands_menu(self):
        bot, tg, api = self.make()
        sent = []
        bot.call = lambda method, upload=None, **p: sent.append((method, p))
        bot.start_workers = lambda: None
        bot.tg.call = lambda method, **p: (_ for _ in ()).throw(KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            bot.run()
        cmds = [c["command"] for c in dict(sent)["setMyCommands"]["commands"]]
        self.assertEqual(cmds[-2:], ["picktokens", "digest"])
        self.assertNotIn("import", cmds)


if __name__ == "__main__":
    unittest.main()
