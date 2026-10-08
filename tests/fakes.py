"""Подставной адаптер для тестов engine и server: синтетический токен, никакой сети.

Импортировать до engine/server: ставит фиктивный CRAWLER_RPC (адаптер читает его при импорте)
и запрещает любые настоящие RPC-запросы — ch._post падает, если до него дошли.
"""
import os, sys, time
from contextlib import ExitStack
from unittest import mock

os.environ.setdefault("CRAWLER_RPC", "http://rpc.invalid")   # .env не перезапишет: load_dotenv не трогает заданные
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chains import robinhood as ch   # noqa: E402
import engine, market                # noqa: E402

TOKEN = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20              # не Pons V2: get_launch вернёт None
CURVE = "0x" + "c0" * 20
DEPLOYER = "0x" + "de" * 20
ZERO = "0x" + "0" * 40
LAUNCH_BLOCK, LAUNCH_TS = 1000, int(time.time()) - 3600   # свежий запуск: час назад
SUPPLY = 1_000_000 * 10 ** 18
N_WALLETS = 15
BUNDLE = 3                            # первые 3 кошелька купили одной транзакцией
ETH = 10 ** 18


def wallet(i):
    return "0x" + f"{i + 1:02x}" * 20


WALLETS = [wallet(i) for i in range(N_WALLETS)]


def _no_network(*a, **k):
    raise AssertionError("тест попытался сделать настоящий RPC-запрос")


def transfers():
    """Минт в кривую, затем покупки с кривой: доли убывают, первые BUNDLE — в одной транзакции."""
    out = [{"frm": ZERO, "to": CURVE, "amount": SUPPLY, "tx": "0xmint", "block": LAUNCH_BLOCK, "log_index": 0}]
    for i, w in enumerate(WALLETS):
        tx = "0xbundle" if i < BUNDLE else f"0xtx{i:02d}"
        block = LAUNCH_BLOCK + 1 if i < BUNDLE else LAUNCH_BLOCK + 10 * (i + 1)
        out.append({"frm": CURVE, "to": w, "amount": SUPPLY // 100 * (20 - i) // 10, "tx": tx,
                    "block": block, "log_index": i + 1})
    return out


def classify_entries(token, trs, wallets):
    first = {}
    for t in trs:
        if t["to"] in wallets and t["to"] not in first:
            first[t["to"]] = {"kind": "buy", "tx": t["tx"], "block": t["block"], "via": CURVE, "eth_in": ETH // 20}
    return first


def history(wallet_, before_block, token, window=None, cap=4):
    """Сколько разных монет до входа: у каждого третьего кошелька — 0 (девственный), иначе cap."""
    return 0 if WALLETS.index(wallet_) % 3 == 0 else cap


def patched(history_fn=history, launch=True):
    """Контекст: адаптер и рынок подменены, сеть запрещена, кэш истории движка и кэш GT очищены."""
    stack = ExitStack()
    engine._HIST.clear()
    market.clear_cache()
    stack.enter_context(mock.patch.dict(ch._SCAN, clear=True))     # история переводов скана (early buyers)
    stack.enter_context(mock.patch.dict(ch._LAUNCH, clear=True))
    launch_info = {"block": LAUNCH_BLOCK, "curve": CURVE, "deployer": DEPLOYER, "tx": "0xlaunch"}
    p = lambda name, **kw: stack.enter_context(mock.patch.object(ch, name, **kw))
    p("_post", side_effect=_no_network)
    p("get_launch", side_effect=lambda t: launch_info if (launch and t == TOKEN) else None)
    p("get_token_transfers", side_effect=lambda t, b: transfers())
    p("token_supply", return_value=SUPPLY)
    p("excluded_addresses", return_value={ZERO, CURVE})
    p("market_addresses", return_value={CURVE})
    p("classify_entries", side_effect=classify_entries)
    p("block_timestamps", side_effect=lambda blocks: {b: LAUNCH_TS + (b - LAUNCH_BLOCK) for b in blocks})
    p("wallet_distinct_tokens", side_effect=history_fn)
    p("is_contract", side_effect=lambda addrs: {a: False for a in addrs})
    p("token_meta", return_value={"name": "Synthetic", "symbol": "SYN"})
    stack.enter_context(mock.patch.object(market, "fetch_market", return_value={}))
    stack.enter_context(mock.patch.object(market, "native_usd", return_value=None))   # ETH/USD (Flap на кривой)
    return stack


def slow_history(delay, slow_every=2):
    """История, которая для части кошельков отвечает дольше бюджета."""
    def fn(wallet_, before_block, token, window=None, cap=4):
        if WALLETS.index(wallet_) % slow_every == 1:
            time.sleep(delay)
        return history(wallet_, before_block, token, window, cap)
    return fn
