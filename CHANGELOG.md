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
- Bankr launchpad on Robinhood Chain, behind `BANKR_ENABLED` (off by default). Results show a Bankr badge, the pair (ETH or a stock token), the pool as "Bankr · Uniswap V4" and the dev vesting next to the operators ("dev vesting: 15% (0% unlocked)"). The bot adds a "Bankr · ETH pair · dev vesting 15%" line, the recently scanned feed shows a Bankr badge, and the site lists Bankr next to Pons only when it is on. Early buyers and Trade on Axiom work for Bankr tokens.
- Large Bankr tokens: the first scan reads part of the holder history while the full holder index is built in the background. Such a scan is never shown as CLEAN or OK, says "Partial scan: building the full holder history, check again in about a minute" on the site and in the bot, and the site rescans by itself once the index is ready (`GET /api/index`). The index is capped by total entries (`BANKR_INDEX_MAX_ENTRIES`), is dropped by the memory guard, is built at the background request rate and pauses while live scans run.
- Operator memory, behind `OPMEM_ENABLED` (off by default): finished scans are recorded to a separate SQLite file (`OPMEM_DB_PATH`, by default `memory.db` next to the draw database) by a background writer, at most once per token per `OPMEM_MIN_INTERVAL_H` hours. Record only: verdicts don't use it yet, and scans never wait for it. `scripts/opmem_stats.py` prints what has been collected.
- Bankr background holder index is behind its own switch `BANKR_INDEX_ENABLED` (off by default): when it's off, no index is ever built or kept. A Bankr token whose history doesn't fit in a regular scan gets TOO ACTIVE instead of a verdict: no score, "This token has too many trades for a full scan right now", and only what is exact without the full history — dev vesting (total and unlocked), price, liquidity, market cap and the chart. On the site and in the bot; TOO ACTIVE tokens are not recorded in operator memory and can't be added to the watchlist. Bankr tokens that fit in a scan get the full verdict as before. With the switch on, the index works as described below.
- Bankr holder index builds are queued: one is built at a time, up to 5 tokens wait in line (the same token is not added twice). A partial scan says where its token is ("Partial scan: full holder history queued: 2nd in line, check again in about N minutes"), or "Partial scan: full history is queued, check again later" when the line is full. The build runs only at the background request rate and only while no live scan is running: it pauses for every live scan (no new requests, a page that arrives during a scan is processed after it), reads short pages so a request in flight is short, and doesn't start near the memory limit. The time estimate is based on the measured history size and reading speed, without the pauses.
- Operator memory records Bankr tokens with launchpad `bankr`: the dev and the vesting beneficiaries get the `dev` role (with the dev's share of supply including vesting), a vesting allocation is not counted as an early buy or a snipe, and a partial scan (`partial`) doesn't block recording the full scan that follows it.
- Premium for $CrawlScan holders, behind `PREMIUM_ENABLED` (off by default). In the bot, `/verify` reserves a wallet for 5 minutes (`PREMIUM_VERIFY_MIN`) for your Telegram account; buying any amount of $CrawlScan to that wallet links it: tokens coming from the pool or a known router, or through any app or aggregator when the same transaction moves the token out of the pool with a swap (taking liquidity out of the pool is not a buy). Tokens sent from another wallet don't count. One wallet per Telegram account, first come first served. Premium is on while the linked wallet holds at least `PREMIUM_MIN_TOKENS` (500,000 by default), checked when verifying and once a day. Premium gives a watchlist of up to 10 tokens with no 7-day limit, a "⭐ Premium" line under scan results, `/premium` (status, wallet, balance, minimum, next check) and `/unlink`. When the balance drops below the minimum the watchlist stays as it is for 7 days, then goes back to 3 tokens, 7 days each (extra tokens are removed, with a message). Admins (`PREMIUM_ADMIN_ID`) can `/admin_unlink <wallet>`. Data lives in its own SQLite file (`PREMIUM_DB_PATH`, by default `premium.db` next to the draw database): Telegram ID, wallet, times, verification transaction and balance only. With the switch off, the site, the bot and alerts behave exactly as before.
- Premium menu in the bot (with `PREMIUM_ENABLED`): a "⭐ Premium features" button in `/start`; the button and `/premium` show one message with the minimum to hold, what Premium unlocks (priority scanning, a bigger watchlist), the commands (features switched off on the site are not listed) and your status (wallet, balance, Premium on or off). The bot no longer tells users when or how often the balance is checked. Buttons depend on the status: not linked — Verify wallet; linked without Premium — Buy $CrawlScan (Trade on Axiom) and Unlink; Premium — Pick tokens, Daily digest on/off and Unlink. Unlink asks to confirm first.
- Five more Premium features, each behind its own switch (off by default) and only with `PREMIUM_ENABLED`; scans, the website, alerts and the draw are unchanged. Dev history (`PREMIUM_DEVCHECK`): `/dev <token>` and a "Dev history" button under a Premium holder's verdict list the dev's other tokens we've recorded (ticker, scan date, verdict and market cap at scan) and mark the ones confirmed in Rug Replay. Memory insights (`PREMIUM_MEMORY_INSIGHTS`): after the verdict, a "Memory" block shows how many top holders were in other tokens, sniper bots (sniper in 5+ tokens in 24 hours) and linked wallets that were together in other tokens, with Rug Replay marks; hidden when there's nothing notable. Trending (`PREMIUM_TRENDING`): `/trending` and a button in the Premium menu show the 10 most scanned tokens of the last hour (cached scans count too) with the last verdict and a Scan button. Fresh scan (`PREMIUM_FRESH`): a button under the verdict rescans a token past the 10-minute cache (once per token per 2 minutes, 20 per hour). Bot scan limit per Telegram user (`PREMIUM_BOT_RATE`): new scans only, `BOT_SCAN_RATE_PER_MIN` (3) for everyone and `BOT_SCAN_RATE_PREMIUM_PER_MIN` (15) for Premium holders; the per-IP limit of the website is unchanged. These features read only the operator memory database and the server's own memory: no RPC or DexScreener requests.
- Three more Premium features, each behind its own switch (off by default) and only with `PREMIUM_ENABLED`; regular users see no change. Queue priority (`PREMIUM_PRIORITY`): when a scan slot frees up, a waiting scan from a Premium holder in the bot goes first, the one holding more $CrawlScan in the linked wallet ahead of the others; after 2 Premium scans in a row a waiting regular scan always goes next. The number of parallel scans, the memory guard and the per-IP limit are unchanged, and scans from the website are not affected. Pick tokens (`PREMIUM_IMPORT`): a "Pick tokens" button in `/premium` and in the watchlist, and `/picktokens` (`/import` also works), shows the top 5 memecoins (Pons, Flap, Bankr, $CrawlScan included) in the linked wallet by value, priced on DexScreener; add one or all, within the 10-token watchlist. TOO ESTABLISHED and TOO ACTIVE tokens are listed but can't be added. Once every 10 minutes per user. Morning digest (`PREMIUM_DIGEST`, at `PREMIUM_DIGEST_HOUR_UTC`, 8 by default): one message a day to Premium holders with a non-empty watchlist: the current verdict of each token, what changed in 24 hours (verdict, probably rug, the biggest operator selling, early buyers) from saved alert snapshots, and the 24h price change from DexScreener. No new scans. `/digest on|off`.
- Rug Replay, behind `REPLAY_ENABLED` (off by default): a public `/replay` page and `GET /api/replay` with tokens CrawlScan marked DANGER whose market cap later fell 90% or more from the moment of the scan (market cap at scan → now, hours after the scan, the verdict at scan), and the share of such tokens among DANGER and among CLEAN/OK tokens over the last 7 days once there is enough data. A background thread checks current market caps on DexScreener in batches (`REPLAY_CHECK_MIN`, `REPLAY_MIN_MCAP`, `REPLAY_MAX_CLEAN_PER_HOUR`); it makes no RPC requests and never touches scans, the bot or the draw. Uses operator memory, which now also records the market at the time of the scan (market cap, FDV, price, liquidity, source, probably rug drop, name and ticker); existing `memory.db` files are migrated in place. A "Rug Replay" link appears in the site header only when it is on; `GET /api/config` returns `replay`.

### Changed

- Background work (reward checks, alert rechecks, early buyers without a fresh scan) has its own request rate (`BACKGROUND_RPS`) and always waits for live scans.
- The RPC rate limiter no longer holds its lock while waiting.

### Fixed

- Stability under heavy traffic. When the scanner is overloaded, new scans get `503` with `Retry-After` and `{"error": "busy"}` instead of piling up, and the site and the bot say "Scanner is busy, try again in a few seconds". Cached results and joining a scan of the same token that is already running work as before. Settings: `SCAN_QUEUE_MAX` (scans waiting for a slot) and `MEMORY_SOFT_LIMIT_MB` (process memory above which new scans wait and in-memory caches are dropped).
- Lower peak memory per scan: transfer history is read and parsed page by page, and the in-memory history kept for early buyers is capped by total transfers (`SCAN_CACHE_MAX_TRANSFERS`).
- Memory guard no longer gets stuck letting one scan through at a time. Thresholds now follow the container memory limit (cgroup, with a fallback) and the defaults are 75% (`MEMORY_SOFT_LIMIT_MB`) and 90% (`MEMORY_HARD_LIMIT_MB`). After dropping caches the process returns freed memory to the system (glibc `malloc_trim`) and logs memory before and after. If memory stays above the soft threshold, scans still run up to `MAX_CONCURRENT`, and new scans are refused only above the hard threshold. The log gets one memory line per minute (memory, active scans, scans started and refused) instead of one line per scan.
- The rewards scheduler (daily draw, burns, payouts) no longer yields to live scans. Under steady scan traffic it waited before every request, and the daily draw ran late. It now has its own request rate (`CRITICAL_RPS`), and the memory guard does not block it.
- The site no longer shows stale data from the browser cache. The recently scanned feed, rewards and draw status, and burn and winner history are fetched past the browser HTTP cache, because a CDN can raise their cache lifetime to hours. A feed read that overlaps a new scan is no longer cached.
- Pons tokens with a very long history get a clear "token history too large" error (`SCAN_MAX_LOGS`) instead of exhausting memory.
- Less load from page polling: `/api/rewards/status`, `/api/draw/status`, `/api/config` and `/api/recent` are served from memory for a short time and sent with `Cache-Control: public` so Cloudflare can cache them. The page polls the reward and draw status and the recently scanned feed less often and not at all while the tab is hidden.
- Per-IP limit on new scans (`SCAN_RATE_PER_MIN`, IP from `CF-Connecting-IP`): above it `/api/scan` returns `429` with `Retry-After` and "Too many scans from your address, try again in a minute". Cached results and joining a running scan don't count. The CrawlScan bot is not limited.
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
