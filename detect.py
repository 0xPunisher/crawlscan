"""Детекторы rh-crawler: чистые функции без сети, не знают, какая сеть.
На входе — переводы токена и заранее собранные адаптером данные по кошелькам,
на выходе — холдеры, сигналы, связи, стаи, операторы и скор. Правила — README.

Все доли (share) — числа 0..1 от ОБОРОТНОГО сапплая (circulating = сапплай минус
всё, что лежит в инфраструктуре: кривая, pool manager, локер, burn, ...).
Доля от общего сапплая — share_supply = share * circulating / supply. Суммы ETH — wei (int).
"""
from collections import defaultdict
from statistics import median

# --- Холдеры (README «Холдеры») ---
TOP_N = 20                 # смотрим топ-20 холдеров
DUST_SHARE = 0.001         # пыль: < 0.1% оборота не считаем

# --- Сигналы (README «Сигналы на кошелёк») ---
SHORT_HISTORY_MAX = 3      # короткая история: ≤ 3 разных монет за всю историю до входа
SNIPER_SECONDS = 30        # снайпер: вход в первые 30 с после запуска

# --- Связи и стаи (README «Связи») ---
HUB_MIN_OUT = 100          # хаб: ≥ 100 исходящих ETH-переводов, через него не склеиваем
PACK_MIN = 3               # стая: ≥ 3 кошельков в одном блоке
PACK_SPREAD = 0.30         # стая: eth_in каждого в пределах ±30% от медианы группы
PACK_WEIGHT = 0.6          # вес стаи в доле оператора (доказанный ×1.0)

# --- Скор (README «Скор») ---
MIN_HOLDERS = 10           # меньше 10 холдеров (все ненулевые, без инфраструктуры) → «рано или поздно»
WEIGHTS = {                # веса частей скора, сумма 100
    "operator": 35,        # доля сапплая у крупнейшего оператора (с учётом веса)
    "virgin": 25,          # доля девственных кошельков в топе (без деплоера)
    "transfer": 20,        # доля сапплая, полученного переводом, а не купленного
    "sniper": 10,          # сапплай снайперов (первые 30 с), ещё не проданный
    "concentration": 10,   # концентрация топ-20
}
# Шкала части: x ≤ good → полный балл, x ≥ bad → 0, между — линейно.
# Верхние границы трёх частей = стоп-правила README; остальные границы стартовые
# (в README не заданы), калибруются на реальных токенах.
SCALE = {
    "operator":      (0.05, 0.25),  # README: 35 баллов при ≤ 5% оборота, 0 при ≥ 25%
    "virgin":        (0.10, 0.80),
    "transfer":      (0.01, 0.10),
    "sniper":        (0.01, 0.15),
    "concentration": (0.25, 0.70),
}
GATE_OPERATOR = 0.30       # стоп: крупнейший оператор с учётом веса ≥ 30% оборота
GATE_VIRGIN = 0.80         # стоп: девственных ≥ 80% топа ...
GATE_VIRGIN_MIN = 5        # ... минимум 5 холдеров (без деплоера)
GATE_TRANSFER = 0.10       # стоп: полученное переводом ≥ 10% оборота
SOFT_GATE_SCORE = 59       # мягкое стоп-правило: стая или доказанный оператор из ≥ 3 кошельков
SOFT_GATE_WALLETS = 3      #   → band не лучше RISKY (score = min(score, 59))
BANDS = ((80, "CLEAN"), (60, "OK"), (40, "RISKY"))  # иначе DANGER
TOO_EARLY = "TOO_EARLY_OR_LATE"


