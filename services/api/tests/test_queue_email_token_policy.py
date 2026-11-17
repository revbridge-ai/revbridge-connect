"""queue_email applies the personalization's token_policy to the subject, ALL the
content parts and the headers — the three places where legacy already substitutes —
and the mail_data queued in Redis goes out resolved (the queue processor does not
substitute again).
"""

from email.generator import BytesGenerator
from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from mail_api.models.email_models import SendEmailRequest
from mail_api.services.email_service import MailService


def _request(personalization: dict) -> SendEmailRequest:
    return SendEmailRequest.model_validate(
        {
            "personalizations": [
                {"to": [{"address": "ana@example.org", "name": "Ana"}], **personalization}
            ],
            "headers": {"X-Connect-Campaign": "c1", "X-Promo": "{{user.coupon|NONE}}"},
            "from": {"address": "no-reply@shop.example", "name": "Shop"},
            "subject": "{{subject}}",
            "content": [
                {"type": "text/plain", "value": "Hi {{first_name}}, coupon {{user.coupon|NONE}} {{user.nickname}} ok"},
                {
                    "type": "text/html",
                    "value": (
                        '<p>{{headline}}</p><a href="{{user.page_url|https://shop.example}}">view</a>'
                        '<a href="{{unsub_link}}">leave</a><a href="{{link}}">x</a>'
                    ),
                },
            ],
        }
    )


async def _enqueue(req: SendEmailRequest) -> dict:
    svc = MailService.__new__(MailService)
    captured: list[dict] = []

    async def _capture(data):
        captured.append(data)

    with patch.object(svc, "_add_to_send_queue", AsyncMock(side_effect=_capture)), \
         patch("mail_api.services.send_ledger.admit", AsyncMock()):
        await svc.queue_email("shop.example", req, message_id="m-1")
    assert len(captured) == 1
    return captured[0]["mail_data"]


SUBS = {
    "subject": "Hi {{first_name}}, {{user.coupon|NOTHING}} for you",
    "first_name": "Ana",
    "user.coupon": "WELCOME10",
    "unsub_link": "http://evil",
}


@pytest.mark.asyncio
async def test_v2_resolves_subject_every_content_part_and_headers():
    mail = await _enqueue(_request({"substitutions": SUBS, "token_policy": "v2"}))

    assert mail["subject"] == "Hi Ana, WELCOME10 for you"
    plain, html = mail["content"]
    assert plain == {"type": "text/plain", "value": "Hi Ana, coupon WELCOME10 ok"}
    assert html["type"] == "text/html"
    # absent headline = structural, stays literal; URL fallback applied;
    # unsub_link untouched EVEN with the key in substitutions; absent link stays literal.
    assert html["value"] == (
        '<p>{{headline}}</p><a href="https://shop.example">view</a>'
        '<a href="{{unsub_link}}">leave</a><a href="{{link}}">x</a>'
    )
    assert mail["headers"] == {"X-Connect-Campaign": "c1", "X-Promo": "WELCOME10"}


@pytest.mark.asyncio
async def test_legacy_is_default_and_keeps_historic_output():
    mail = await _enqueue(_request({"substitutions": SUBS}))

    # legacy: no fallback, no user.* alias, no collapsing; unsub_link IS
    # replaced if the caller sends the key (legacy behavior).
    assert mail["subject"] == "Hi Ana, {{user.coupon|NOTHING}} for you"
    plain, html = mail["content"]
    assert plain["value"] == "Hi Ana, coupon {{user.coupon|NONE}} {{user.nickname}} ok"
    assert html["value"] == (
        '<p>{{headline}}</p><a href="{{user.page_url|https://shop.example}}">view</a>'
        '<a href="http://evil">leave</a><a href="{{link}}">x</a>'
    )
    assert mail["headers"]["X-Promo"] == "{{user.coupon|NONE}}"


@pytest.mark.asyncio
async def test_null_value_is_empty_in_legacy_and_absent_in_v2():
    legacy = await _enqueue(_request({"substitutions": {"subject": "S", "first_name": None}}))
    assert legacy["content"][0]["value"].startswith("Hi , coupon")

    v2 = await _enqueue(
        _request({"substitutions": {"subject": "S", "user.coupon": None}, "token_policy": "v2"})
    )
    assert v2["content"][0]["value"] == "Hi , coupon NONE ok"


@pytest.mark.asyncio
async def test_v2_numeric_values_render_as_text():
    mail = await _enqueue(
        _request({"substitutions": {"subject": "S", "user.coupon": 150}, "token_policy": "v2"})
    )
    assert mail["content"][0]["value"] == "Hi , coupon 150 ok"


