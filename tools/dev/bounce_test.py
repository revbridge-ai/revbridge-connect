"""Local asynchronous-bounce test: delivers a DSN (5.1.1) to the MTA's MX, at the VERP address.

Runs INSIDE the `mta` container (the image's python3):
    python3 /tmp/bounce_test.py <return_path_domain> <recipient> <message_id>
e.g.: python3 /tmp/bounce_test.py rbm307876.shop.example recipient@example.net 6ecb...

Path exercised: Postfix :25 -> bounce_accept/bounce_transport -> bounce-handler pipe ->
POST events-updater /bounces/async -> suppressions (hard bounce).
"""

import smtplib
import sys

rp_domain, rcpt, message_id = sys.argv[1], sys.argv[2], sys.argv[3]
local, dom = rcpt.split("@", 1)
verp = f"bounces+{local}={dom}@{rp_domain}"
dsn = f"""From: MAILER-DAEMON@mx.{dom}
To: {verp}
Subject: Undelivered Mail Returned to Sender
MIME-Version: 1.0
Content-Type: multipart/report; report-type=delivery-status; boundary="B0UNDARY"

--B0UNDARY
Content-Type: text/plain

The mail could not be delivered to {rcpt}.

--B0UNDARY
Content-Type: message/delivery-status

Reporting-MTA: dns; mx.{dom}

Final-Recipient: rfc822; {rcpt}
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 <{rcpt}>: User unknown

--B0UNDARY
Content-Type: text/rfc822-headers

Message-ID: <{message_id}@example.com>
X-Connect-Id: {message_id}
From: contact@{rp_domain.split('.', 1)[1]}
To: {rcpt}
Subject: RevBridge Connect test

--B0UNDARY--
"""
with smtplib.SMTP("127.0.0.1", 25, timeout=20) as s:
    s.ehlo("mx." + dom)
    if s.has_extn("starttls"):  # use STARTTLS when the MX offers it, as a remote MTA would
        s.starttls()
        s.ehlo("mx." + dom)
    s.sendmail("", [verp], dsn.encode())
print("DSN delivered to", verp)
