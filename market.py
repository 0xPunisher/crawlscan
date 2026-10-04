"""Шапка токена с GeckoTerminal (цена, капа, ликвидность, объём). Read-only, ошибки не фатальны.
На HTTP 429 повторяем с нарастающей паузой. Берём один запрос /tokens/{token}:
бесплатный тариф GT быстро отвечает 429, а в этом ответе уже есть всё для шапки."""
import json, time, urllib.request, urllib.error

GT = "https://api.geckoterminal.com/api/v2/networks/robinhood"


def _gt(path, tries=3, timeout=8):
    last = None
    for i in range(tries):
        req = urllib.request.Request(GT + path, headers={"accept": "application/json", "user-agent": "rh-crawler/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(1.2 * (i + 1))
                continue
            raise
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


def fetch_market(token):
    """{"name", "ticker", "price_usd", "mcap_usd", "liquidity_usd", "vol24h_usd"} или {} при ошибке.
    mcap — market_cap_usd, если GT его знает, иначе fdv."""
    try:
        a = _gt(f"/tokens/{token.lower()}").get("data", {}).get("attributes", {})
    except Exception:
        return {}
    f = lambda v: float(v) if v not in (None, "") else None
    return {"name": a.get("name"), "ticker": a.get("symbol"), "price_usd": f(a.get("price_usd")),
            "mcap_usd": f(a.get("market_cap_usd")) or f(a.get("fdv_usd")),
            "liquidity_usd": f(a.get("total_reserve_in_usd")),
            "vol24h_usd": f((a.get("volume_usd") or {}).get("h24"))}
