# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Planned

- Early buyers crawl: see how much supply was bundled at launch, even after bundlers exit.

## [1.5.0] - 2026-10-07

### Added

- Recently scanned feed on the homepage: the latest verdicts, click a token to open its result, copy its address.
- `/rewards` command in the Telegram bot: next burn and draw, the last winner and payout, total burned.
- Trade on Axiom button on scan results and in the Telegram bot.
- Copy the token address with one click in the result header.
- Too established state for large, older tokens (30+ days, high liquidity and market cap). These tokens skip the full scan: the result explains why holder signals don't apply, shows a token stats card and a live chart.
- Market data fallback to DexScreener when GeckoTerminal rate-limits or does not answer, with a shared market data cache. Older tokens never get a verdict worse than `RISKY` when no market data is available.
- Chart fallbacks: the last good chart is shown (with its time) while GeckoTerminal is unavailable, and an embedded live chart is used when there are no candles at all.

### Changed

- Reserve safeguard: when the liquidity reserve can't be measured, the result says "liquidity not measured" instead of showing a dump projection.

### Fixed

- Liquidity reserve for pump.fun tokens migrated to Raydium and Meteora pools: these tokens no longer get a false `DANGER` and a "-100%" projection.
- Chart errors no longer break the result page or the crawler animation.
- The token chart widget on the homepage points to the migrated pool.
- Mobile result header: long names and addresses wrap instead of scrolling sideways.

## [1.4.0] - 2026-10-06

### Added

- Price chart on every scan result, from GeckoTerminal. It loads from a separate cached endpoint (`/api/chart`), so the scan is not slowed down. The timeframe is picked by token age, and for migrated pump.fun tokens the bonding curve history is joined with the pool.
- Probably rug projection, shown only when the verdict is `DANGER`. Suspicious supply is collected from the top holders: fresh wallets, linked operators, tokens received by transfer, the launch bundle and unsold snipers, with each wallet counted once. If selling it into the current liquidity would drop the price by 40% or more, the chart shows an arrow down to that level with the reasons below it. The score and the verdict are unchanged.
- Solana launch bundle signal: wallets that bought in the launch bundle.
- Probably rug line in the Telegram bot, with the main reasons.

## [1.3.0] - 2026-10-06

### Added

- Telegram bot [@CrawlScanBot](https://t.me/CrawlScanBot): send a token address from Robinhood Chain or Solana and get the score, verdict and key holder signals in seconds. `/scan <address>` works in groups. Runs as a separate service on the site's scan API and shares its cache, with rate limits per user.
- Telegram links on the website, in the header and footer.

## [1.2.0] - 2026-10-06

### Added

- Token burn tracking: burns from the developer wallet to `0x…dEaD` are detected onchain. Next burn timer (10:00 and 22:00 UTC), last burn and total burned.
- Daily holder rewards: every day at 22:00 UTC one holder wins 10% of the last 24h creator fees, paid in $CrawlScan. The chance is proportional to the time-weighted average balance over the day, so every holder takes part. Developer wallets and infrastructure are excluded.
- Verifiable draw: the winner comes from a Robinhood Chain block hash produced after the holder list is locked, and the Verify button recomputes it in the browser. Payouts are detected onchain.

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

[Unreleased]: https://github.com/0xPunisher/crawlscan/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/0xPunisher/crawlscan/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/0xPunisher/crawlscan/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/0xPunisher/crawlscan/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/0xPunisher/crawlscan/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/0xPunisher/crawlscan/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/0xPunisher/crawlscan/releases/tag/v1.0.0
