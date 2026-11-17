"""Relay coverage compares with the IP that SUBMISSION_HOST RESOLVES on every run.

A fixed submission IP would stop matching every domain as soon as the host's IP changed
(submission_domains=0, missing=0), which silences the missing-relay alert.
"""
import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "group_health.py"
spec = importlib.util.spec_from_file_location("group_health_submission", SCRIPT)
gh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gh)

DNS = {
    gh.SUBMISSION_HOST: "192.0.2.25",           # this installation's submission host
    "smtp.rbm111.example.com": "192.0.2.25",
    "smtp.rbm222.example.com": "192.0.2.25",
    "smtp.other.example": "203.0.113.9",      # outside this installation
}


def _setup(monkeypatch, dns, override=None):
    def fake_resolve(host):
        if host not in dns:
            raise OSError("NXDOMAIN")
        return dns[host]

    monkeypatch.setattr(gh.socket, "gethostbyname", fake_resolve)
    monkeypatch.setattr(gh, "SUBMISSION_IP_OVERRIDE", override)
    monkeypatch.setattr(gh, "_relay_bindings", lambda cnx: [
        {"sending_domain": "a.com", "smtp_host": "smtp.rbm111.example.com", "relay_name": "node-1"},
        {"sending_domain": "b.com", "smtp_host": "smtp.rbm222.example.com", "relay_name": None},
        {"sending_domain": "c.com", "smtp_host": "smtp.other.example", "relay_name": None},
    ])
    monkeypatch.setattr(gh, "_volume_24h_by_sending_domain", lambda cnx: {})
    monkeypatch.setattr(gh, "_default_relay", lambda cnx: {"name": "provider-relay", "smtp_host": "smtp.provider.example"})


def _events(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_coverage_follows_the_ip_the_name_resolves_to(monkeypatch, capsys):
    _setup(monkeypatch, DNS)
    gh._relay_coverage(None)
    cov = next(e for e in _events(capsys) if e["event"] == "relay_coverage")
    assert cov["submission_domains"] == 2                    # a.com and b.com (same IP); c.com is outside
    assert cov["missing_relay_domains"] == ["b.com"]


def test_env_override_still_applies(monkeypatch, capsys):
    _setup(monkeypatch, DNS, override="203.0.113.9")
    gh._relay_coverage(None)
    cov = next(e for e in _events(capsys) if e["event"] == "relay_coverage")
    assert cov["submission_domains"] == 1                    # only c.com
    assert cov["missing_relay_domains"] == ["c.com"]


def test_unresolvable_name_warns_instead_of_zeroing(monkeypatch, capsys):
    dns = {k: v for k, v in DNS.items() if k != gh.SUBMISSION_HOST}
    _setup(monkeypatch, dns)
    gh._relay_coverage(None)
    events = _events(capsys)
    assert [e["event"] for e in events] == ["relay_coverage_error"]
