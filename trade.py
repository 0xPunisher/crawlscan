"""Ссылка «Trade on Axiom»: шаблоны по сети из env, общие для сайта (/api/config) и бота.

  TRADE_URL_ROBINHOOD  по умолчанию https://axiom.trade/t/{address}/@crawlscan?chain=robinhood
  TRADE_URL_SOLANA     по умолчанию https://axiom.trade/t/{address}/@crawlscan

{address} заменяется адресом токена. Шаблон не https:// или без {address} — берётся дефолт.
"""
import os
from urllib.parse import quote

DEFAULTS = {"robinhood": "https://axiom.trade/t/{address}/@crawlscan?chain=robinhood",
            "solana": "https://axiom.trade/t/{address}/@crawlscan"}
ENV = {"robinhood": "TRADE_URL_ROBINHOOD", "solana": "TRADE_URL_SOLANA"}


def templates():
    """{сеть: шаблон} из env с проверкой."""
    out = {}
    for chain, default in DEFAULTS.items():
        v = (os.environ.get(ENV[chain]) or "").strip()
        out[chain] = v if v.startswith("https://") and "{address}" in v else default
    return out


def url(chain, address, tpls=None):
    """Ссылка на торговлю токеном или None, если сеть неизвестна."""
    tpl = (tpls or templates()).get(chain)
    return tpl.replace("{address}", quote(address, safe="")) if tpl and address else None