def supply_base(transfers, supply, excluded):
    """{"supply", "circulating", "holders_total", "balances"}: circulating = сумма
    положительных балансов вне excluded (= сапплай минус инфраструктура);
    holders_total — все холдеры с ненулевым балансом без инфраструктуры (без отсечки пыли)."""
    bal = defaultdict(int)
    for t in transfers:
        bal[t["frm"]] -= t["amount"]
        bal[t["to"]] += t["amount"]
    pos = {a: v for a, v in bal.items() if v > 0 and a not in excluded}
    return {"supply": supply, "circulating": sum(pos.values()), "holders_total": len(pos), "balances": pos}


def top_holders(base, n=TOP_N):
    """[(addr, balance, share)] по убыванию баланса, share — доля оборота;
    без пыли < DUST_SHARE оборота. base — supply_base(...)."""
    circ = base["circulating"]
    rows = [(a, v, v / circ) for a, v in base["balances"].items() if circ and v / circ >= DUST_SHARE]
    rows.sort(key=lambda r: -r[1])
    return rows[:n]


def sellers(transfers, market):
    """Кошельки, которые хоть раз отдали токен на рынок (продали)."""
    return {t["frm"] for t in transfers if t["to"] in market and t["frm"] not in market}


def wallet_signals(data, launch_ts, deployer):
    """Сигналы по кошелькам. data — {wallet: {
        "kind": "buy" | "transfer" | None,  # тип первого входа (adapter.classify_entries); None — не найден
        "tx": str, "block": int,      # транзакция и блок первого входа
        "via": str,                   # от кого пришёл токен
        "eth_in": int | None,         # ETH на покупку, wei
        "entry_ts": int | None,       # unix-время блока входа; None — вход не найден (Solana)
        "distinct_tokens": int | None,  # разных монет за всю историю до входа (cap > 3); None — не успели прочитать
        "inflows": [{"from", "block", "amount"}],  # входящие ETH до входа
        "sold": bool,                 # продавал ли токен (detect.sellers)
    }}
    Возвращает {wallet: {"kind", "virgin", "short_history", "one_shot_funded",
    "sniper", "eth_in", "is_deployer", "sold", "unread", "block", "tx", "via"}}.
    virgin — 0 монет до входа; short_history — 1..3 монет (virgin в неё не входит).
    unread — историю не успели прочитать в бюджет: virgin и short_history = False."""
    out = {}
    for w, d in data.items():
        n = d["distinct_tokens"]
        out[w] = {
            "kind": d["kind"],
            "virgin": n == 0,
            "short_history": n is not None and 0 < n <= SHORT_HISTORY_MAX,
            "unread": n is None,
            "one_shot_funded": len(d["inflows"]) == 1,
            "sniper": d["entry_ts"] is not None and d["entry_ts"] - launch_ts <= SNIPER_SECONDS,
            "eth_in": d["eth_in"],
            "is_deployer": w == deployer,
            "sold": d.get("sold", False),
            "block": d["block"], "tx": d["tx"], "via": d["via"],
        }
    return out


def _star(group, kind, via):
    """Связи группы звездой от первого кошелька: для компонент связности достаточно."""
    group = sorted(group)
    return [{"a": group[0], "b": b, "kind": kind, "level": "proven", "via": via} for b in group[1:]]


