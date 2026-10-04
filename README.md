# CRAWLSCAN

Crawlers that catch one wallet wearing many.

CRAWLSCAN is a read-only scanner for Pons V2 memecoins on Robinhood Chain. Paste a token address, and crawlers walk the top holders on-chain and answer one question: is this a crowd, or one person behind many wallets? The verdict comes in about 25 seconds: how many real operators stand behind the top 20 holders, and how much of the float the biggest one controls.

> 20 wallets → 4 operators, biggest holds 38% of float.

## What the crawlers check

Everything starts from the token's full transfer history since launch. Infrastructure (the bonding curve, pool, locker, routers, burn address) is never counted as a holder, and all shares are measured against the real circulating float, not total supply.

For each of the top 20 holders:

- **Entry**: bought on the market or received tokens by transfer. Buys are detected per transaction, so purchases through third-party bot routers still count as buys.
- **History**: whether the wallet ever traded a token before this one (a virgin wallet), or only a handful.
- **Timing**: sniper entries in the first seconds after launch.
- **Size**: how much ETH went into the buy.
- **Deployer**: what the creator still holds and whether they sold.

## How wallets are linked

Wallets are merged into one operator when they share a hard on-chain tie:

- they got their tokens in the same transaction;
- they received tokens from the same ordinary wallet;
- they moved tokens directly between each other.

A softer, behavioural link catches packs: several fresh wallets buying in the same block with similar sizes. Packs count with a lower weight than proven links.

Exchanges, distributors and contracts are never used to link wallets: thousands of unrelated users pass through them.

## Score

A score from 0 to 100 (100 = clean), built from five parts:

1. Share of float held by the biggest operator.
2. Share of virgin wallets in the top holders.
3. Share of float received by transfer instead of bought.
4. Float still held by snipers.
5. Concentration of the top 20.

Hard red flags (one dominant operator, a top made of virgin wallets, a large bundle) force `DANGER`. A pack or a multi-wallet operator caps the verdict at `RISKY`. Tokens with too few holders get `TOO EARLY` instead of a score.

## Tech

- Python standard library only, no third-party dependencies.
- Read-only: no keys, no signing, no transactions.
- On-chain data via an Alchemy RPC endpoint for Robinhood Chain; market data from GeckoTerminal.
- Single-page frontend served by the same Python process.

## Run locally

```sh
echo "CRAWLER_RPC=https://robinhood-mainnet.g.alchemy.com/v2/<your-key>" > .env
python3 server.py
```

Open http://localhost:8000 and paste a token address, or go straight to `http://localhost:8000/?ca=0x...`.

`PORT` and `CRAWLER_RPS` (requests per second to the RPC) can be set in the environment.

Tests: `python3 -m unittest discover -s tests`.

## License

MIT, see [LICENSE](LICENSE).
