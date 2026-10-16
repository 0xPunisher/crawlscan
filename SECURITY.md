# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through **GitHub Security Advisories**:
open the [Security tab](https://github.com/0xPunisher/crawlscan/security/advisories/new) of this repository and choose *Report a vulnerability*.

Please do not open public issues for security problems. Include steps to reproduce and the impact you see; we will reply as soon as we can and credit you in the fix unless you prefer otherwise.

## Scope

CRAWLSCAN is **read-only**:

- it holds no private keys and never signs or sends transactions;
- it never asks users to connect a wallet;
- the only secret is the RPC endpoint, read from the `CRAWLER_RPC` environment variable and never stored in the repository.

Relevant reports include anything that could leak the RPC endpoint, let a request run arbitrary code or read files on the server, or make the service unavailable to others.
