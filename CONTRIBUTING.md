# Contributing to RevBridge Connect

Thanks for helping. Bug reports, documentation fixes, translations and code are all welcome.

## Before you start

- For anything bigger than a small fix, open an issue first and describe the problem and the
  approach, so nobody spends a weekend on something that will not be merged.
- Security problems go through [SECURITY.md](SECURITY.md), never a public issue.

## Development setup

```bash
make env && make build && make dev     # local stack, delivering to Mailpit
make test-unit                         # unit tests
make test                              # end-to-end suites against a fresh stack
```

See [docs/development.md](docs/development.md) for the layout and the test suites.

## Pull requests

- Keep each pull request focused on one change, with a clear description of what and why.
- Add or update tests. Anything that changes SMTP headers, webhook payloads or API responses
  needs a test that pins the new behaviour, and a line in [CHANGELOG.md](CHANGELOG.md).
- New environment variables need `make catalog` and, if the code default is not right for
  Connect, an entry in `config/variables.overrides.toml`.
- Schema changes need a new numbered file in `config/db/initdb/` and upgrade notes in the
  changelog.
- Run `make test-unit` and the relevant end-to-end suite before asking for review.

## Code style

- Python: `ruff format` and `ruff check`; type hints on new code.
- TypeScript (panel): the existing ESLint/TypeScript settings; `npm run build` must pass.
- New identifiers in English. Comments may be in English or Portuguese.

## License

By contributing you agree that your contributions are licensed under the Apache License 2.0
(see [LICENSE](LICENSE)).
