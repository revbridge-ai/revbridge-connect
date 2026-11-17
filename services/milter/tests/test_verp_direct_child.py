"""
The VERP envelope must be a DIRECT child of the API key's domain.

The MySQL LIKE `%` crosses dots, so `name LIKE '%.shop.example.com'` would match
`rbm533320.mail.shop.example.com`, and the PARENT domain's key would send bounces
and SPF to the CHILD's account.

Rule: only `rbm<something>.<domain>` counts, one label before the domain. Without a
direct child, the envelope is NOT rewritten (borrowing another domain's rbm would
send bounces to the wrong owner).
"""
import email.utils as _email_utils
import sys
import types
from unittest.mock import patch

if "Milter" not in sys.modules:  # pragma: no cover - environment stub
    _stub = types.ModuleType("Milter")

    class _Base:
        def __init__(self, *a, **k):
            pass

    _stub.Milter = _Base
    for _c in ("CONTINUE", "REJECT", "TEMPFAIL", "ACCEPT", "DISCARD", "ADDRCPT",
               "CHGHDRS", "CHGFROM"):
        setattr(_stub, _c, 0)
    _stub.factory = None
    _stub.runmilter = lambda *a, **k: None
    _utils = types.ModuleType("Milter.utils")
    _utils.parseaddr = _email_utils.parseaddr
    _stub.utils = _utils
    sys.modules["Milter"] = _stub
    sys.modules["Milter.utils"] = _utils

from mail_milter.tracking_milter.milter_server import (
    generate_envelope_from,
    get_rbm_domain_for_api_key,
    is_direct_rbm_child,
)

PAI = "shop.example.com"
FILHO = "mail.shop.example.com"
NETO_RBM = "rbm533320.mail.shop.example.com"


# --- pure rule ----------------------------------------------------------------

def test_direct_child_counts():
    assert is_direct_rbm_child(f"rbm123456.{PAI}", PAI)


def test_grandchild_does_not_count_for_the_parent():
    # the parent must NOT inherit the child's envelope
    assert not is_direct_rbm_child(NETO_RBM, PAI)


def test_the_same_name_counts_for_the_child():
    assert is_direct_rbm_child(NETO_RBM, FILHO)


def test_label_without_rbm_does_not_count():
    assert not is_direct_rbm_child(f"mail.{PAI}", PAI)


def test_equal_or_empty_domain_does_not_count():
    assert not is_direct_rbm_child(PAI, PAI)
    assert not is_direct_rbm_child("", PAI)
    assert not is_direct_rbm_child(f"rbm1.{PAI}", "")


def test_case_insensitive():
    assert is_direct_rbm_child(f"RBM99.{PAI.upper()}", PAI)


# --- integration with the lookup ----------------------------------------------

def _lookup(rows, domain=PAI):
    with patch("mail_milter.tracking_milter.milter_server.fetch_one",
               return_value={"name": domain}), \
         patch("mail_milter.tracking_milter.milter_server.fetch_all",
               return_value=rows):
        return get_rbm_domain_for_api_key("ak_test", api_key_domain_id=1)


def test_lookup_ignores_grandchild_and_does_not_rewrite():
    # even if the SQL returned the grandchild, the Python rule rejects it
    assert _lookup([{"name": NETO_RBM}]) is None


def test_lookup_returns_the_direct_child():
    assert _lookup([{"name": f"rbm777.{PAI}"}]) == f"rbm777.{PAI}"


def test_lookup_without_candidate_does_not_rewrite():
    assert _lookup([]) is None


def test_envelope_not_generated_without_a_direct_child():
    # the caller only applies chgfrom for a string; None = keep the original envelope
    with patch("mail_milter.tracking_milter.milter_server.fetch_one",
               return_value={"name": PAI}), \
         patch("mail_milter.tracking_milter.milter_server.fetch_all",
               return_value=[{"name": NETO_RBM}]):
        assert generate_envelope_from("ak_test", "someone@example.com", 1) is None


def test_verp_envelope_of_the_direct_child():
    with patch("mail_milter.tracking_milter.milter_server.fetch_one",
               return_value={"name": FILHO}), \
         patch("mail_milter.tracking_milter.milter_server.fetch_all",
               return_value=[{"name": NETO_RBM}]):
        assert generate_envelope_from("ak_test", "someone@example.com", 1) == \
            f"bounces+someone=example.com@{NETO_RBM}"
