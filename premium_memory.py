"""Premium: история дева (PREMIUM_DEVCHECK) и память кошельков в скане (PREMIUM_MEMORY_INSIGHTS).

Только чтение memory.db (память операторов, opmem.py) и таблицы replay (Rug Replay, replay.py) в том же файле:
своё короткое соединение на запрос с PRAGMA query_only, без RPC и DexScreener. Базы нет — пустой ответ, файл не
создаётся. Запросы идут только по существующим индексам: wallets(wallet), scans(token, ts), operators(token),
replay(token — первичный ключ). Путь скана не трогается: считается отдельным запросом бота после вердикта.

История дева: дев токена — scans.deployer последнего скана токена; его другие токены — строки wallets с этим
кошельком и ролью dev (индекс по wallet); по каждому — первый скан (дата, вердикт, капа при скане), тикер и
подтверждение Rug Replay (упал на X% за N часов).

Memory: по топ-холдерам последнего результата скана (из памяти сервера; нет — из memory.db) — сколько их было в
других токенах; снайпер-боты (роль sniper в ≥ SNIPER_TOKENS разных токенах за 24 ч, считая этот скан); кластеры
(операторы из ≥ 2 кошельков), у которых ≥ 2 кошелька уже встречались вместе в другом токене, и в Rug Replay ли те
токены. Ничего примечательного — notable = False (бот блок не показывает).
"""
import json, os, sqlite3

import opmem

DEV_LIST = 15            # токенов дева в ответе (новые первыми); итог считает все
SNIPER_TOKENS = 5        # снайпер-бот: роль sniper в стольких разных токенах за 24 ч
REPEAT_MIN = 3           # холдеров из других токенов, начиная с которых это стоит показать
TOP_N = 20               # холдеров топа
ROWS_MAX = 20_000        # строк wallets на запрос (кошельки-боты бывают в тысячах токенов)
DAY = 86400


