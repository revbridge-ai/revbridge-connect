"""Local spam-complaint test: delivers an ARF report (RFC 5965) to the MTA's MX.
Runs INSIDE the `mta` container (the image's python3):
    python3 /tmp/arf_inject.py <return_path_domain> <recipient> <message_id> [feedback_type]
Path exercised: Postfix :25 (bounces+fbl@rbm...) -> bounce-handler pipe ->
POST events-updater /bounces/async -> feedback_report -> 'complaint' suppression + webhook.
"""

import smtplib
import sys

rp_domain, rcpt, message_id = sys.argv[1], sys.argv[2], sys.argv[3]
feedback = sys.argv[4] if len(sys.argv) > 4 else "abuse"
fbl = f"bounces+fbl@{rp_domain}"
local, dom = rcpt.split("@", 1)
sender_domain = rp_domain.split(".", 1)[1]
arf = f"""From: feedback@fbl.mailbox-provider.example
To: {fbl}
Subject: FW: RevBridge Connect test
MIME-Version: 1.0
Content-Type: multipart/report; report-type=feedback-report; boundary="ARFBOUNDARY"

--ARFBOUNDARY
Content-Type: text/plain

This is an email abuse report for an email message received from IP 203.0.113.25.

--ARFBOUNDARY
Content-Type: message/feedback-report

Feedback-Type: {feedback}
User-Agent: ExampleFBL/1.0
Version: 1
Original-Mail-From: <bounces+{local}={dom}@{rp_domain}>
Original-Rcpt-To: <{rcpt}>
Arrival-Date: Sat, 08 Nov 2026 01:00:00 +0000
Source-IP: 203.0.113.25
Reported-Domain: {sender_domain}

--ARFBOUNDARY
Content-Type: text/rfc822-headers

Return-Path: <bounces+{local}={dom}@{rp_domain}>
Message-ID: <{message_id}@{sender_domain}>
X-Connect-ID: {message_id}
From: Example Shop <contact@{sender_domain}>
To: {rcpt}
Subject: RevBridge Connect test

--ARFBOUNDARY--
"""
with smtplib.SMTP("127.0.0.1", 25, timeout=20) as s:
    s.ehlo("fbl.mailbox-provider.example")
    if s.has_extn("starttls"):  # use STARTTLS when the MX offers it, as a remote MTA would
        s.starttls()
        s.ehlo("fbl.mailbox-provider.example")
    s.sendmail("", [fbl], arf.encode())
print("ARF delivered to", fbl)
