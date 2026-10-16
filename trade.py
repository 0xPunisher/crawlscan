"""The "Trade on Axiom" link: per-chain templates from env, shared by the site (/api/config) and the bot.

  TRADE_URL_ROBINHOOD  default https://axiom.trade/t/{address}/@crawlscan?chain=robinhood
  TRADE_URL_SOLANA     default https://axiom.trade/t/{address}/@crawlscan

{address} is replaced with the token address. A template that is not https:// or lacks {address} falls back to the default.
"""
import os
from urllib.parse import quote

DEFAULTS = {"robinhood": "https://axiom.trade/t/{address}/@crawlscan?chain=robinhood",
            "solana": "https://axiom.trade/t/{address}/@crawlscan"}
ENV = {"robinhood": "TRADE_URL_ROBINHOOD", "solana": "TRADE_URL_SOLANA"}


def templates():
    """{chain: template} from env, validated."""
    out = {}
    for chain, default in DEFAULTS.items():
        v = (os.environ.get(ENV[chain]) or "").strip()
        out[chain] = v if v.startswith("https://") and "{address}" in v else default
    return out


def url(chain, address, tpls=None):
    """Link to trade the token, or None if the chain is unknown."""
    tpl = (tpls or templates()).get(chain)
    return tpl.replace("{address}", quote(address, safe="")) if tpl and address else None
