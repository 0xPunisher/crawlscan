<div align="center">

<img src="static/favicon.svg" width="96" alt="CRAWLSCAN">

# CRAWLSCAN

**Crawlers that catch one wallet wearing many.**

Terminal access: https://crawlscan.fun/

[![live](https://img.shields.io/badge/live-crawlscan.fun-00c805?style=flat-square\&labelColor=07090b)](https://crawlscan.fun)
![chains](https://img.shields.io/badge/chains-Robinhood_%C2%B7_Solana-9fd9ff?style=flat-square\&labelColor=07090b)
![Robinhood Chain](https://img.shields.io/badge/Robinhood_Chain-4663-9fd9ff?style=flat-square\&labelColor=07090b)
![read-only](https://img.shields.io/badge/read--only-no_keys-00c805?style=flat-square\&labelColor=07090b)
![python](https://img.shields.io/badge/python-3.12-9fd9ff?style=flat-square\&labelColor=07090b)
![runtime deps](https://img.shields.io/badge/runtime_deps-0-00c805?style=flat-square\&labelColor=07090b)
[![license](https://img.shields.io/badge/license-MIT-9fd9ff?style=flat-square\&labelColor=07090b)](LICENSE)
[![tests](https://img.shields.io/github/actions/workflow/status/0xPunisher/crawlscan/tests.yml?branch=main\&style=flat-square\&labelColor=07090b\&label=tests)](https://github.com/0xPunisher/crawlscan/actions/workflows/tests.yml)

![crawlscan](banner.jpg)

</div>

## What is CRAWLSCAN?

**CRAWLSCAN is an on-chain token intelligence engine built to detect hidden wallet concentration, coordinated buying and suspicious holder behaviour in seconds.**

Instead of simply showing how many holders a token has, CRAWLSCAN tries to answer the question that actually matters:

> **How many real participants are behind those wallets?**

A token can show hundreds of holders while a surprisingly large portion of its supply is controlled by the same person, the same group, or a network of connected wallets.

CRAWLSCAN crawls the token's on-chain activity, analyses its top holders, follows transfers and trading behaviour, identifies wallet relationships, and turns the result into a simple **0-100 score**.

The goal is to make a complex on-chain investigation understandable in seconds, without requiring users to manually inspect hundreds of transactions.

![crawlscan](screen1.jpg)

### The core idea

Paste a token address. CRAWLSCAN detects the chain automatically from the address format and returns a verdict such as:

> **20 wallets -> 4 operators, biggest holds 38% of float (9% of supply), could move price -45% if sold**

What normally requires manual blockchain analysis is reduced to a few seconds.

### Supported chains

| Chain              | Launchpads                                   | Explorer links |
| ------------------ | -------------------------------------------- | -------------- |
| **Robinhood Chain** | Pons V2 (bonding curve and Uniswap V4 pools) | Robinhood explorer |
| **Solana**          | pump.fun (bonding curve, PumpSwap and Raydium after migration) | Solscan |

## Why CRAWLSCAN is different

Most token scanners answer questions like:

* How many holders does this token have?
* How much liquidity is available?
* Is the contract verified?
* What is the current price?

Those metrics are useful, but they don't tell you **who actually controls the supply**.

CRAWLSCAN looks deeper. It analyses wallets as a network rather than treating every address as an independent holder.

```text
top holders
      |
wallet behaviour + transfers + trading history
      |
wallet relationships
      |
operator clustering
      |
real concentration + price impact
      |
0-100 verdict
```

This makes it possible to distinguish between:

**20 genuinely independent holders**

and

**20 wallets that may actually represent 4 operators.**

That distinction can completely change how a token's distribution should be read.

## How the crawlers work

The crawlers start by building the token's real holder picture directly from on-chain data.

* **Robinhood Chain**: the token's full transfer history since launch is read and the holder balances are reconstructed from it.
* **Solana**: the top holders are read directly from the chain, then each holder's own history is crawled up to the moment it entered the token.

In both cases bonding curves, liquidity pools, lockers, routers and other infrastructure addresses are excluded, so the analysis focuses on actual wallets. Ownership is measured against the **real circulating float**, not against raw supply that sits locked in a curve or pool.

![crawlscan](screen4.jpg)

### What CRAWLSCAN checks

Each top holder is analysed across several behavioural dimensions.

* **Bought vs received**: did the wallet buy on the market, or receive tokens through a transfer? Buys are recognised at the transaction level, even when they go through third-party trading bots and routers.
* **Virgin wallets**: wallets that never traded a single token before entering this one.
* **History depth**: how much trading activity a wallet had before entry.
* **Snipers**: wallets entering in the first seconds after launch, and how much of their position they still hold.
* **Deployer**: what the creator still holds and how it affects concentration.

If a wallet's entry cannot be read reliably, it is marked as **unread** and left out of the signals instead of being guessed.

![crawlscan](screen2.jpg)

### How wallets are linked

Counting wallets individually is not enough. CRAWLSCAN looks for evidence that multiple addresses belong to the same operator.

**Proven links**

Observable on-chain connections:

* wallets taking part in the same buy transaction;
* tokens distributed from the same ordinary wallet;
* direct transfers between holders.

Wallets connected by proven links are merged into a single **operator**.

**Behavioural packs**

Groups of fresh wallets that:

* enter together: the same block on Robinhood Chain, neighbouring slots on Solana;
* buy near-identical amounts;
* have no trading history before the launch.

Packs are treated as **behavioural signals**, not proof of common ownership, so they carry a lower weight than proven links.

Exchanges, bridges, routers and other high-traffic addresses are never used to link wallets, so unrelated users are not glued together.

![crawlscan](screen5.jpg)

## Verdict

All signals are combined into a single score from **0 to 100**.

**100 = cleanest distribution.**

| Signal            | What it measures |
| ----------------- | ---------------- |
| **Operator**      | How far the price could fall if the largest detected operator sold everything into the liquidity |
| **Virgin**        | The share of virgin wallets among the top holders |
| **Transfer**      | How much of the float was received rather than bought |
| **Sniper**        | How much float early snipers still hold |
| **Concentration** | How much of the float sits with the top holders |

Hard rules cover cases where a weighted score alone would be misleading:

* a single operator able to crash the price forces `DANGER`;
* detected multi-wallet operators or suspicious packs cap the result at `RISKY`;
* thin liquidity caps the result at `RISKY`;
* too few holders returns `TOO EARLY`.

> **Complex on-chain investigation -> one understandable verdict.**

<div align="center">
<img src="assets/scan.png" alt="CRAWLSCAN scan result" width="74%">
<img src="assets/mobile.jpg" alt="CRAWLSCAN scan result on mobile" width="21%">
</div>

## Built for speed

Analysing wallets one by one would take minutes. CRAWLSCAN runs multiple wallet crawlers in parallel under a hard time budget, so a full verdict arrives in seconds while the interface streams the crawl live.

```text
Token
  |
Top holders
  |
Wallet history
  |
Trades & transfers
  |
Wallet relationships
  |
Operator detection
  |
Risk scoring
  |
Verdict
```

Every crawler move you see on the page is a real step of the scan, not a loading animation.

## Technical architecture

CRAWLSCAN is intentionally lightweight.

* **Python 3.12** with the **standard library only**: zero runtime dependencies.
* **One adapter per chain**: Robinhood Chain and Solana each have their own adapter that turns on-chain data into the same set of facts. The detectors and the scoring are shared and chain-agnostic.
* **Alchemy RPC**: read-only access to Robinhood Chain and Solana.
* **GeckoTerminal**: market data for the token header, with a short timeout so it never blocks a scan.
* **Transaction-level trade classification**: real buys are recognised even through bot routers and aggregators.
* **Parallel crawling** under a hard time budget.
* **Live event stream** from the engine to the page.
* **Automated tests** on every push.
* **No private keys**: CRAWLSCAN never signs transactions or touches funds.

> **Read the chain. Understand the wallets. Never touch the user's funds.**

## Where this is going

Today every scan is evaluated by a fixed, transparent set of rules: you can read every one of them in this repository.

The next step is to make the crawler learn from what actually happens to tokens:

* **Track record**: record every verdict and compare it with how the token played out afterwards, to measure which signals really predict a rug.
* **Operator memory**: remember operator clusters across launches, so the same wallets are recognised the next time they appear.
* **Calibration**: tune the weights and thresholds against that real-world data instead of intuition.

The long-term goal is to move from a scanner that reads blockchain data to an intelligence layer that recognises patterns across launches.

## Why this matters

A holder count is not the same thing as decentralisation.

A wallet address is not necessarily an independent participant.

A token with many holders is not automatically a token with a healthy distribution.

Instead of asking only:

> **"How many holders does this token have?"**

CRAWLSCAN asks:

> **"How many independent participants actually control the supply?"**

## Run locally

```sh
cp .env.example .env
# CRAWLER_RPC=https://robinhood-mainnet.g.alchemy.com/v2/<your-key>
# SOLANA_RPC=https://solana-mainnet.g.alchemy.com/v2/<your-key>
# SOLANA_ENABLED=true          # Solana scans are off unless this is true
python3 server.py
```

Open `http://localhost:8000`, or go straight to:

`http://localhost:8000/?ca=0x...` (Robinhood Chain) or `http://localhost:8000/?ca=<mint>` (Solana)

Run the tests:

```sh
python3 -m unittest discover -s tests -v
```

## Roadmap

### ✅ Shipped

* [x] **Live crawler scanner for Robinhood Chain (Pons V2)**
* [x] **Operator clustering**: proven links and behavioural packs
* [x] **Solana support (pump.fun)**: automatic chain detection, Solscan links
* [x] **Dump-impact scoring** and liquidity guard

### 🟢 In progress

* [ ] **Browser extension**
  Bring CRAWLSCAN into the places where users discover and trade tokens, so a token can be checked without leaving the page.

### 🔜 Next

* [ ] **Early buyers crawl**
  See how much supply was bundled at launch, even after the bundlers exit.

* [ ] **All-chain support**
  More EVM chains and beyond: the same methodology, adapted to each chain's infrastructure.

* [ ] **Telegram bot**
  Send a token address in Telegram and get the verdict, score and key holder signals.

* [ ] **Operator memory across launches**
  Recognise wallet clusters and behavioural patterns across multiple launches.

* [ ] **Wallet profiler**
  Paste a wallet and see its trading history, behaviour and the operators it belongs to.

### 🧠 Intelligence layer

* [ ] **Watchlists & alerts**
  Follow tokens, wallets and operators and get alerts on new concentration, coordinated buying or large operator moves.

* [ ] **Track record**
  A public log of verdicts compared with what happened to each token afterwards.

### 🌐 Expansion

* [ ] **Launch radar**
  Scan new launches automatically and surface tokens with unusual or dangerous holder behaviour.

## Vision

> **A blockchain shows you wallets. It doesn't always show you the people behind them.**

CRAWLSCAN is built to close that gap: from a fast token crawler today to a network of wallet, operator and token intelligence tomorrow.

## License

[MIT](LICENSE)
