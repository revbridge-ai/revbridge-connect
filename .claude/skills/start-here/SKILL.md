---
name: start-here
description: Set up RevBridge Connect step by step. Either try it on this computer with demo data, or install it on the user's own server (names and DNS, deploy, first customer, first test email). Use when someone opens this repository for the first time, asks how to try, install or deploy Connect, or types /start-here (Claude Code) or $start-here (Codex).
---

# Start here

Guide the user through one of two paths. Ask which one first, in one short question:

1. **Try it here**: everything runs in Docker on this computer and mail goes to a local inbox
   (Mailpit). Nothing leaves the machine. About 10 minutes.
2. **Install on a server**: a VPS that sends real email from its own IP. About 30 minutes, most
   of it the build and waiting for DNS.

Work one step at a time: run the step, check its result, explain it in a sentence, then move on.
When a check fails, stop, say what it means and how to fix it. Do not work around it.

## Ground rules

- Never show secrets in the chat: API keys, panel passwords, the contents of `secrets/` or of a
  bootstrap output. Save them to a file only the user can read (`chmod 600`) and say where it is.
- Ask before anything that changes a server: `scripts/deploy.sh`, commands over SSH that write.
- Send test email only to addresses the user owns and names. Connect is for mail people asked
  for: do not help import bought lists or raise a new domain's daily limit to "send it all now".
- The PTR is set by the user in the VPS panel. DNS records are published by the user: by hand,
  by importing the zone file, or with `scripts/dns_records.py apply`, which the USER runs with their
  own scoped token. Never ask for DNS or cloud credentials in the chat, and never put a token in
  a command you run.

## Path 1: try it here

1. Check the tools: `docker compose version` (Compose v2, Docker running), `python3 --version`
   (3.11 or newer), `make --version` and `openssl version`. Say what is missing and how to get it.
2. Build and start: `make env build dev`. The first build takes a few minutes.
3. Fill it with a demo store: `make demo`. It sends about 90 real emails through the pipeline,
   adds 40 days of history and saves the store's login and API key in
   `/tmp/connect-demo/bootstrap.json` (do not print that file).
4. Point the user to the panel at http://localhost:8088 (login printed by `make demo`) and to
   the inbox at http://localhost:8025.
5. Send one email of their own through the API with the demo key, reading it from the file:

   ```bash
   API_KEY=$(python3 -c 'import json; print(json.load(open("/tmp/connect-demo/bootstrap.json"))["api_key"])')
   curl -X POST http://localhost:8001/send -H "Authorization: Bearer $API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"personalizations":[{"to":[{"address":"you@example.org"}]}],
          "from":{"name":"Acme Store","address":"hello@acme.example"},
          "subject":"Hello from Connect",
          "content":[{"type":"text/html","value":"<p>Hi! <a href=\"https://example.org\">A tracked link</a></p>"}]}'
   ```

   Show it arriving in Mailpit (DKIM-signed, tracked link, one-click unsubscribe) and in the
   panel under *Emails*.
6. When they are done: `make clean-local` removes the containers, volumes and local images.

## Path 2: install on a server

Read `docs/installation.md` first; it is the reference for every step below. The order is built so
you never wait on the user twice: ask everything once, let the long build run while they set the
reverse DNS, then give them every DNS record in a single list.

1. **Ask everything at once**, in one message, with the defaults in brackets:
   - the VPS public IPv4, and the SSH user [root] and key [default key];
   - the domain the email is sent from (the one in `From:`, for example `acme.com`). The
     installation will live on its own subdomain of it, `mail.acme.com` [change it if they want],
     with the hostnames `mta1.`, `api.`, `t.`, `smtp.`, `bounce.`, `img.` and `panel.` under it;
   - their email: a mailbox they read. It is the panel login, the Let's Encrypt contact and where
     the DMARC reports go;
   - the customer's name [the domain].

   Do not ask about the manifest or the hostnames separately, and do not ask whether port 25 is
   open: step 2 tests it. **Never assume the sending domain**: if the answer does not name it,
   ask for it before step 3. It ends up in `From:` and in the DKIM records.
2. **Check the server.** `scripts/check-vps.sh user@ip` (add `SSH_KEY=...`). It is read-only: SSH,
   system, CPU/RAM/disk, outbound port 25, DNS resolver, free ports. Stop on `FAIL` and say what it
   means; read the warnings to the user in one sentence each.
3. **Write the manifest outside the checkout:**
   `scripts/init_manifest.py --ip IP --domain DOMAIN --email EMAIL` (add `--install-domain` to
   change the subdomain). The scripts that follow pick the newest manifest on their own, so you do
   not need `CONNECT_TOML`. Show the user the names it chose, not the whole file.

   **The installation domain is where the PTR points.** Step 2 printed the IP's current reverse
   DNS. If it is `mta1.<X>`, propose `<X>` as the installation domain (`--install-domain <X>`):
   the PTR then needs no change. Say so in the same message as the names, and do not read the
   scripts to work this out. Only if there is no usable PTR use the default `mail.<domain>`, and
   then tell the user to set the PTR to `mta1.mail.<domain>` during the build.