def connect(path=None):
    """Соединение только для чтения или None, если базы нет (файл не создаётся)."""
    path = path or opmem.db_path()
    if not os.path.exists(path):
        return None
    con = sqlite3.connect(path, timeout=2.0, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = 1")
    return con


def _in(n):
    return ",".join("?" * n)


def _has_role(roles, role):
    return role in (roles or "").split(",")


def replay_of(con, tokens):
    """{токен: {"drop", "hours"}} подтверждённых в Rug Replay (таблицы нет — пусто)."""
    tokens = list(tokens)
    if not tokens:
        return {}
    try:
        rows = con.execute(f"SELECT token, confirmed_drop, hours_after FROM replay WHERE token IN ({_in(len(tokens))}) "
                           "AND confirmed_ts IS NOT NULL", tokens).fetchall()
    except sqlite3.OperationalError:            # Rug Replay ещё не создавал таблицу
        return {}
    return {r["token"]: {"drop": r["confirmed_drop"], "hours": r["hours_after"]} for r in rows}


def first_scans(con, tokens):
    """{токен: первый скан {"ts", "band", "score", "mcap_usd", "chain", "ticker"}}; тикер — последний известный."""
    tokens = list(tokens)
    out = {}
    if not tokens:
        return out
    for r in con.execute(f"SELECT token, chain, ts, band, score, mcap_usd, ticker, name FROM scans "
                         f"WHERE token IN ({_in(len(tokens))}) ORDER BY ts", tokens):
        t = r["token"]
        if t not in out:
            out[t] = {"token": t, "chain": r["chain"], "ts": r["ts"], "band": r["band"], "score": r["score"],
                      "mcap_usd": r["mcap_usd"], "ticker": r["ticker"] or r["name"]}
        elif r["ticker"]:
            out[t]["ticker"] = r["ticker"]
    return out


def dev_history(con, token):
    """→ {"known": False} (токен не в памяти или без дева) | {"known": True, "dev", "ticker", "total", "in_replay",
    "tokens": [{"token", "ticker", "ts", "band", "score", "mcap_usd", "replay": {"drop", "hours"} | None}]}."""
    if con is None:
        return {"known": False}
    row = con.execute("SELECT deployer, ticker FROM scans WHERE token = ? AND deployer IS NOT NULL "
                      "ORDER BY ts DESC LIMIT 1", (token,)).fetchone()
    if not row:
        return {"known": False}
    dev = row["deployer"]
    others = sorted({r["token"] for r in con.execute(
        "SELECT token, roles FROM wallets WHERE wallet = ? AND token != ?", (dev, token)) if _has_role(r["roles"], "dev")})
    scans = first_scans(con, others)
    rep = replay_of(con, others)
    items = sorted((scans[t] | {"replay": rep.get(t)} for t in others if t in scans), key=lambda x: -x["ts"])
    return {"known": True, "dev": dev, "ticker": row["ticker"], "total": len(items),
            "in_replay": sum(1 for x in items if x["replay"]), "tokens": items[:DEV_LIST]}


def holders_from_db(con, token):
    """Топ-холдеры и кластеры последнего записанного скана токена (если в памяти сервера результата нет)."""
    r = con.execute("SELECT MAX(ts) AS ts FROM scans WHERE token = ?", (token,)).fetchone()
    if not r or r["ts"] is None:
        return [], set(), []
    ts = r["ts"]
    hs, snipers = [], set()
    for w in con.execute("SELECT wallet, roles, share_supply FROM wallets WHERE token = ? AND ts = ? "
                         "ORDER BY share_supply DESC", (token, ts)):
        if _has_role(w["roles"], "top_holder"):
            hs.append(w["wallet"])
            if _has_role(w["roles"], "sniper"):
                snipers.add(w["wallet"])
    ops = [{"wallets": json.loads(o["wallets"]), "share_supply": o["share_supply"]}
           for o in con.execute("SELECT wallets, share_supply FROM operators WHERE token = ? AND ts = ?", (token, ts))]
    return hs[:TOP_N], snipers, ops


def from_result(result):
    """Топ-холдеры, снайперы этого скана и кластеры (операторы из ≥ 2 кошельков) из готового результата скана."""
    hs = [h["wallet"] for h in (result.get("holders") or []) if h.get("wallet")][:TOP_N]
    snipers = {h["wallet"] for h in (result.get("holders") or [])
               if h.get("wallet") and (h.get("signals") or {}).get("sniper")}
    ops = [{"wallets": list(o["wallets"]), "share_supply": o.get("share_supply")}
           for o in result.get("operators") or [] if len(o.get("wallets") or []) > 1]
    return hs, snipers, ops


def insights(con, token, holders, snipers_now, operators, now):
    """→ {"notable", "holders", "repeat", "snipers": [{"wallet", "tokens"}], "clusters": [{"wallets", "share_supply",
    "tokens": [{"token", "ticker", "replay"}]}]}. holders — топ этого скана, snipers_now — снайперы в нём,
    operators — [{"wallets", "share_supply"}]. Один запрос к wallets по индексу wallet, плюс тикеры и Rug Replay."""
    out = {"notable": False, "holders": len(holders), "repeat": 0, "snipers": [], "clusters": []}
    if con is None or not holders:
        return out
    cluster_ws = {w for o in operators for w in o["wallets"]}
    ws = sorted(set(holders) | cluster_ws)
    tokens_of, sniped = {}, {}
    for r in con.execute(f"SELECT wallet, token, ts, roles FROM wallets WHERE wallet IN ({_in(len(ws))}) AND token != ? "
                         f"LIMIT {ROWS_MAX}", ws + [token]):
        tokens_of.setdefault(r["wallet"], set()).add(r["token"])
        if r["ts"] >= now - DAY and _has_role(r["roles"], "sniper"):
            sniped.setdefault(r["wallet"], set()).add(r["token"])
    out["repeat"] = sum(1 for w in holders if tokens_of.get(w))
    for w in holders:
        n = len(sniped.get(w, ())) + (1 if w in snipers_now else 0)
        if n >= SNIPER_TOKENS:
            out["snipers"].append({"wallet": w, "tokens": n})
    together = []
    for o in operators:
        seen = {}
        for w in o["wallets"]:
            for t in tokens_of.get(w, ()):
                seen[t] = seen.get(t, 0) + 1
        toks = sorted(t for t, n in seen.items() if n >= 2)
        if toks:
            together.append((o, toks))
    if together:
        all_toks = {t for _, toks in together for t in toks}
        scans, rep = first_scans(con, all_toks), replay_of(con, all_toks)
        for o, toks in together:
            out["clusters"].append({"wallets": len(o["wallets"]), "share_supply": o.get("share_supply"),
                                    "tokens": [{"token": t, "ticker": (scans.get(t) or {}).get("ticker"),
                                                "replay": rep.get(t)} for t in toks]})
    out["notable"] = out["repeat"] >= REPEAT_MIN or bool(out["snipers"]) or bool(out["clusters"])
    return out