def find_links(holders, transfers, signals, inflows, outgoing, contracts, excluded, packs=()):
    """Связи между холдерами: [{"a", "b", "kind", "level", "via"}].
    holders — адреса холдеров; signals — wallet_signals (нужны tx);
    inflows — {wallet: [{"from", ...}]}; outgoing — {addr: исходящих ETH-переводов}
    (для фандеров и раздатчиков); contracts — адреса с кодом; excluded — инфраструктура и рынок.
    Доказанные (level "proven"):
      same_tx     — первый вход в одной транзакции;
      distributor — получили токен от одного и того же обычного кошелька (не контракт, не хаб);
      direct      — прямой перевод токена между холдерами;
      funder      — общий фандер первой глубины: не контракт и < HUB_MIN_OUT исходящих.
    Неизвестное число исходящих у раздатчика/фандера считаем хабом: без данных не склеиваем.
    Стаи (level "pack", kind "pack") — из packs (find_packs)."""
    hs = set(holders)
    bad = set(excluded) | set(contracts)
    ok = lambda a: a not in bad and outgoing.get(a, HUB_MIN_OUT) < HUB_MIN_OUT
    links, seen = [], set()

    def add(ls):
        for l in ls:
            key = (min(l["a"], l["b"]), max(l["a"], l["b"]), l["kind"])
            if l["a"] != l["b"] and key not in seen:
                seen.add(key); links.append(l)

    by_tx = defaultdict(set)
    for w in hs:
        by_tx[signals[w]["tx"]].add(w)
    for tx, g in by_tx.items():
        if tx and len(g) > 1:  # tx None — вход не найден, это не общая транзакция
            add(_star(g, "same_tx", tx))

    by_src = defaultdict(set)
    for t in transfers:
        if t["to"] in hs and t["frm"] not in hs and ok(t["frm"]):
            by_src[t["frm"]].add(t["to"])
        if t["to"] in hs and t["frm"] in hs:
            add([{"a": t["frm"], "b": t["to"], "kind": "direct", "level": "proven", "via": t["tx"]}])
    for src, g in by_src.items():
        if len(g) > 1:
            add(_star(g, "distributor", src))

    by_funder = defaultdict(set)
    for w in hs:
        for f in inflows.get(w, []):
            if ok(f["from"]):
                by_funder[f["from"]].add(w)
    for f, g in by_funder.items():
        if len(g) > 1:
            add(_star(g, "funder", f))

    for p in packs:
        add([dict(l, level="pack") for l in _star(p["wallets"], "pack", f"block {p['block']}")])
    return links


def find_packs(signals, window=0):
    """Поведенческие стаи: [{"block", "wallets", "median_eth"}].
    ≥ PACK_MIN кошельков купили в одном окне блоков, каждый virgin, eth_in каждого
    в пределах ±PACK_SPREAD от медианы группы. Деплоер в стаю не входит.
    window — параметр сети: 0 = тот же блок (Robinhood), 2 = до 2 слотов от первого
    покупателя группы (Solana); block стаи — блок первого покупателя."""
    cand = sorted((s["block"], w) for w, s in signals.items()
                  if s["kind"] == "buy" and s["virgin"] and s["eth_in"] and not s["is_deployer"])
    by_block = {}
    start = None
    for b, w in cand:
        if start is None or b - start > window:
            start = b
        by_block.setdefault(start, []).append(w)
    packs = []
    for b, ws in sorted(by_block.items()):
        if len(ws) < PACK_MIN:
            continue
        med = median(signals[w]["eth_in"] for w in ws)
        g = sorted(w for w in ws if abs(signals[w]["eth_in"] - med) <= PACK_SPREAD * med)
        if len(g) >= PACK_MIN:
            packs.append({"block": b, "wallets": g, "median_eth": med})
    return packs


def operators(holders, links, packs=(), supply_ratio=1.0):
    """Операторы = компоненты связности (union-find) по связям и стаям.
    holders — [(addr, balance, share)]; supply_ratio = circulating / supply.
    Возвращает по убыванию взвешенной доли:
    [{"wallets", "share", "share_supply", "level", "weighted"}] (доли — от оборота); level "proven", если внутри есть
    доказанная связь, "pack" — только стая, None — одиночный кошелёк (вес 1.0)."""
    share = {a: s for a, _, s in holders}
    parent = {a: a for a in share}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x

    def union(a, b):
        if a in parent and b in parent:
            parent[find(a)] = find(b)

    for l in links:
        union(l["a"], l["b"])
    for p in packs:
        for w in p["wallets"][1:]:
            union(p["wallets"][0], w)

    groups = defaultdict(list)
    for a in share:
        groups[find(a)].append(a)
    proven = {find(l["a"]) for l in links if l["level"] == "proven" and l["a"] in parent and l["b"] in parent}

    out = []
    for r, ws in groups.items():
        lvl = None if len(ws) == 1 else "proven" if r in proven else "pack"
        s = sum(share[w] for w in ws)
        out.append({"wallets": sorted(ws, key=lambda w: -share[w]), "share": s,
                    "share_supply": s * supply_ratio, "level": lvl,
                    "weighted": s * (PACK_WEIGHT if lvl == "pack" else 1.0)})
    out.sort(key=lambda o: -o["weighted"])
    return out


