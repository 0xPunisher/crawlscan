"""Детекторы rh-crawler: чистые функции без сети, не знают, какая сеть.
На входе — переводы токена и заранее собранные адаптером данные по кошелькам,
на выходе — холдеры, сигналы, связи, стаи, операторы, скор и проекция «probably rug». Правила — README.

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
# launch_bundle: вход не позже bundle_window блоков от запуска (параметр сети: Robinhood 0, Solana 2)

# --- Связи и стаи (README «Связи») ---
HUB_MIN_OUT = 100          # хаб: ≥ 100 исходящих ETH-переводов, через него не склеиваем
PACK_MIN = 3               # стая: ≥ 3 кошельков в одном блоке
PACK_SPREAD = 0.30         # стая: eth_in каждого в пределах ±30% от медианы группы
PACK_WEIGHT = 0.6          # вес стаи в доле оператора (доказанный ×1.0)

# --- Скор (README «Скор») ---
MIN_HOLDERS = 10           # меньше 10 холдеров (все ненулевые, без инфраструктуры) → «рано или поздно»
WEIGHTS = {                # веса частей скора, сумма 100
    "operator": 35,        # падение цены, если крупнейший оператор (с учётом веса) продаст всё в ликвидность
    "virgin": 25,          # доля девственных кошельков в топе (без деплоера)
    "transfer": 20,        # доля сапплая, полученного переводом, а не купленного
    "sniper": 10,          # сапплай снайперов (первые 30 с), ещё не проданный
    "concentration": 10,   # концентрация топ-20
}
# Шкала части: x ≤ good → полный балл, x ≥ bad → 0, между — линейно.
# Верхние границы трёх частей = стоп-правила README; остальные границы стартовые
# (в README не заданы), калибруются на реальных токенах.
SCALE = {
    "operator":      (0.10, 0.60),  # dump_impact: 35 баллов при ≤ 10% падения цены, 0 при ≥ 60%
    "operator_share": (0.05, 0.25), # резерв не измерен (reserve_ok=False): по взвешенной доле оборота, как в v1
    "virgin":        (0.10, 0.80),
    "transfer":      (0.01, 0.10),
    "sniper":        (0.01, 0.15),
    "concentration": (0.25, 0.70),
}
GATE_IMPACT = 0.50         # стоп: продажа крупнейшего оператора уронит цену на ≥ 50% — если он из 2+ связанных
                           # кошельков или один, но подозрительный (деплоер, virgin, без прочитанной истории, вход
                           # переводом или не найден); независимый покупатель с историей (тонкий пул) — мягкое
                           # правило, band не хуже RISKY
LIQ_MIN_USD = 1_000        # мягкое правило: ликвидность из шапки < $1,000 → band не лучше RISKY
GATE_VIRGIN = 0.80         # стоп: девственных ≥ 80% топа ...
GATE_VIRGIN_MIN = 5        # ... минимум 5 прочитанных холдеров (без деплоера)
UNREAD_MAX = 0.50          # не прочитано больше половины топа (без деплоера) — доля девственных неизвестна:
                           # часть virgin — нейтральная половина веса, правило по virgin не применяется, пометка
GATE_TRANSFER = 0.10       # стоп: полученное переводом ≥ 10% оборота
SOFT_GATE_SCORE = 59       # мягкое стоп-правило: стая или доказанный оператор из ≥ 3 кошельков
SOFT_GATE_WALLETS = 3      #   → band не лучше RISKY (score = min(score, 59))
BANDS = ((80, "CLEAN"), (60, "OK"), (40, "RISKY"))  # иначе DANGER
TOO_EARLY = "TOO_EARLY_OR_LATE"

# --- Too established: старый и большой токен — полный скан не запускаем (README «Too established») ---
TOO_ESTABLISHED = "TOO_ESTABLISHED"
ESTABLISHED = {"min_age_days": 30.0, "min_liquidity_usd": 750_000.0, "min_mcap_usd": 10_000_000.0}
ESTABLISHED_HEADLINE = "This token is too established for CrawlScan."
LIMITED_NOTE = " — market data unavailable, older token: holder signals are limited"
PARTIAL_NOTE = " — long history, top holders partially read: holder signals are limited"   # Flap: история окнами
ESTABLISHED_TEXT = ("CrawlScan is built for fresh memecoins. On large, older tokens the top holders are mostly "
                    "exchanges and big liquidity pools: tokens reach exchange wallets by transfer, not by buying, "
                    "and liquidity is spread across many pools, so holder patterns don't mean what they mean "
                    "on a fresh launch.")

# --- Probably rug (README «Probably rug») ---
RUG_MIN_DROP = 0.40        # проекция показывается, если продажа подозрительного запаса уронит цену на ≥ 40%
RUG_ORDER = ("linked", "transfer", "virgin", "bundle")  # приоритет причин: кошелёк попадает в первую подходящую


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


def wallet_signals(data, launch_ts, deployer, launch_block=None, bundle_window=0):
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
    "sniper", "launch_bundle", "eth_in", "is_deployer", "sold", "unread", "block", "tx", "via"}}.
    virgin — 0 монет до входа; short_history — 1..3 монет (virgin в неё не входит).
    unread — историю не успели прочитать в бюджет: virgin и short_history = False.
    launch_bundle — вход в блоке запуска или не позже bundle_window блоков (слотов) после;
    launch_block None или вход не найден — False."""
    out = {}
    for w, d in data.items():
        n = d["distinct_tokens"]
        b = d["block"]
        out[w] = {
            "kind": d["kind"],
            "virgin": n == 0,
            "short_history": n is not None and 0 < n <= SHORT_HISTORY_MAX,
            "unread": n is None,
            "one_shot_funded": len(d["inflows"]) == 1,
            "sniper": d["entry_ts"] is not None and d["entry_ts"] - launch_ts <= SNIPER_SECONDS,
            "launch_bundle": launch_block is not None and b is not None and 0 <= b - launch_block <= bundle_window,
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


def dump_impact(q, reserve):
    """На сколько (0..1) упадёт цена, если продать q токенов в ликвидность x*y=k с резервом
    токенов reserve (пул после миграции или бондинг-кривая до): 1 - (R / (R + q))^2.
    Нет резерва — продать некуда: 1.0 (если есть что продавать)."""
    if q <= 0:
        return 0.0
    if not reserve or reserve <= 0:
        return 1.0
    return 1.0 - (reserve / (reserve + q)) ** 2


def _part(name, x, scale=None):
    good, bad = SCALE[scale or name]
    k = 1.0 if x <= good else 0.0 if x >= bad else (bad - x) / (bad - good)
    return round(WEIGHTS[name] * k, 1)


def too_established(market, limits=ESTABLISHED):
    """Старый и большой токен по данным GeckoTerminal: возраст ≥ min_age_days И ликвидность всех пулов
    ≥ min_liquidity_usd И капитализация ≥ min_mcap_usd. Нет любого из чисел — False (обычный скан)."""
    age, liq, mcap = (market or {}).get("age_days"), (market or {}).get("liquidity_usd"), (market or {}).get("mcap_usd")
    if age is None or liq is None or mcap is None:
        return False
    return age >= limits["min_age_days"] and liq >= limits["min_liquidity_usd"] and mcap >= limits["min_mcap_usd"]


def _impact_phrase(impact, ops):
    """Фраза о цене для headline: < 1% → "<1%"; без операторов из 2+ кошельков и < 1% — не показываем;
    резерв не измерен (None) — "liquidity not measured"."""
    if impact is None:
        return ", liquidity not measured"
    if impact < 0.01:
        return ", could move price <1% if sold" if any(len(o["wallets"]) > 1 for o in ops) else ""
    return f", could move price −{impact * 100:.0f}% if sold"


def _sold_into_pool(q, q_factor):
    """Сколько из q токенов дойдёт до ликвидности: q × q_factor (налог на продажу токенами, Flap после выпуска).
    q_factor = 1 — q без изменений (тот же int: результат Pons бит в бит прежний)."""
    return q if q_factor == 1 else q * q_factor


def score(holders, signals, ops, base, reserve, liquidity_usd=None, reserve_ok=True, limited=False, q_factor=1,
          limited_note=LIMITED_NOTE):
    """Скор 0–100 (100 = чисто): {"score", "band", "parts", "gates", "metrics", "headline"}.
    reserve — резерв токенов ликвидности (сырые единицы): баланс пула после миграции или кривой до.
    Часть "operator" и её стоп-правило — от dump_impact крупнейшего оператора (q = взвешенная доля
    оборота × оборот), metrics["impact"]; metrics["operator"] — его взвешенная доля оборота (для информации).
    Остальные метрики — доли оборота. parts — баллы по пяти частям README.
    liquidity_usd — ликвидность из шапки токена; < LIQ_MIN_USD → мягкое правило (band не лучше RISKY),
    None (неизвестна) — правило не применяется.
    reserve_ok=False — резерв не удалось определить надёжно: impact = None, стоп-правило по impact не
    применяется, часть "operator" — по взвешенной доле оборота (SCALE["operator_share"]).
    gates — сработавшие стоп-правила: жёсткие (→ DANGER) и мягкие с префиксом "soft:"
    (стая или доказанный оператор из ≥ 3 кошельков → score не выше 59, band не лучше RISKY).
    limited=True — данных рынка нет (GT не ответил), а токен старый (сигналы холдеров ограничены):
    жёсткие правила по impact и transfer не применяются, band не ниже RISKY, к headline добавляется LIMITED_NOTE.
    Меньше MIN_HOLDERS холдеров (base["holders_total"]) → score None, band TOO_EARLY_OR_LATE.
    q_factor — доля продажи, которая дойдёт до ликвидности (1 − налог на продажу, если налог берётся токенами).
    limited_note — пояснение к limited в headline (по умолчанию LIMITED_NOTE; Flap с неполным топом — PARTIAL_NOTE)."""
    n = len(holders)
    big = ops[0] if ops else {"share": 0.0, "share_supply": 0.0, "weighted": 0.0, "wallets": []}
    impact = dump_impact(_sold_into_pool(big["weighted"] * base["circulating"], q_factor), reserve) if reserve_ok else None
    headline = (f"{n} wallets → {len(ops)} operators, biggest holds "
                f"{big['share'] * 100:.1f}% of float ({big['share_supply'] * 100:.1f}% of supply)"
                + _impact_phrase(impact, ops) + (limited_note if limited else ""))
    if base["holders_total"] < MIN_HOLDERS:
        return {"score": None, "band": TOO_EARLY, "parts": {}, "gates": [], "metrics": {}, "headline": headline,
                "notes": []}

    share = {a: s for a, _, s in holders}
    non_dev = [a for a in share if not signals[a]["is_deployer"]]
    # девственные — только среди прочитанных: непрочитанный кошелёк не улика и не «чистый»
    read = [a for a in non_dev if not signals[a]["unread"]]
    unread_share = 1 - len(read) / len(non_dev) if non_dev else 0.0
    virgin_known = unread_share <= UNREAD_MAX
    virgins = [a for a in read if signals[a]["virgin"]]
    notes = [] if virgin_known else [f"virgin: {len(non_dev) - len(read)} of {len(non_dev)} top wallets unread — "
                                     f"scored neutral"]
    m = {
        "operator": big["weighted"],
        "impact": impact,
        "virgin": (len(virgins) / len(read) if read else 0.0) if virgin_known else None,
        "unread": unread_share,
        "transfer": sum(s for a, s in share.items() if signals[a]["kind"] == "transfer"),
        "sniper": sum(share[a] for a in non_dev if signals[a]["sniper"]),
        "concentration": sum(share.values()),
    }
    parts = {k: _part(k, m[k]) for k in WEIGHTS if k not in ("operator", "virgin")}
    parts["virgin"] = _part("virgin", m["virgin"]) if virgin_known else WEIGHTS["virgin"] / 2   # неизвестно — нейтрально
    parts["operator"] = (_part("operator", impact) if impact is not None
                         else _part("operator", m["operator"], "operator_share"))
    parts = {k: parts[k] for k in WEIGHTS}
    gates = []
    thin = impact is not None and impact >= GATE_IMPACT and not limited
    # один кошелёк: жёстко — только если есть улика (деплоер, вход переводом, прочитан и без истории — virgin);
    # непрочитанный кошелёк или вход не найден — не улика: мягкое правило, как у независимого покупателя
    lone = big["wallets"][0] if len(big["wallets"]) == 1 else None
    s0 = signals.get(lone) if lone else None
    suspect = bool(s0) and (s0["is_deployer"] or s0["kind"] == "transfer" or s0["virgin"])
    unknown = bool(s0) and not suspect and (s0["unread"] or s0["kind"] is None)
    independent = bool(s0) and not suspect and not unknown
    if thin and not (independent or unknown):
        gates.append(f"biggest operator could move price −{m['impact'] * 100:.0f}% if sold "
                     f"(≥ {GATE_IMPACT * 100:.0f}%; {m['operator'] * 100:.1f}% of float)")
    if virgin_known and len(read) >= GATE_VIRGIN_MIN and m["virgin"] >= GATE_VIRGIN:
        gates.append(f"{m['virgin'] * 100:.0f}% virgin wallets in top (≥ {GATE_VIRGIN * 100:.0f}%)")
    if m["transfer"] >= GATE_TRANSFER and not limited:
        gates.append(f"{m['transfer'] * 100:.1f}% of float received by transfer (≥ {GATE_TRANSFER * 100:.0f}%)")
    hard = bool(gates)
    total = round(sum(parts.values()))
    if thin and independent:   # один независимый покупатель с историей в тонком пуле: не DANGER сам по себе
        gates.append(f"soft: one holder could move price −{m['impact'] * 100:.0f}% (thin liquidity)")
    if thin and unknown:       # историю не прочитали (бюджет) или вход не найден — не улика
        gates.append(f"soft: biggest holder couldn't be fully read; could move price −{m['impact'] * 100:.0f}% "
                     f"(thin liquidity)")
    groups = [o for o in ops if o["level"] == "pack"
              or (o["level"] == "proven" and len(o["wallets"]) >= SOFT_GATE_WALLETS)]
    for o in groups:
        gates.append(f"soft: {'pack of' if o['level'] == 'pack' else 'proven operator of'} "
                     f"{len(o['wallets'])} wallets, {o['share'] * 100:.1f}% of float")
    if liquidity_usd is not None and liquidity_usd < LIQ_MIN_USD:
        gates.append(f"soft: liquidity too thin (${liquidity_usd:,.0f})")
    if any(g.startswith("soft:") for g in gates):
        total = min(total, SOFT_GATE_SCORE)
    band = "DANGER" if hard else next((b for lim, b in BANDS if total >= lim), "DANGER")
    if limited and band == "DANGER":
        band = "RISKY"   # старый токен без данных рынка: сигналы холдеров ограничены, вердикт не ниже RISKY
    return {"score": total, "band": band, "parts": parts, "gates": gates, "metrics": m, "headline": headline,
            "notes": notes}


def rug_projection(holders, signals, ops, base, reserve, band, snipers=True, reserve_ok=True, behavioral=True,
                   q_factor=1):
    """Проекция «probably rug»: куда упадёт цена, если весь подозрительный запас продадут в ликвидность.
    Только при band DANGER (скор и вердикт не меняет); иначе, без запаса или при падении
    < RUG_MIN_DROP — None.
    Запас (каждый кошелёк один раз) — кошельки топа:
      linked   — в операторе из 2+ кошельков (доказанные связи и стаи);
      transfer — первый вход переводом;
      virgin   — 0 монет до входа (unread сюда не попадает: историю не угадываем);
      bundle   — не продавал и launch_bundle, а при snipers=True ещё и снайпер (≤ SNIPER_SECONDS).
    Кошелёк относится к первой подходящей причине в порядке RUG_ORDER, поэтому доли parts
    складываются в share. drop = dump_impact(q, reserve), q — все токены запаса без весов.
    {"drop", "level_factor" (= 1 − drop), "share", "share_supply", "parts": [{"kind", "wallets",
    "share", "share_supply"}], "wallets"}; доли — от оборота, share_supply — от сапплая.
    reserve_ok=False (резерв не измерен надёжно) — None: падение без ликвидности не считаем.
    behavioral — DANGER вызван поведенческим жёстким правилом (связанный оператор, virgin, transfer); нет — None:
    одиночный кит в тонком пуле или низкий скор без сигналов — не rug. Части запаса — только поведенческие
    (linked, transfer, virgin, bundle/snipers): кит без сигналов в запас не входит.
    q_factor — как в score: до ликвидности доходит q × q_factor."""
    if band != "DANGER" or not reserve_ok or not behavioral:
        return None
    linked = {w for o in ops if len(o["wallets"]) > 1 for w in o["wallets"]}
    test = {
        "linked": lambda w, s: w in linked,
        "transfer": lambda w, s: s["kind"] == "transfer",
        "virgin": lambda w, s: s["virgin"],
        "bundle": lambda w, s: not s["sold"] and (s["launch_bundle"] or (snipers and s["sniper"])),
    }
    ratio = base["circulating"] / base["supply"] if base["supply"] else 0.0
    parts, taken, q = [], set(), 0
    for kind in RUG_ORDER:
        rows = [(w, v, sh) for w, v, sh in holders if w not in taken and test[kind](w, signals[w])]
        if not rows:
            continue
        sh = sum(r[2] for r in rows)
        parts.append({"kind": kind, "wallets": [r[0] for r in rows], "share": sh, "share_supply": sh * ratio})
        taken.update(r[0] for r in rows)
        q += sum(r[1] for r in rows)
    if not taken:
        return None
    drop = dump_impact(_sold_into_pool(q, q_factor), reserve)
    if drop < RUG_MIN_DROP:
        return None
    share = sum(p["share"] for p in parts)
    return {"drop": drop, "level_factor": 1.0 - drop, "share": share, "share_supply": share * ratio,
            "parts": parts, "wallets": [w for w, _, _ in holders if w in taken]}


# ---------------------------------------------------------------------------
# early buyers: статус первых покупателей и итог (данные — адаптер: early_buyers / early_status)
# ---------------------------------------------------------------------------
EARLY_EXIT_DUST = 0.01  # осталось меньше 1% купленного — вышел полностью
EARLY_MOVED_MIN = 0.20  # статус moved — только если на другие кошельки ушло столько купленного или больше


def early_status(bought, now, sold=0, moved=0, burned=0):
    """holding_all | added | sold_part | sold_all | moved | burned.
    Перевод меньше EARLY_MOVED_MIN купленного — мелкий: статус по остальной части (купленное минус переведённое),
    сам перевод виден только долей (moved_share_supply). Вышел (осталось ≤ EARLY_EXIT_DUST) или вышел частично —
    по тому, чего больше: продано, переведено, сожжено (поровну — продажа)."""
    big_move = moved >= bought * EARLY_MOVED_MIN
    base = bought if big_move else bought - moved
    if now > base:
        return "added"
    if now == base:
        return "holding_all"
    if big_move and moved > sold and moved >= burned:
        return "moved"
    if burned > sold:
        return "burned"
    return "sold_all" if now <= base * EARLY_EXIT_DUST else "sold_part"


def early_report(data, status, flags=None):
    """{"buyers": [...], "summary": {...}} из early_buyers (data) и early_status (status) адаптера.
    flags — {кошелёк: [флаги скана]} для кошельков, которые скан проверял (топ-20); остальным scan_flags = None.
    Доли — от сапплая. dt_s — секунды от запуска, block_offset — блоков (Solana: слотов) от запуска.
    summary.same_destination — адреса, куда перевели токены 2+ покупателя (признак одного владельца)."""
    launch, supply = data["launch"], status["supply"] or 1
    rows, dest = [], {}
    for i, b in enumerate(data["buyers"], 1):
        w = b["wallet"]
        st = status["wallets"].get(w) or {"now": 0, "sold": 0, "moved": {}, "burned": 0, "partial": True}
        moved = sum(st["moved"].values())
        for to in st["moved"]:
            dest.setdefault(to, []).append(w)
        rows.append({
            "rank": i, "wallet": w, "dev": w == launch.get("deployer"),
            "dt_s": b["ts"] - launch["ts"] if b.get("ts") is not None and launch.get("ts") is not None else None,
            "block_offset": b["block"] - launch["block"],
            "bought_share_supply": b["bought"] / supply, "now_share_supply": st["now"] / supply,
            "status": early_status(b["bought"], st["now"], st["sold"], moved, st["burned"]),
            "sold_share_supply": st["sold"] / supply, "moved_share_supply": moved / supply,
            "burned_share_supply": st["burned"] / supply,
            "moved_to": sorted(st["moved"], key=lambda a: -st["moved"][a]),
            "scan_flags": flags.get(w) if flags is not None and w in flags else None,
            "partial": st["partial"]})
    held = [r for r, b in zip(rows, data["buyers"]) if status["wallets"].get(r["wallet"], {}).get("now", 0)
            > b["bought"] * EARLY_EXIT_DUST]
    return {"buyers": rows, "summary": {
        "buyers": len(rows), "exited": len(rows) - len(held), "holding": len(held),
        "now_share_supply": sum(r["now_share_supply"] for r in rows),
        "bought_share_supply": sum(r["bought_share_supply"] for r in rows),
        "same_destination": [{"to": to, "wallets": ws} for to, ws in dest.items() if len(ws) >= 2],
        "partial": any(r["partial"] for r in rows)}}
