# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Planned

- Early buyers crawl: see how much supply was bundled at launch, even after bundlers exit.

## [1.3.0] - 2026-10-06

### Added

- Telegram bot [@CrawlScanBot](https://t.me/CrawlScanBot): send a token address from Robinhood Chain or Solana and get the score, verdict and key holder signals in seconds. `/scan <address>` works in groups. Runs as a separate service on the site's scan API and shares its cache, with rate limits per user.
- Telegram links on the website, in the header and footer.

## [1.1.0] - 2026-10-05

### Added

- Solana support for pump.fun tokens: automatic chain detection from the address, Solscan links for wallets and tokens, slots instead of blocks. Behind the `SOLANA_ENABLED` flag.
- Network switch "Robinhood | Solana" above the address field, with a sample token for each chain.
- Liquidity guard: thin liquidity caps the verdict at `RISKY`.

### Changed

- Operator is scored by dump impact: how far the price could fall if the largest operator sold into liquidity, instead of its share of float. The verdict headline shows the possible price move.
- The token header from GeckoTerminal no longer delays a scan.

### Fixed

- Score colors: more points now read as cleaner (green), fewer as riskier (red).
- Mobile layout: no horizontal scroll, wrapped tables and logs on narrow screens.

## [1.0.0] - 2026-10-04

First public release.

### Added

- Live crawl of the top 20 holders of any Pons V2 token on Robinhood Chain, with a verdict in seconds.
- Transaction-level trade classification: buys through third-party bot routers are read as buys.
- Holder shares measured against the real circulating float; infrastructure (curve, pool, locker, routers) is never a holder.
- Wallet signals: bought vs received, virgin wallets, history depth, snipers, deployer.
- Proven wallet links (same transaction, shared distributor, direct transfers) and behavioural packs.
- Operators as linked wallet groups, with packs weighted lower than proven links.
- Score 0–100 from five parts, hard red-flag gates and a soft cap for packs and multi-wallet operators.
- Parallel crawl under a hard time budget; slow wallets never block the verdict.
- Live event stream and the CRAWLSCAN frontend with crawlers, holder table and verdict panel.
- Token header from GeckoTerminal: name, ticker, price, market cap, liquidity, volume, age.
- Python standard library only, read-only, no keys.

[Unreleased]: https://github.com/0xPunisher/crawlscan/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/0xPunisher/crawlscan/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/0xPunisher/crawlscan/releases/tag/v1.0.0
