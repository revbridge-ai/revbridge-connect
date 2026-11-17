"""The 10k/day cap only applies when the DEFAULT relay is Google's SMTP relay.

Any other default relay has no such ceiling: comparing its daily volume with a 10,000 cap
would raise a false CRITICAL `relay_cap_breach` on every run and hide real signals.
"""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "group_health.py"
spec = importlib.util.spec_from_file_location("group_health", SCRIPT)
gh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gh)


class FakeCursor:
    def __init__(self, row):
        self._row = row

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return self._row


class FakeCnx:
    def __init__(self, row):
        self._row = row

    def cursor(self, **k):
        return FakeCursor(self._row)


def test_default_relay_is_read_from_the_database():
    row = {"name": "provider-relay", "smtp_host": "smtp.provider.example"}
    assert gh._default_relay(FakeCnx(row)) == row


def test_other_default_is_not_google():
    row = {"name": "provider-relay", "smtp_host": "smtp.provider.example"}
    d = gh._default_relay(FakeCnx(row))
    assert "gmail.com" not in (d.get("smtp_host") or "")   # -> cap None, no breach


def test_google_default_is_still_google():
    row = {"name": "google", "smtp_host": "smtp-relay.gmail.com"}
    d = gh._default_relay(FakeCnx(row))
    assert "gmail.com" in (d.get("smtp_host") or "")       # -> cap applies, real breach


def test_no_default_configured():
    assert gh._default_relay(FakeCnx(None)) is None
