"""GeckoTerminal: шапка токена (цена, капа, ликвидность, объём) и свечи для чарта. Read-only, ошибки не фатальны.
На HTTP 429 повторяем с нарастающей паузой. Шапка — один запрос /tokens/{token}:
бесплатный тариф GT быстро отвечает 429, а в этом ответе уже есть всё для шапки.
Тот же ответ (с include=top_pools) даёт возраст пулов и FDV для проверки «too established» до скана.
Чарт (fetch_chart) в скан не входит: его отдаёт отдельный /api/chart.
Удачные ответы GT кэшируются по пути запроса на GT_CACHE_TTL (общий кэш для проверки перед сканом,
шапки и чарта); вердикт «too established» — на ESTABLISHED_TTL (established_get / established_put).
Каждый сбой GT пишется в лог одной строкой: путь, исход попыток (http-код / timeout), время."""
import json, socket, threading, time, urllib.request, urllib.error
from datetime import datetime

GT = "https://api.geckoterminal.com/api/v2/networks"
GT_BUDGET = 3.0   # секунд на шапку целиком (все попытки): дольше скан не ждёт
CHART_BUDGET = 4.0  # секунд на чарт целиком (пулы + свечи + история кривой)
CHART_LIMIT = 1000  # свечей за запрос (максимум GT)
CURVE_DEX = "pump-fun"  # пул бондинг-кривой pump.fun: после миграции его свечи — начало истории
# таймфрейм по возрасту токена: (возраст до, часов; GT timeframe; aggregate; подпись)
TIMEFRAMES = ((2, "minute", 1, "1m"), (6, "minute", 5, "5m"), (48, "minute", 15, "15m"),
              (240, "hour", 1, "1h"), (None, "hour", 4, "4h"))
GT_CACHE_TTL = 15 * 60       # секунд: удачный ответ GT по пути запроса
GT_CACHE_MAX = 2000          # путей в кэше; старые вытесняются
ESTABLISHED_TTL = 24 * 3600  # секунд: вердикт «too established» по токену — повторный скан не идёт в GT

_CACHE, _EST, _lock = {}, {}, threading.Lock()


def clear_cache():
    with _lock:
        _CACHE.clear()
        _EST.clear()


def _key(token, network):
    return network, token.lower() if network == "robinhood" else token


def established_get(token, network="robinhood"):
    """Данные GT, по которым токен признан too established, если вердикт моложе ESTABLISHED_TTL, иначе None."""
    with _lock:
        hit = _EST.get(_key(token, network))
    return dict(hit[1]) if hit and time.time() - hit[0] < ESTABLISHED_TTL else None


def established_put(token, network, market):
    with _lock:
        _EST[_key(token, network)] = (time.time(), dict(market))
        if len(_EST) > GT_CACHE_MAX:
            for k, _ in sorted(_EST.items(), key=lambda kv: kv[1][0])[:len(_EST) - GT_CACHE_MAX]:
                del _EST[k]


def _why(e):
    """Исход неудачной попытки для лога: http-код, timeout или тип ошибки."""
    if isinstance(e, urllib.error.HTTPError):
        return f"http {e.code}"
    if isinstance(e, (TimeoutError, socket.timeout)) or isinstance(getattr(e, "reason", None), (TimeoutError, socket.timeout)):
        return "timeout"
    return type(e).__name__


