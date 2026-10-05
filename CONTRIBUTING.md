# Contributing

Thanks for helping improve CRAWLSCAN.

## Run locally

Requirements: Python 3.12, no third-party packages.

```sh
echo "CRAWLER_RPC=https://robinhood-mainnet.g.alchemy.com/v2/<your-key>" > .env
python3 server.py
```

Open http://localhost:8000 and paste a Pons V2 token address, or go straight to `http://localhost:8000/?ca=0x...`.

Optional environment variables: `PORT` (default 8000) and `CRAWLER_RPS` (requests per second to the RPC, default 8).

To run a scan from the terminal and watch the events:

```sh
python3 scripts/run_scan.py 0x...
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests run on a mocked chain adapter: no network and no RPC key are needed. Every push and pull request to `main` runs them in GitHub Actions.

## Project layout

| Path | Role |
|---|---|
| `chains/robinhood.py` | Chain adapter: the only code that talks to the blockchain. |
| `detect.py` | Detectors: pure functions, no network. Holders, signals, links, operators, score. |
| `engine.py` | Runs a scan step by step and emits events under a hard time budget. |
| `server.py` | HTTP server (standard library). |
| `index.html` | Frontend, **generated** from `design.html` by `scripts/build_frontend.py`. Do not edit by hand. |

## Pull requests

- Keep the standard library only and keep everything read-only.
- Add or update tests for behaviour changes; detectors are tested in `tests/test_detect.py`.
- Never commit `.env` or any RPC key.
