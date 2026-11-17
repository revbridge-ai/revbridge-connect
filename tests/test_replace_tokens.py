"""Personalization values with backslashes (e.g. "\\N", MySQL's export marker for NULL) must be
inserted literally: re.sub would read them as escapes ("bad escape" PatternError) and fail the
whole POST /emails/send with a 500."""

from mail_api.services.email_service import MailService


def _service() -> MailService:
    # _replace_tokens does not use instance state; skips the side effects of __init__
    return MailService.__new__(MailService)


def test_replace_tokens_basic():
    svc = _service()
    result = svc._replace_tokens(
        "Hello {{first_name}}!", {"first_name": "Maria"}
    )
    assert result == "Hello Maria!"


def test_replace_tokens_value_with_mysql_null_marker():
    svc = _service()
    result = svc._replace_tokens(
        "Offer for {{ first_name }}", {"first_name": r"Customer\N"}
    )
    assert result == r"Offer for Customer\N"


def test_replace_tokens_value_with_backslash_escapes():
    svc = _service()
    subs = {"name": r"D'Ávila \Norte", "code": r"\1\g<0>\\"}
    result = svc._replace_tokens("{{name}} - {{code}}", subs)
    assert result == r"D'Ávila \Norte - \1\g<0>\\"


def test_replace_tokens_nested_substitution_with_backslash():
    svc = _service()
    subs = {"subject": r"Hello {{first_name}}", "first_name": r"\Name"}
    result = svc._replace_tokens("{{subject}}", subs)
    assert result == r"Hello \Name"