def _part(name, x):
    good, bad = SCALE[name]
    k = 1.0 if x <= good else 0.0 if x >= bad else (bad - x) / (bad - good)
    return round(WEIGHTS[name] * k, 1)


def score(holders, signals, ops, base):
    """Скор 0–100 (100 = чисто): {"score", "band", "parts", "gates", "metrics", "headline"}.
    Все метрики — доли оборота. parts — баллы по пяти частям README.
    gates — сработавшие стоп-правила: жёсткие (→ DANGER) и мягкие с префиксом "soft:"
    (стая или доказанный оператор из ≥ 3 кошельков → score не выше 59, band не лучше RISKY).
    Меньше MIN_HOLDERS холдеров (base["holders_total"]) → score None, band TOO_EARLY_OR_LATE."""
    n = len(holders)
    big = ops[0] if ops else {"share": 0.0, "share_supply": 0.0, "weighted": 0.0, "wallets": []}
    headline = (f"{n} wallets → {len(ops)} operators, biggest holds "
                f"{big['share'] * 100:.1f}% of float ({big['share_supply'] * 100:.1f}% of supply)")
    if base["holders_total"] < MIN_HOLDERS:
        return {"score": None, "band": TOO_EARLY, "parts": {}, "gates": [], "metrics": {}, "headline": headline}

    share = {a: s for a, _, s in holders}
    non_dev = [a for a in share if not signals[a]["is_deployer"]]
    virgins = [a for a in non_dev if signals[a]["virgin"]]
    m = {
        "operator": big["weighted"],
        "virgin": len(virgins) / len(non_dev) if non_dev else 0.0,
        "transfer": sum(s for a, s in share.items() if signals[a]["kind"] == "transfer"),
        "sniper": sum(share[a] for a in non_dev if signals[a]["sniper"]),
        "concentration": sum(share.values()),
    }
    parts = {k: _part(k, m[k]) for k in WEIGHTS}
    gates = []
    if m["operator"] >= GATE_OPERATOR:
        gates.append(f"biggest operator holds {m['operator'] * 100:.1f}% of float (≥ {GATE_OPERATOR * 100:.0f}%)")
    if len(non_dev) >= GATE_VIRGIN_MIN and m["virgin"] >= GATE_VIRGIN:
        gates.append(f"{m['virgin'] * 100:.0f}% virgin wallets in top (≥ {GATE_VIRGIN * 100:.0f}%)")
    if m["transfer"] >= GATE_TRANSFER:
        gates.append(f"{m['transfer'] * 100:.1f}% of float received by transfer (≥ {GATE_TRANSFER * 100:.0f}%)")
    hard = bool(gates)
    total = round(sum(parts.values()))
    groups = [o for o in ops if o["level"] == "pack"
              or (o["level"] == "proven" and len(o["wallets"]) >= SOFT_GATE_WALLETS)]
    for o in groups:
        gates.append(f"soft: {'pack of' if o['level'] == 'pack' else 'proven operator of'} "
                     f"{len(o['wallets'])} wallets, {o['share'] * 100:.1f}% of float")
    if groups:
        total = min(total, SOFT_GATE_SCORE)
    band = "DANGER" if hard else next((b for lim, b in BANDS if total >= lim), "DANGER")
    return {"score": total, "band": band, "parts": parts, "gates": gates, "metrics": m, "headline": headline}
