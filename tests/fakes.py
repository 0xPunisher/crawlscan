"""Fake adapter for engine and server tests: a synthetic token, no network at all.

Import before engine/server: sets a dummy CRAWLER_RPC (the adapter reads it on import)
and forbids any real RPC requests — ch._post fails if reached.
"""
import os, sys, time
from contextlib import ExitStack
from unittest import mock

os.environ.setdefault("CRAWLER_RPC", "http://rpc.invalid")   # .env won't overwrite: load_dotenv doesn't touch already set vars
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chains import robinhood as ch   # noqa: E402
import engine, market                # noqa: E402

TOKEN = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20              # not Pons V2: get_launch returns None
CURVE = "0x" + "c0" * 20
DEPLOYER = "0x" + "de" * 20
ZERO = "0x" + "0" * 40
LAUNCH_BLOCK, LAUNCH_TS = 1000, int(time.time()) - 3600   # fresh launch: an hour ago
SUPPLY = 1_000_000 * 10 ** 18
N_WALLETS = 15
BUNDLE = 3                            # the first 3 wallets bought in one transaction
ETH = 10 ** 18


def wallet(i):
    return "0x" + f"{i + 1:02x}" * 20


WALLETS = [wallet(i) for i in range(N_WALLETS)]


def _no_network(*a, **k):
    raise AssertionError("test attempted a real RPC request")


def transfers():
    """Mint into the curve, then buys from the curve: shares decrease, the first BUNDLE in one transaction."""
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
    """How many distinct coins before entry: every third wallet has 0 (virgin), otherwise cap."""
    return 0 if WALLETS.index(wallet_) % 3 == 0 else cap


def patched(history_fn=history, launch=True):
    """Context: adapter and market mocked, network forbidden, engine history cache and GT cache cleared."""
    stack = ExitStack()
    engine._HIST.clear()
    market.clear_cache()
    stack.enter_context(mock.patch.dict(ch._SCAN, clear=True))     # scan transfer history (early buyers)
    stack.enter_context(mock.patch.dict(ch._LAUNCH, clear=True))
    srv = sys.modules.get("server")
    if srv is not None:   # server: per-IP scan limit and status cache — from a clean slate in each test
        stack.enter_context(mock.patch.dict(srv._RATE, clear=True))
        stack.enter_context(mock.patch.dict(srv._RESP, clear=True))
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
    stack.enter_context(mock.patch.object(market, "native_usd", return_value=None))   # ETH/USD (Flap on the curve)
    return stack


def slow_history(delay, slow_every=2):
    """History that responds slower than the budget for some wallets."""
    def fn(wallet_, before_block, token, window=None, cap=4):
        if WALLETS.index(wallet_) % slow_every == 1:
            time.sleep(delay)
        return history(wallet_, before_block, token, window, cap)
    return fn
