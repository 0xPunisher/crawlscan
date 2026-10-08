# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Optional transfer history cache between scans (`TRANSFER_CACHE_ENABLED`, off by default): a repeat scan of a token reads only the new transfers. Scan results are the same with and without the cache.
- `MAX_CONCURRENT` sets how many scans run at the same time.
- Flap launchpad on Robinhood Chain, behind `FLAP_ENABLED` (off by default). Results show a Flap badge, the bonding curve progress or Uniswap V2, the buy and sell tax, "tax goes to the dev" when the dev receives it, and a link to flap.sh. Tokens still on the curve get their name and ticker from the contract and a price from the curve times ETH/USD; the chart card says the chart appears after the token graduates. Early buyers work for Flap tokens (buys through the curve and through the pair). The bot adds a "Flap · bonding curve 26% · tax 3%/3%" line, the recently scanned feed shows a Flap badge, and the site lists Flap next to Pons only when it is on.
- `GET /api/config` returns `flap`, `GET /api/recent` returns `launchpad`.
- Early buyers who send their tokens to a known token locker (Sablier) now show "locked in Sablier" instead of "moved", on Pons and Flap tokens. Locked tokens don't count as an exit, also for early buyer alerts.

### Changed

- Background work (reward checks, alert rechecks, early buyers without a fresh scan) has its own request rate (`BACKGROUND_RPS`) and always waits for live scans.
- The RPC rate limiter no longer holds its lock while waiting.

### Fixed

- Stability under heavy traffic. When the scanner is overloaded, new scans get `503` with `Retry-After` and `{"error": "busy"}` instead of piling up, and the site and the bot say "Scanner is busy, try again in a few seconds". Cached results and joining a scan of the same token that is already running work as before. Settings: `SCAN_QUEUE_MAX` (scans waiting for a slot) and `MEMORY_SOFT_LIMIT_MB` (process memory above which new scans wait and in-memory caches are dropped).
- Lower peak memory per scan: transfer history is read and parsed page by page, and the in-memory history kept for early buyers is capped by total transfers (`SCAN_CACHE_MAX_TRANSFERS`).
- Pons tokens with a very long history get a clear "token history too large" error (`SCAN_MAX_LOGS`) instead of exhausting memory.
- SQLite "database is locked": the database uses WAL with a busy timeout and one write lock per file. A database error in the rewards or draw API returns `503` and no longer affects scans or the process.

## [1.6.0] - 2026-10-08

### Added

- Early buyers on scan results: the first 20 buyers after launch, what each of them did since (holding, added, sold part, sold everything, moved or burned the tokens), and groups of early buyers that sent tokens to the same destination wallet.
- Telegram alerts in [@CrawlScanBot](https://t.me/CrawlScanBot): a Watch button under every result, a Watchlist menu with New and Remove, and the `/watch`, `/watchlist` and `/unwatch` commands. Up to 3 tokens per person, each watched for 7 days.
- Alert notifications when the verdict moves into or out of `DANGER` (other verdict changes only when the score moves by 8+ points), a probably rug projection appears or disappears, the biggest operator sells 30%+ of their position, or early buyers exit (their share of supply drops by 30%+). Alerts come from live scans on the site and from scheduled rechecks (every 15 minutes per watched token, with a site-wide hourly cap). Anti-spam: at most one alert per token per person every 15 minutes, with later changes sent as one summary.
- Full burn and reward history on the site: "All burns" and "All winners" lists with Show more, a Verify link for every draw, and direct links [crawlscan.fun/#burns](https://crawlscan.fun/#burns) and [crawlscan.fun/#winners](https://crawlscan.fun/#winners).
- `GET /api/rewards/history?kind=burns|draws` with pages of up to 50 and a time cursor (`before`).
- Token card on the homepage: daily rewards and burns explained, with a Trade $CrawlScan button.

### Changed

- Holder rewards are paid in ETH. ETH payouts from the developer wallets are detected on the site automatically; past payouts in $CrawlScan are shown as they were.
- New Help text in the Telegram bot and a cleaner "too established" message.

### Fixed

- The page is served with `no-cache`, so visitors get the new version right after a deploy.
- `HEAD` requests are answered like `GET`, without a body.

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

[Unreleased]: https://github.com/0xPunisher/crawlscan/compare/v1.6.0...HEAD
[1.6.0]: https://github.com/0xPunisher/crawlscan/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/0xPunisher/crawlscan/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/0xPunisher/crawlscan/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/0xPunisher/crawlscan/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/0xPunisher/crawlscan/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/0xPunisher/crawlscan/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/0xPunisher/crawlscan/releases/tag/v1.0.0
