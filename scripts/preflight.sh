#!/usr/bin/env bash
# Checks before sending real email from this VM.
# Usage: bash scripts/preflight.sh               (reads connect.toml)
#        bash scripts/preflight.sh --before-dns  (only what does not need your DNS records yet)
# Exits with code 1 if something blocks delivery; warnings do not block.
set -uo pipefail
cd "$(dirname "$0")/.."
BEFORE_DNS=0
[ "${1:-}" = "--before-dns" ] && BEFORE_DNS=1

read_toml() { python3 -c "import tomllib,sys;d=tomllib.load(open('connect.toml','rb'));print(d$1)" 2>/dev/null; }
HOST="$(read_toml "['install']['hostname']")"
IP="$(read_toml "['install']['public_ip']")"
[ -z "$IP" ] && IP="$(curl -fsS4 --max-time 5 https://ifconfig.me 2>/dev/null || true)"
FAIL=0
UNVERIFIED=0
ok()   { echo "  ok    $*"; }
warn() { echo "  warn  $*"; }
skip() { echo "  later $*"; }
bad()  { echo "  FAIL  $*"; FAIL=1; }

echo "Connect preflight — host=${HOST} ip=${IP}"

# 1. outbound port 25 (many providers block it; without it there is no direct delivery)
if timeout 8 bash -c 'exec 3<>/dev/tcp/gmail-smtp-in.l.google.com/25' 2>/dev/null; then
  ok "outbound port 25 open (gmail-smtp-in.l.google.com:25)"
else
  bad "outbound port 25 blocked — ask the VPS provider to unblock it"
fi

# 2. FCrDNS: the IP's PTR = hostname, and the hostname's A = IP
if [ "$BEFORE_DNS" -eq 1 ]; then
  skip "PTR and A record: checked after you publish the DNS records (scripts/dns_records.py check)"
else
  PTR="$(dig +short -x "$IP" 2>/dev/null | sed 's/\.$//' | head -1)"
  A="$(dig +short A "$HOST" 2>/dev/null | head -1)"
  [ "$PTR" = "$HOST" ] && ok "PTR ${IP} -> ${PTR}" || bad "PTR ${IP} -> '${PTR}' (expected ${HOST}; set the reverse DNS in the provider's panel)"
  [ "$A" = "$IP" ] && ok "A ${HOST} -> ${A}" || bad "A ${HOST} -> '${A}' (expected ${IP})"
fi

# 3. blocklists (reverse lookup of the IP). Spamhaus refuses public resolvers (8.8.8.8, 1.1.1.1...):
#    that is about the resolver, not about your IP.
REV="$(echo "$IP" | awk -F. '{print $4"."$3"."$2"."$1}')"
for BL in zen.spamhaus.org b.barracudacentral.org bl.spamcop.net psbl.surriel.com dnsbl-1.uceprotect.net dnsbl-3.uceprotect.net; do
  R="$(dig +short A "${REV}.${BL}" 2>/dev/null | head -1)"
  if [ -z "$R" ]; then ok "not on ${BL}"
  elif [[ "$R" == 127.255.255.* ]]; then
    warn "${BL} refused the query (${R}) because this server uses a public DNS resolver. This says nothing about your IP: check it at https://check.spamhaus.org/"
    UNVERIFIED=$((UNVERIFIED+1))
  elif [[ "$BL" == dnsbl-3.uceprotect.net ]]; then warn "the provider's network is listed on ${BL} (${R}); this usually does not block Gmail/Outlook"
  else bad "IP listed on ${BL} (${R})"
  fi
done

# 4. zone records (after the bootstrap and once the DNS is published)
if [ "$BEFORE_DNS" -eq 1 ]; then
  skip "zone SPF, MX and DKIM: checked after you publish the DNS records"
  echo
  if [ "$FAIL" -eq 0 ]; then echo "Preflight (before DNS): nothing blocks delivery."; else echo "Preflight (before DNS): FAIL above blocks delivery."; fi
  [ "$UNVERIFIED" -gt 0 ] && echo "Not verified: Spamhaus (see the warning). Check the IP by hand at https://check.spamhaus.org/."
  exit $FAIL
fi
SPF="$(dig +short TXT "$HOST" 2>/dev/null | tr -d '"' | grep -i 'v=spf1' | head -1)"
[ -n "$SPF" ] && ok "zone SPF: ${SPF}" || warn "no SPF on ${HOST} (publish the records the bootstrap prints)"
MX="$(dig +short MX "$HOST" 2>/dev/null | head -1)"
[ -n "$MX" ] && ok "zone MX: ${MX}" || warn "no MX on ${HOST} (asynchronous bounces will not arrive)"
DK="$(dig +short TXT "s1._domainkey.${HOST}" 2>/dev/null | head -1)"
[ -n "$DK" ] && ok "zone DKIM s1 published" || warn "no DKIM on s1._domainkey.${HOST}"
DK2="$(dig +short TXT "s2._domainkey.${HOST}" 2>/dev/null | head -1)"
[ -n "$DK2" ] && ok "zone DKIM s2 published" || warn "no DKIM on s2._domainkey.${HOST}"

echo
if [ "$FAIL" -eq 0 ]; then echo "Preflight: nothing blocks delivery."; else echo "Preflight: FAIL above blocks delivery."; fi
[ "$UNVERIFIED" -gt 0 ] && echo "Not verified: Spamhaus (see the warning). Check the IP by hand at https://check.spamhaus.org/."

exit $FAIL
