"""Telegram alerts: rules. Pure functions with no network or DB.

Step A1: snapshot of a token's scan result (snapshot) and comparison of two snapshots (diff) -> list of important
changes with short English text. Snapshot storage: alerts_store.py, writing: server.record_snapshot.
Step A2: chat subscriptions to tokens (alerts_store, API /api/alerts/* in server.py, bot commands), no sending.
Step A3: notification text (message); queue and sending: alerts_notify.py.
Everything is behind the ALERTS_ENABLED switch (off by default).
"""
import html, os, time

BAND_RANK = {"DANGER": 0, "RISKY": 1, "OK": 2, "CLEAN": 3}   # verdicts with a score; others are not compared
VERDICT_MIN_MOVE = 8         # band change not involving DANGER (CLEAN↔OK, OK↔RISKY) only if the score moved by ≥ 8
OPERATOR_SOLD_MIN = 0.30     # largest operator sold ≥ 30% of their share
OPERATOR_MIN_SHARE = 0.001   # ... if they held ≥ 0.1% of supply (less is noise)
EARLY_DROP_MIN = 0.30        # early buyers' supply share dropped ≥ 30%
EARLY_MIN_SHARE = 0.005      # ... if it was ≥ 0.5% of supply
ROUND = 6                    # decimal places for shares in a snapshot
WATCH_LIMIT = 3              # tokens per chat subscription
WATCH_DAYS = 7               # subscription lifetime in days (watching again extends it)
WEBSITE = "https://crawlscan.fun"
CHAIN_NAME = {"robinhood": "Robinhood Chain", "solana": "Solana"}
BAND_ICON = {"CLEAN": "🟢", "OK": "🟡", "RISKY": "🟠", "DANGER": "🔴"}


