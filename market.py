"""Шапка токена с GeckoTerminal (цена, капа, ликвидность, объём). Read-only, ошибки не фатальны.
На HTTP 429 повторяем с нарастающей паузой. Берём один запрос /tokens/{token}:
бесплатный тариф GT быстро отвечает 429, а в этом ответе уже есть всё для шапки."""
import json, time, urllib.request, urllib.error

GT = "https://api.geckoterminal.com/api/v2/networks"
GT_BUDGET = 3.0   # секунд на шапку целиком (все попытки): дольше скан не ждёт


def _gt(path, budget=GT_BUDGET):
    """GET к GeckoTerminal. На все попытки (включая паузу после 429) не больше budget секунд:
    шапка не должна задерживать скан."""
    end = time.time() + budget
    last = None
    while True:
        left = end - time.time()
        if left <= 0.2:
            raise last or TimeoutError("geckoterminal: no time left")
        req = urllib.request.Request(GT + path, headers={"accept": "application/json", "user-agent": "rh-crawler/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=min(GT_BUDGET, left)) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 429:
                raise
        except Exception as e:
            last = e
        time.sleep(min(0.5, max(0.0, end - time.time() - 0.2)))


def fetch_market(token, network="robinhood"):
    """{"name", "ticker", "price_usd", "mcap_usd", "liquidity_usd", "vol24h_usd"} или {} при ошибке.
    mcap — market_cap_usd, если GT его знает, иначе fdv. network — сеть GT: "robinhood" | "solana"
    (адреса Solana регистрозависимы, их не приводим к нижнему регистру)."""
    tok = token.lower() if network == "robinhood" else token
    try:
        a = _gt(f"/{network}/tokens/{tok}").get("data", {}).get("attributes", {})
    except Exception:
        return {}
    f = lambda v: float(v) if v not in (None, "") else None
    return {"name": a.get("name"), "ticker": a.get("symbol"), "price_usd": f(a.get("price_usd")),
            "mcap_usd": f(a.get("market_cap_usd")) or f(a.get("fdv_usd")),
            "liquidity_usd": f(a.get("total_reserve_in_usd")),
            "vol24h_usd": f((a.get("volume_usd") or {}).get("h24"))}