# Every separator that str.splitlines() treats as a line break.
LINE_BREAKS = [
    "\r\n", "\r", "\n", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("sep", LINE_BREAKS, ids=[repr(s) for s in LINE_BREAKS])
async def test_v2_newlines_from_values_never_reach_subject_or_headers(sep):
    # Multi-line profile value: in the Subject, Python's email package raises
    # HeaderParseError in the queue processor. In v2 it becomes a space; the body keeps it.
    value = f"A{sep}Bcc: evil@example.org"
    subs = {"subject": "Hi {{user.coupon}}", "user.coupon": value}
    mail = await _enqueue(_request({"substitutions": subs, "token_policy": "v2"}))
    assert mail["subject"] == "Hi A Bcc: evil@example.org"
    assert mail["headers"]["X-Promo"] == "A Bcc: evil@example.org"
    assert mail["content"][0]["value"] == f"Hi , coupon {value} ok"

    # MIME serialization, as the queue processor does it, must not raise.
    msg = MailService.__new__(MailService)._create_message(
        {**mail, "to": [{"name": "Ana", "address": "ana@example.org"}]}
    )
    BytesGenerator(BytesIO()).flatten(msg)


@pytest.mark.asyncio
async def test_legacy_subject_newlines_are_not_rewritten():
    # Legacy is byte-identical: no sanitizing (legacy behavior kept).
    subs = {"subject": "Hi\nX"}
    mail = await _enqueue(_request({"substitutions": subs}))
    assert mail["subject"] == "Hi\nX"


@pytest.mark.asyncio
async def test_v2_subject_text_is_not_logged(caplog):
    # A v2 subject carries profile/event data (PII): only its length is logged.
    subs = {"subject": "Hi {{user.coupon}}", "user.coupon": "PRIVATE-PII"}
    with caplog.at_level("INFO", logger="mail_api.services.email_service"):
        await _enqueue(_request({"substitutions": subs, "token_policy": "v2"}))
    lines = [r.getMessage() for r in caplog.records if "Subject processed" in r.getMessage()]
    assert lines, "the processed-subject log line is missing"
    assert all("PRIVATE-PII" not in m for m in lines)
    assert any("len=14" in m for m in lines)


@pytest.mark.asyncio
async def test_legacy_subject_log_is_unchanged(caplog):
    with caplog.at_level("INFO", logger="mail_api.services.email_service"):
        await _enqueue(_request({"substitutions": {"subject": "Hi legacy"}}))
    assert any(
        "Subject processed: 'Hi legacy...'" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_v2_escapes_values_only_in_text_html_parts():
    evil = 'A"<b>&'
    req = SendEmailRequest.model_validate(
        {
            "personalizations": [
                {
                    "to": [{"address": "ana@example.org", "name": "Ana"}],
                    "substitutions": {
                        "subject": "Hi {{user.name}}",
                        "user.name": evil,
                        "user.url": evil,
                        "body": "<p class=\"b\">R&amp;D</p>",
                    },
                    "token_policy": "v2",
                }
            ],
            "headers": {"X-Promo": "{{user.name}}"},
            "from": {"address": "no-reply@shop.example", "name": "Shop"},
            "subject": "{{subject}}",
            "content": [
                {"type": "text/plain", "value": "Hi {{user.name}} {{user.x|<i>&</i>}}"},
                {
                    "type": "text/html",
                    "value": '<a href="{{user.url}}">{{user.name}}</a>{{body}}{{user.x|<i>&amp;</i>}}',
                },
            ],
        }
    )
    mail = await _enqueue(req)
    assert mail["subject"] == f"Hi {evil}"
    assert mail["headers"]["X-Promo"] == evil
    plain, html = mail["content"]
    assert plain["value"] == f"Hi {evil} <i>&</i>"
    assert html["value"] == (
        '<a href="A&quot;&lt;b&gt;&amp;">A&quot;&lt;b&gt;&amp;</a>'
        '<p class="b">R&amp;D</p><i>&amp;</i>'
    )


@pytest.mark.asyncio
async def test_legacy_html_part_is_not_escaped():
    req = SendEmailRequest.model_validate(
        {
            "personalizations": [
                {
                    "to": [{"address": "ana@example.org", "name": "Ana"}],
                    "substitutions": {"first_name": 'A"<b>&', "user.url": 'u"&'},
                }
            ],
            "from": {"address": "no-reply@shop.example", "name": "Shop"},
            "subject": "S",
            "content": [
                {"type": "text/html", "value": '<a href="{{user.url}}">{{first_name}}</a>'}
            ],
        }
    )
    mail = await _enqueue(req)
    assert mail["content"][0]["value"] == '<a href="u"&">A"<b>&</a>'
