# RevBridge Connect: notes for coding agents

If the user opens this repository to try, install or deploy Connect, use the **`start-here`** skill
(`.agents/skills/start-here/SKILL.md`, the same file Claude Code reads from `.claude/skills/`).
In Codex, invoke it with `$start-here`.

It walks through two paths, one step at a time: try it on this computer with demo data, or install
it on the user's own server (names and DNS, deploy, first customer, first test email).

Ground rules, also in the skill:

- Never print secrets (API keys, panel passwords, `secrets/`, bootstrap output); keep them in files
  only the user can read.
- Ask before anything that changes a server, and never ask for DNS or cloud credentials in the
  chat: the user runs `scripts/dns_records.py apply` themselves with their own scoped token.
- Send test email only to addresses the user owns and names.

Developer reference: [docs/README.md](docs/README.md), [CONTRIBUTING.md](CONTRIBUTING.md).
Unit tests run in the app image: `bash tools/dev/run_unit_tests.sh`.
