"""
Tests for the suppression filter at the point of sending (services/suppression_filter.py).

Every send checks its recipients against the active suppressions of the sender's
domains before anything is queued.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mail_api.services import suppression_filter as sf


def _pers(*addresses):
    """Minimal personalization: only what the filter touches (.to[].address)."""
    to = [SimpleNamespace(address=a, name="") for a in addresses]

    def model_copy(update):
        return _pers(*[r.address for r in update["to"]])

    return SimpleNamespace(to=to, model_copy=lambda update: model_copy(update))


def _row(email, domain, tipo="bounce"):
    return {"email": email, "domain_name": domain, "suppression_type": tipo}


class TestDomainMatches:
    def test_equal(self):
        assert sf._domain_matches("m.shop.example", {"m.shop.example"})

    def test_suppression_on_the_rbm_envelope_matches_the_jwt_domain(self):
        # events_updater records the ENVELOPE domain; the API only knows the JWT's.
        assert sf._domain_matches(
            "rbm1001.updates.shop.example",
            {"updates.shop.example"},
        )

    def test_suppression_on_the_base_domain_matches_a_sender_subdomain(self):
        assert sf._domain_matches("shop.example", {"news.shop.example"})

    def test_does_not_match_across_customers(self):
        assert not sf._domain_matches(
            "rbm1002.send.store-b.example",
            {"updates.shop.example"},
        )

    def test_does_not_match_a_partial_suffix_without_a_dot(self):
        # "shop.example" must not match "myshop.example"
        assert not sf._domain_matches("shop.example", {"myshop.example"})

    def test_empty_domain(self):
        assert not sf._domain_matches("", {"example.com"})
        assert not sf._domain_matches(None, {"example.com"})


class TestSenderDomains:
    def test_combines_jwt_and_from(self):
        assert sf.sender_domains("updates.shop.example", "no-reply@m.shop.example") == {
            "updates.shop.example",
            "m.shop.example",
        }

    def test_normalizes_case(self):
        assert sf.sender_domains("UPDATES.SHOP.example", None) == {"updates.shop.example"}

    def test_tolerates_nulls(self):
        assert sf.sender_domains(None, None) == set()
        assert sf.sender_domains(None, "no-at-sign") == set()


@pytest.mark.asyncio
class TestLookupSuppressed:
    async def test_returns_only_the_sender(self):
        rows = [
            _row("a@example.org", "rbm1.shop.example", "unsubscribe"),
            _row("b@example.org", "rbm2.store-b.example", "bounce"),
        ]
        with patch.object(sf, "fetch_all_async", AsyncMock(return_value=rows)):
            out = await sf.lookup_suppressed(
                ["a@example.org", "b@example.org"], {"shop.example"}
            )
        assert out == {"a@example.org": "unsubscribe"}

    async def test_normalizes_email(self):
        with patch.object(
            sf, "fetch_all_async", AsyncMock(return_value=[_row("a@example.org", "shop.example")])
        ):
            out = await sf.lookup_suppressed([" A@Example.ORG "], {"shop.example"})
        assert out == {"a@example.org": "bounce"}

    async def test_no_recipients_no_lookup(self):
        mock = AsyncMock(return_value=[])
        with patch.object(sf, "fetch_all_async", mock):
            assert await sf.lookup_suppressed([], {"shop.example"}) == {}
        mock.assert_not_called()

    async def test_no_domain_no_lookup(self):
        mock = AsyncMock(return_value=[])
        with patch.object(sf, "fetch_all_async", mock):
            assert await sf.lookup_suppressed(["a@example.org"], set()) == {}
        mock.assert_not_called()

    async def test_database_error_fails_open(self):
        with patch.object(sf, "fetch_all_async", AsyncMock(side_effect=RuntimeError("db down"))):
            out = await sf.lookup_suppressed(["a@example.org"], {"shop.example"})
        assert out == {}  # does not block the send

    async def test_chunking(self):
        emails = [f"u{i}@example.org" for i in range(1200)]
        mock = AsyncMock(return_value=[])
        with patch.object(sf, "fetch_all_async", mock), \
             patch.object(sf, "SUPPRESSION_CHECK_CHUNK", 500):
            await sf.lookup_suppressed(emails, {"shop.example"})
        assert mock.await_count == 3  # 500 + 500 + 200


@pytest.mark.asyncio
class TestFilterPersonalizations:
    async def test_enforce_removes_only_the_suppressed(self):
        pers = [_pers("ok@example.org", "bad@example.org")]
        with patch.object(sf, "SUPPRESSION_CHECK_MODE", sf.MODE_ENFORCE), \
             patch.object(sf, "lookup_suppressed", AsyncMock(return_value={"bad@example.org": "bounce"})):
            kept, supp = await sf.filter_personalizations(pers, {"example.org"})
        assert [r.address for r in kept[0].to] == ["ok@example.org"]
        assert supp == {"bad@example.org": "bounce"}

    async def test_enforce_drops_an_empty_personalization(self):
        pers = [_pers("bad@example.org"), _pers("ok@example.org")]
        with patch.object(sf, "SUPPRESSION_CHECK_MODE", sf.MODE_ENFORCE), \
             patch.object(sf, "lookup_suppressed", AsyncMock(return_value={"bad@example.org": "bounce"})):
            kept, _ = await sf.filter_personalizations(pers, {"example.org"})
        assert len(kept) == 1
        assert [r.address for r in kept[0].to] == ["ok@example.org"]

    async def test_enforce_all_suppressed_returns_empty(self):
        pers = [_pers("bad@example.org")]
        with patch.object(sf, "SUPPRESSION_CHECK_MODE", sf.MODE_ENFORCE), \
             patch.object(sf, "lookup_suppressed", AsyncMock(return_value={"bad@example.org": "unsubscribe"})):
            kept, supp = await sf.filter_personalizations(pers, {"example.org"})
        assert kept == []
        assert supp == {"bad@example.org": "unsubscribe"}

    async def test_dryrun_reports_but_does_not_remove(self):
        pers = [_pers("ok@example.org", "bad@example.org")]
        with patch.object(sf, "SUPPRESSION_CHECK_MODE", sf.MODE_DRYRUN), \
             patch.object(sf, "lookup_suppressed", AsyncMock(return_value={"bad@example.org": "bounce"})):
            kept, supp = await sf.filter_personalizations(pers, {"example.org"})
        assert kept is pers
        assert [r.address for r in kept[0].to] == ["ok@example.org", "bad@example.org"]
        assert supp == {"bad@example.org": "bounce"}

    async def test_off_does_not_even_look_up(self):
        pers = [_pers("bad@example.org")]
        mock = AsyncMock(return_value={"bad@example.org": "bounce"})
        with patch.object(sf, "SUPPRESSION_CHECK_MODE", sf.MODE_OFF), \
             patch.object(sf, "lookup_suppressed", mock):
            kept, supp = await sf.filter_personalizations(pers, {"example.org"})
        assert kept is pers
        assert supp == {}
        mock.assert_not_called()

    async def test_no_suppression_returns_the_original_list(self):
        pers = [_pers("ok@example.org")]
        with patch.object(sf, "SUPPRESSION_CHECK_MODE", sf.MODE_ENFORCE), \
             patch.object(sf, "lookup_suppressed", AsyncMock(return_value={})):
            kept, supp = await sf.filter_personalizations(pers, {"example.org"})
        assert kept is pers  # identity: the router relies on it to skip the model_copy
        assert supp == {}


@pytest.mark.asyncio
class TestComModelosReais:
    """
    The tests above use fakes. These use the real pydantic models —
    that is where `model_copy(update=...)` and the `from_` alias may differ from the fake.
    """

    def _req(self, *addresses):
        from mail_api.models.email_models import SendEmailRequest

        return SendEmailRequest(
            **{
                "personalizations": [
                    {"to": [{"address": a, "name": "T"} for a in addresses]}
                ],
                "from": {"address": "no-reply@updates.shop.example", "name": "Shop"},
                "subject": "hi",
                "content": [{"type": "text/html", "value": "<p>x</p>"}],
            }
        )

    async def test_removes_a_recipient_keeping_the_rest(self):
        req = self._req("ok@example.org", "bad@example.org")
        with patch.object(sf, "SUPPRESSION_CHECK_MODE", sf.MODE_ENFORCE), \
             patch.object(sf, "lookup_suppressed", AsyncMock(return_value={"bad@example.org": "bounce"})):
            kept, _ = await sf.filter_personalizations(req.personalizations, {"updates.shop.example"})

        assert [r.address for r in kept[0].to] == ["ok@example.org"]
        # model_copy must not lose the other fields of the personalization
        assert kept[0].substitutions == req.personalizations[0].substitutions

    async def test_rebuilt_request_keeps_the_from_alias(self):
        req = self._req("ok@example.org", "bad@example.org")
        with patch.object(sf, "SUPPRESSION_CHECK_MODE", sf.MODE_ENFORCE), \
             patch.object(sf, "lookup_suppressed", AsyncMock(return_value={"bad@example.org": "bounce"})):
            kept, _ = await sf.filter_personalizations(req.personalizations, {"updates.shop.example"})

        # this is what the router does when it rebuilds the filtered request
        novo = req.model_copy(update={"personalizations": kept})
        assert novo.from_.address == "no-reply@updates.shop.example"
        assert novo.subject == "hi"
        assert sum(len(p.to) for p in novo.personalizations) == 1

    async def test_lookup_receives_the_request_addresses(self):
        req = self._req("a@example.org", "b@example.org")
        mock = AsyncMock(return_value={})
        with patch.object(sf, "SUPPRESSION_CHECK_MODE", sf.MODE_ENFORCE), \
             patch.object(sf, "lookup_suppressed", mock):
            await sf.filter_personalizations(req.personalizations, {"updates.shop.example"})
        assert sorted(mock.await_args.args[0]) == ["a@example.org", "b@example.org"]
