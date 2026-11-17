"""
The tracking milter MUST reset the PER-MESSAGE state in envfrom().

pymilter reuses the SAME instance for the whole SMTP connection, and one
connection can carry several messages (MAIL FROM...DATA repeated), all the more
with the worker's connection reuse (SMTP_CONNECTION_REUSE). Without a reset in
envfrom, self.rcpts would ACCUMULATE across messages, and since eom() iterates
`for rcpt in self.rcpts`, it would emit one 'sent' event per accumulated
recipient: the N-th message of the connection would emit N events.
"""
import email.utils as _email_utils
import sys
import types
from unittest.mock import patch

# pymilter (the C module `Milter`) is only installed by the milter Dockerfile (it
# needs libmilter-dev). For the unit test, a minimal stub with the base class, the
# constants and the Milter.utils submodule used by milter_server is injected
# BEFORE importing it.
if "Milter" not in sys.modules:
    _stub = types.ModuleType("Milter")

    class _Base:
        def __init__(self, *a, **k):
            pass

        def setreply(self, *a, **k):
            pass

        def getsymval(self, *a, **k):
            return None

    _stub.Milter = _Base
    _stub.CONTINUE = 0
    _stub.REJECT = 1
    _stub.TEMPFAIL = 2
    _stub.ACCEPT = 3
    _stub.DISCARD = 4
    _stub.ADDRCPT = 8
    _stub.CHGHDRS = 16
    _stub.CHGFROM = 32
    _stub.factory = None
    _stub.runmilter = lambda *a, **k: None
    # Milter.utils submodule (milter_server does `from Milter.utils import parseaddr`)
    _utils = types.ModuleType("Milter.utils")
    _utils.parseaddr = _email_utils.parseaddr
    _stub.utils = _utils
    sys.modules["Milter"] = _stub
    sys.modules["Milter.utils"] = _utils

import Milter
from mail_milter.tracking_milter import milter_server
from mail_milter.tracking_milter.milter_server import TrackingMilter


def _api_patches():
    """Patches that let envfrom pass the API key validation (no real DB)."""
    return (
        patch.object(milter_server, "extract_api_key_from_smtp_username", return_value="ak_test123"),
        patch.object(milter_server, "get_api_key_by_id", return_value={"is_active": True, "domain_id": 1}),
        patch.object(milter_server, "fetch_one", return_value=None),
    )


def test_envfrom_resets_state_per_message():
    p1, p2, p3 = _api_patches()
    with p1, p2, p3:
        m = TrackingMilter()
        # Leftovers of the PREVIOUS message on the same connection (from envrcpt/header/body/eom)
        m.rcpts = ["old1@example.com", "old2@example.com"]
        m.headers = {"subject": ["old"]}
        m.message_id = "<old@mail.example.com>"
        m.queue_id = "OLDQ123"
        m.body_chunks = [b"old body"]
        m.attachments_info = ["a1"]
        m.email_size = 9999
        m.is_html = True
        m.is_multipart = True
        m.campaign_id = "camp-old"
        m.smtp_session["rcpt_to_raw"] = ["old1@example.com"]
        m.smtp_session["mail_from"] = "old-from"

        result = m.envfrom("new-sender@test.com")
        assert result == Milter.CONTINUE

        # The per-message state is back to its defaults: nothing leaked from the previous message
        assert m.rcpts == []
        assert m.headers == {}
        assert m.message_id is None
        assert m.queue_id is None
        assert m.body_chunks == []
        assert m.attachments_info == []
        assert m.email_size == 0
        assert m.is_html is False
        assert m.is_multipart is False
        assert m.campaign_id is None
        assert m.smtp_session["rcpt_to_raw"] == []
        # mail_from is cleared by the reset and set again by envfrom for the NEW sender
        # (the old value does not leak)
        assert m.smtp_session["mail_from"] != "old-from"
        # connect/helo (per connection) are NOT touched by the reset
        assert "connect" in m.smtp_session and "helo" in m.smtp_session


def test_rcpts_do_not_accumulate_across_messages():
    """2 messages on the SAME connection: the 2nd does not inherit the 1st's rcpts (eom emits 1, not 2)."""
    p1, p2, p3 = _api_patches()
    with p1, p2, p3:
        m = TrackingMilter()

        # --- Message 1 ---
        assert m.envfrom("sender1@test.com") == Milter.CONTINUE
        m.rcpts.append("rcpt1@example.com")  # simulates envrcpt for message 1
        assert m.rcpts == ["rcpt1@example.com"]  # eom would emit 1 'sent' event (correct)

        # --- Message 2 on the same connection ---
        assert m.envfrom("sender2@test.com") == Milter.CONTINUE
        m.rcpts.append("rcpt2@example.com")  # simulates envrcpt for message 2

        # Without the reset: rcpts == [rcpt1, rcpt2] -> eom would emit 2 'sent' events (1 extra).
        assert m.rcpts == ["rcpt2@example.com"], "rcpts leaked from the previous message"