4. **Deploy.** Say what it will do (copy the repo, install Docker and the firewall, generate the
   secrets on the server, build and start, then create the first customer; about 10 minutes) and
   ask for **one** OK that covers the deploy and step 5. Do not ask again before step 5. Then:

   ```bash
   SSH_KEY=... scripts/deploy.sh user@ip > ~/.config/revbridge-connect/deploy.log 2>&1; echo "exit=$?" >> ~/.config/revbridge-connect/deploy.log
   ```

   Run it in the background and read the log; never pipe it into `tail` (the pipe hides the real
   exit code). The install runs **on the server**, so a dropped connection does not stop it:
   `deploy.sh` retries on its own, and if it gives up, running it again reattaches to the same
   install without copying or restarting anything. The log ends with `deploy finished: exit N`
   and then your `exit=`: only `0` is a success. If it is not `0`, read the log before saying
   anything. **Do not assume an earlier installation exists** because containers are already up:
   they are probably from this same run. Check `/var/log/connect-install.log` on the server.
   The last lines of the log are the preflight summary. A `FAIL` there (blocked port 25, IP on a blocklist)
   means sending from that IP will not work: stop. A Spamhaus warning means the server uses a
   public DNS resolver and Spamhaus refuses those: give the user
   https://check.spamhaus.org/ to look the IP up by hand.

   **While it builds**, tell the user to set the IP's reverse DNS (PTR) to `mta1.<installation
   domain>` in the VPS provider's panel; `dns_records.py plan` prints where. Only they can do it.
   Say what the PTR is **now** (step 2 printed it) and whether it already matches.
5. **First customer.** Once the deploy log says `exit 0` (and the preflight does not say `FAIL`):
   `SSH_KEY=... scripts/first_customer.sh user@ip --domain D
   --customer NAME --admin-email EMAIL`. It creates the customer, saves the API key and the panel
   password in `~/.config/revbridge-connect/bootstrap-<domain>.json` (mode 600; never print that
   file) and prints **every** DNS record in one table: it finds the zone, says who hosts it, and
   writes a zone file. **Show that output to the user whole**, with the DKIM values complete; do
   not summarize the table, and say which records are conflicts with what already exists in the
   zone (for example a `_dmarc` the domain already has). Give the user the three ways to publish it, easiest first:
   - **Cloudflare, automatic.** They create an API token and run the command themselves, so the
     token never goes through the chat. How to create it, in this order:
     1. open https://dash.cloudflare.com/profile/api-tokens and click **Create Token**;
     2. next to the template **Edit zone DNS**, click **Use template**;
     3. under *Zone Resources* choose **Include > Specific zone** and pick the zone the plan named;
     4. **Continue to summary**, then **Create Token**, and copy it.

     Then the user runs it themselves, in their own terminal (in Claude Code they can type it here
     with a `!` in front; in Codex, in a separate terminal), so the token stays out of the chat:
     first a dry run and, if the list looks right, the real one (the script finds the manifest and
     the bootstrap itself):
     `CLOUDFLARE_API_TOKEN=<token> scripts/dns_records.py apply`
     `CLOUDFLARE_API_TOKEN=<token> scripts/dns_records.py apply --apply`
     It never edits or deletes an existing record (a conflict is listed and left alone).
   - **Import the zone file** the plan wrote (Cloudflare, Route 53 and GoDaddy import it).
   - **By hand**, from the table (the DKIM values are shown whole below it).

   Never ask for DNS or cloud credentials in the chat.
6. **Go live.** After they publish: `scripts/go_live.sh user@ip` checks every record (against the
   zone's own name servers and a public resolver), starts Caddy so the certificates are issued,
   and runs the full preflight. If it says some records are not visible yet, wait a few minutes
   and run it again. The PTR must show `ok` too; if not, the provider's panel setting is missing
   or still propagating.
7. **First email.** Ask for one inbox the user owns (Gmail or Outlook) and send one message
   through `https://<endpoints.api>/send` with the key from the private file, like the `curl` in
   path 1. In that inbox, check the headers show `spf=pass`, `dkim=pass` and `dmarc=pass`, and
   follow the message in the panel.
8. **Warm-up.** Explain it before they plan a campaign:
   - a new sending domain starts at 500 emails a day and the limit grows about 30% for each
     day of clean delivery; what is over the limit waits for the next day, and the API answers
     `429` when today and tomorrow are both full;
   - the per-IP warm-up is not active yet, so the first weeks on a new IP should go to the most
     engaged recipients;
   - register the feedback-loop address from the bootstrap output (`fbl_address`) with the
     mailbox providers' complaint programs.

   Details are in `docs/deliverability.md`.

## When something fails

- `docs/operations.md` has the day-to-day commands (logs, status, restarts).
- `docker compose ps` and `docker compose logs --tail=100 <service>` (on the server: inside
  `/opt/revbridge-connect`) show what is wrong.
- Bounces and blocks are explained per message in the panel, under *Emails*.
