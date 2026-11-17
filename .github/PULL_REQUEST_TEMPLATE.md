## What and why

## How it was tested
- [ ] `make test-unit`
- [ ] relevant end-to-end suite (`make test` or one of `tools/dev/*_test.sh`)

## Checklist
- [ ] Wire-format changes (SMTP headers, webhook payloads, API responses) have a test and a CHANGELOG entry
- [ ] New environment variables are in the catalog (`make catalog`)
- [ ] Schema changes have a new `config/db/initdb/` file and upgrade notes