def _gt(path, budget=GT_BUDGET):
    """GET к GeckoTerminal. На все попытки (включая паузы после 429) не больше budget секунд:
    шапка не должна задерживать скан. Удачный ответ кэшируется на GT_CACHE_TTL. Неудачные попытки
    пишутся в лог одной строкой (и при итоговом сбое, и при успехе после повторов)."""
    with _lock:
        hit = _CACHE.get(path)
    if hit and time.time() - hit[0] < GT_CACHE_TTL:
        return hit[1]
    t0 = time.time()
    end = t0 + budget
    last, fails = None, []

    def log(outcome):
        print(f"gt: {outcome} {path} [{', '.join(fails)}] {time.time() - t0:.1f}s", flush=True)

    while True:
        left = end - time.time()
        if left <= 0.2:
            fails.append("no time left")
            log("fail")
            raise last or TimeoutError("geckoterminal: no time left")
        req = urllib.request.Request(GT + path, headers={"accept": "application/json", "user-agent": "rh-crawler/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=left) as r:
                data = json.loads(r.read())
            if fails:
                log("ok after")
            with _lock:
                _CACHE[path] = (time.time(), data)
                if len(_CACHE) > GT_CACHE_MAX:
                    for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:len(_CACHE) - GT_CACHE_MAX]:
                        del _CACHE[k]
            return data
        except urllib.error.HTTPError as e:
            last = e
            fails.append(_why(e))
            if e.code != 429:
                log("fail")
                raise
        except Exception as e:
            last = e
            fails.append(_why(e))
        # пауза растёт: 0.5, 1, 2, 4 с — на 429 не добиваем GT частыми повторами
        time.sleep(min(0.5 * 2 ** (len(fails) - 1), 4.0, max(0.0, end - time.time() - 0.2)))


def fetch_market(token, network="robinhood", now=None, budget=GT_BUDGET):
    """{"name", "ticker", "price_usd", "mcap_usd", "fdv_usd", "liquidity_usd", "vol24h_usd", "age_days"}
    или {} при ошибке. Один запрос /tokens/{token}?include=top_pools.
    mcap — market_cap_usd, если GT его знает, иначе fdv; liquidity — сумма пулов (total_reserve_in_usd);
    age_days — от создания самого раннего из топ-пулов (None, если GT не дал дат).
    network — сеть GT: "robinhood" | "solana" (адреса Solana регистрозависимы, их не приводим к нижнему регистру).
    budget — секунд на все попытки (движок ждёт ответ в фоне дольше, чем шапку)."""
    tok = token.lower() if network == "robinhood" else token
    try:
        d = _gt(f"/{network}/tokens/{tok}?include=top_pools", budget=budget)
    except Exception:
        return {}
    a = (d.get("data") or {}).get("attributes") or {}
    f = lambda v: float(v) if v not in (None, "") else None
    born = [_ts((p.get("attributes") or {}).get("pool_created_at")) for p in d.get("included") or []
            if p.get("type") == "pool"]
    born = [b for b in born if b]
    return {"name": a.get("name"), "ticker": a.get("symbol"), "price_usd": f(a.get("price_usd")),
            "mcap_usd": f(a.get("market_cap_usd")) or f(a.get("fdv_usd")), "fdv_usd": f(a.get("fdv_usd")),
            "liquidity_usd": f(a.get("total_reserve_in_usd")),
            "vol24h_usd": f((a.get("volume_usd") or {}).get("h24")),
            "age_days": round(((now or time.time()) - min(born)) / 86400, 1) if born else None}


def _ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def _pool(p, tok, network):
    """Пул из ответа /tokens/{tok}/pools: адрес, dex, ликвидность, время создания, цена токена в USD."""
    a, rel = p.get("attributes") or {}, p.get("relationships") or {}
    base = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
    same = (lambda x, y: x.lower() == y.lower()) if network == "robinhood" else (lambda x, y: x == y)
    side = "base" if same(base.split("_", 1)[-1], tok) else "quote"
    price = a.get(f"{side}_token_price_usd")
    return {"address": a.get("address"), "dex": ((rel.get("dex") or {}).get("data") or {}).get("id"),
            "liquidity": float(a.get("reserve_in_usd") or 0), "created": _ts(a.get("pool_created_at")),
            "price_usd": float(price) if price not in (None, "") else None}


def timeframe(age_h):
    """(GT timeframe, aggregate, подпись) по возрасту токена в часах; неизвестный возраст — 15m."""
    if age_h is None:
        return TIMEFRAMES[2][1:]
    return next(t[1:] for t in TIMEFRAMES if t[0] is None or age_h < t[0])


def _candles(network, pool, tf, tok, end):
    """[[ts, o, h, l, c, v]] от старых к новым (GT отдаёт новые первыми)."""
    left = end - time.time()
    path = (f"/{network}/pools/{pool}/ohlcv/{tf[0]}?aggregate={tf[1]}&limit={CHART_LIMIT}"
            f"&currency=usd&token={tok}")
    rows = ((_gt(path, budget=min(GT_BUDGET, left)).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted([int(r[0])] + [float(x) for x in r[1:6]] for r in rows if len(r) >= 5)


def fetch_chart(token, network="robinhood", now=None):
    """Свечи цены токена (USD) для чарта: {"pool", "dex", "timeframe", "candles", "price_usd",
    "age_h", "stitched"} или {} (GT не знает токен или не ответил). Не дольше CHART_BUDGET секунд.
    Пул — самый ликвидный из пулов токена. Таймфрейм — по возрасту (TIMEFRAMES), возраст — от
    создания самого раннего из использованных пулов. Solana: если основной пул не кривая pump.fun,
    а пул кривой есть (токен мигрировал), его свечи до первой свечи основного пула идут в начало
    (stitched). Свечи не получены — candles = [], пул и цена всё равно отдаются."""
    end = time.time() + CHART_BUDGET
    tok = token.lower() if network == "robinhood" else token
    try:
        data = _gt(f"/{network}/tokens/{tok}/pools", budget=min(GT_BUDGET, end - time.time())).get("data") or []
        pools = sorted((_pool(p, tok, network) for p in data), key=lambda p: -p["liquidity"])
    except Exception:
        return {}
    pools = [p for p in pools if p["address"]]
    if not pools:
        return {}
    main = pools[0]
    curve = None
    if network == "solana" and main["dex"] != CURVE_DEX:
        curve = next((p for p in pools if p["dex"] == CURVE_DEX), None)
    born = [p["created"] for p in (main, curve) if p and p["created"]]
    age_h = ((now or time.time()) - min(born)) / 3600 if born else None
    tf = timeframe(age_h)
    try:
        candles = _candles(network, main["address"], tf, tok, end)
    except Exception:
        candles = []
    stitched = False
    if curve and end - time.time() > 0.3:
        try:
            first = candles[0][0] if candles else float("inf")
            early = [c for c in _candles(network, curve["address"], tf, tok, end) if c[0] < first]
            stitched = bool(early)
            candles = early + candles
        except Exception:
            pass
    return {"pool": main["address"], "dex": main["dex"], "timeframe": tf[2], "candles": candles,
            "price_usd": main["price_usd"], "age_h": None if age_h is None else round(age_h, 1),
            "stitched": stitched}
