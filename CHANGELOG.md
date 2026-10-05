# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### In progress

- Multichain: Solana and other EVM chains.

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

[Unreleased]: https://github.com/0xPunisher/crawlscan/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/0xPunisher/crawlscan/releases/tag/v1.0.0