def enabled():
    return os.environ.get("ALERTS_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _r(x):
    return round(float(x), ROUND) if isinstance(x, (int, float)) else None


def snapshot(result, early_share=None, ts=None):
    """engine.scan result -> short snapshot, or None (no token or verdict).
    early_share: supply share of the first 20 buyers now, if already known (early.py cache), else None.
    top: {wallet: supply share} of the scan's top holders: diff uses it to see how much the previous
    largest operator still holds."""
    if not isinstance(result, dict) or not result.get("token") or not result.get("band"):
        return None
    score, rug, ops = result.get("score"), result.get("rug"), result.get("operators") or []
    big = ops[0] if ops else None
    h = result.get("header") or {}
    return {
        "token": result["token"], "chain": result.get("chain") or "robinhood",
        "ticker": h.get("ticker") or h.get("name") or None,
        "ts": int(ts if ts is not None else time.time()),
        "band": result["band"], "score": int(score) if isinstance(score, (int, float)) else None,
        "rug": bool(rug), "rug_drop": _r(rug.get("drop")) if rug else None,
        "operator": {"wallets": list(big["wallets"]), "share_supply": _r(big.get("share_supply"))} if big else None,
        "top": {h["wallet"]: _r(h.get("share_supply")) for h in result.get("holders") or [] if h.get("wallet")},
        "early_share": _r(early_share),
    }


def _pct(x):
    return f"{x * 100:.1f}%"


def diff(old, new):
    """Important changes between snapshots old -> new: [{"kind", "text", ...}]. Either snapshot missing: [].
    kind: verdict_worse / verdict_better (only among DANGER/RISKY/OK/CLEAN; entering and leaving DANGER always,
    other band changes only if the score moved by ≥ VERDICT_MIN_MOVE: 80 -> 78 across a boundary is silent),
    rug_appeared / rug_gone (always),
    operator_sold (the previous largest operator's wallets hold ≥ 30% less), early_dropped
    (early buyers' share dropped by ≥ 30%; only if known in both snapshots)."""
    if not old or not new:
        return []
    out = []
    ob, nb = old.get("band"), new.get("band")
    osc, nsc = old.get("score"), new.get("score")
    moved = osc is None or nsc is None or abs(nsc - osc) >= VERDICT_MIN_MOVE
    if ob in BAND_RANK and nb in BAND_RANK and ob != nb and ("DANGER" in (ob, nb) or moved):
        worse = BAND_RANK[nb] < BAND_RANK[ob]
        sc = f" (score {osc} → {nsc})" if osc is not None and nsc is not None else ""
        out.append({"kind": "verdict_worse" if worse else "verdict_better", "from": ob, "to": nb,
                    "text": f"Verdict {'worsened' if worse else 'improved'}: {ob} → {nb}{sc}"})
    if not old.get("rug") and new.get("rug"):
        drop = new.get("rug_drop")
        out.append({"kind": "rug_appeared", "drop": drop,
                    "text": "Probably rug" + (f": −{drop * 100:.0f}% if suspicious holders sell" if drop else "")})
    elif old.get("rug") and not new.get("rug"):
        drop = old.get("rug_drop")
        out.append({"kind": "rug_gone", "text": "Probably rug is gone" + (f" (was −{drop * 100:.0f}%)" if drop else "")})
    op = old.get("operator")
    if op and new.get("top") and (op.get("share_supply") or 0) >= OPERATOR_MIN_SHARE:
        before = op["share_supply"]
        # wallet dropped out of the new scan's top: assume it no longer holds a share
        now = sum(new["top"].get(w) or 0 for w in op["wallets"])
        sold = round(1 - now / before, 4)   # rounding: exactly 30% is not lost as 0.2999…
        if sold >= OPERATOR_SOLD_MIN:
            out.append({"kind": "operator_sold", "sold": sold, "from": before, "to": round(now, ROUND),
                        "text": f"Biggest operator sold {sold * 100:.0f}% of their holdings "
                                f"({_pct(before)} → {_pct(now)} of supply)"})
    oe, ne = old.get("early_share"), new.get("early_share")
    if oe is not None and ne is not None and oe >= EARLY_MIN_SHARE:
        fell = round(1 - ne / oe, 4)
        if fell >= EARLY_DROP_MIN:
            out.append({"kind": "early_dropped", "fell": fell, "from": oe, "to": ne,
                        "text": f"Early buyers' share fell {fell * 100:.0f}% ({_pct(oe)} → {_pct(ne)} of supply)"})
    return out


def _short(addr):
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


TEST_LINE = "This is a test. Real alerts list here what changed since the last scan."


def message(snap, changes, trade_url=None, test=False):
    """Notification to a subscriber (HTML parse mode) -> (text, buttons). snap is the current snapshot, changes the diff.
    test: a trial (POST /api/alerts/test): "(test alert)" in the first line; no changes means a TEST_LINE line."""
    e = lambda x: html.escape(str(x), quote=False)
    token = snap["token"]
    name = f"${e(snap['ticker'])}" if snap.get("ticker") else e(_short(token))
    lines = [f"🔔 <b>{name}</b> · {CHAIN_NAME.get(snap.get('chain'), e(snap.get('chain') or ''))}"
             + (" (test alert)" if test else ""), ""]
    lines += [f"• {e(c['text'])}" for c in changes] or ([f"• {TEST_LINE}"] if test else [])
    band = snap.get("band") or ""
    lines.append("")
    if snap.get("score") is not None:
        lines.append(f"Now: {BAND_ICON.get(band, '⚪️')} <b>{e(band)}</b> · score {snap['score']}/100")
    else:
        lines.append(f"Now: <b>{e(band.replace('_', ' '))}</b>")
    if snap.get("rug"):
        drop = snap.get("rug_drop")
        lines.append("⚠️ probably rug" + (f" −{drop * 100:.0f}%" if drop else ""))
    row = [{"text": "Full report", "url": f"{WEBSITE}/?ca={token}"}]
    if trade_url:
        row.append({"text": "Trade on Axiom", "url": trade_url})
    return "\n".join(lines), {"inline_keyboard": [row, [{"text": "🔕 Unwatch", "callback_data": f"unwatch:{token}"}]]}


def _name(token, ticker, chain):
    e = lambda x: html.escape(str(x), quote=False)
    who = f"<b>${e(ticker)}</b>" if ticker else f"<b>{e(_short(token))}</b>"
    return f"{who} · {CHAIN_NAME.get(chain, e(chain or ''))}\n<code>{e(token)}</code>"


ESTABLISHED_UNWATCH = "This token became too established for CrawlScan, stopped watching it."


def established_message(token, ticker=None, chain=None):
    """Auto-unsubscribe: the scan returned TOO_ESTABLISHED -> (text, buttons)."""
    return f"🏛 {_name(token, ticker, chain)}\n\n{ESTABLISHED_UNWATCH}", None


def expired_message(token, ticker=None, chain=None, days=WATCH_DAYS):
    """Subscription expired -> (text, button [🔔 Watch]: the bot re-subscribes for days days)."""
    name = f"${ticker}" if ticker else _short(token)
    return (f"🔕 Stopped watching {html.escape(name, quote=False)} after {days} days. Tap Watch to renew.\n"
            f"<code>{html.escape(token, quote=False)}</code>",
            {"inline_keyboard": [[{"text": "🔔 Watch", "callback_data": f"watch:{token}"}]]})
