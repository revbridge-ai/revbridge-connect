# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems.

Report privately through GitHub's **"Report a vulnerability"** button (Security tab of this
repository) or by email to **security@revbridge.ai**. Include what you found, how to reproduce
it and the version or commit you tested.

We acknowledge reports within 3 business days, keep you informed while we work on a fix, and
credit you in the release notes unless you prefer otherwise. Please give us a reasonable time to
release a fix before disclosing.

## Scope

In scope: the code in this repository and the default configuration it generates (`make env`,
`docker-compose.yml`). Out of scope: installations you do not own, denial of service through
volume alone, and findings that require access to an operator's `secrets/` directory.

## Supported versions

Security fixes go to the latest release. Upgrade with `git pull && make env && docker compose up -d --build`.

## Hardening guide

See [docs/security.md](docs/security.md).
