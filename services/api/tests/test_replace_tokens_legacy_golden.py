"""Golden cases for the LEGACY behavior of MailService._replace_tokens.

Merge tags add token_policy="v2". These cases pin the legacy behavior byte for
byte for string inputs: any caller that does not send token_policy (test sends,
integrations, warmup) must keep receiving exactly this. If one of these tests
breaks, legacy changed — that is a regression, not an "expectation update".
"""

import pytest
from mail_api.services.email_service import MailService


def _svc() -> MailService:
    # _replace_tokens uses no instance state; this avoids the side effects of __init__
    return MailService.__new__(MailService)


LEGACY_GOLDEN = [
    # (id, text, substitutions, expected)
    ("basic", "Olá {{first_name}}!", {"first_name": "Maria"}, "Olá Maria!"),
    ("spaces inside braces", "Oi {{  first_name   }}.", {"first_name": "Ana"}, "Oi Ana."),
    ("unknown token stays literal", "Oi {{user.cupom}} {{desconhecido}}", {}, "Oi {{user.cupom}} {{desconhecido}}"),
    ("dotted key is a plain key in legacy", "{{user.cupom}}", {"user.cupom": "X"}, "X"),
    ("fallback syntax is NOT parsed in legacy", "{{user.cupom|A}}", {"user.cupom": "X"}, "{{user.cupom|A}}"),
    ("unsub_link IS replaced in legacy when caller sends it", "<a href=\"{{unsub_link}}\">", {"unsub_link": "http://e"}, "<a href=\"http://e\">"),
    ("unsub_link without key stays", "<a href=\"{{unsub_link}}\">", {}, "<a href=\"{{unsub_link}}\">"),
    ("empty value keeps both spaces", "a {{first_name}} b", {"first_name": ""}, "a  b"),
    ("mysql null marker is literal", "Oi {{first_name}}", {"first_name": "\\N"}, "Oi \\N"),
    ("mysql null marker inside value", "Oferta pra {{ first_name }}", {"first_name": r"Cliente\N"}, r"Oferta pra Cliente\N"),
    ("nested value with backslash", "{{subject}}", {"subject": r"Olá {{first_name}}", "first_name": r"\Nome"}, r"Olá \Nome"),
    ("backslash escapes are literal", "{{name}} - {{code}}", {"name": r"D'Ávila \Norte", "code": r"\1\g<0>\\"}, r"D'Ávila \Norte - \1\g<0>\\"),
    ("dollar is literal", "{{v}}", {"v": "R$ 1 $1"}, "R$ 1 $1"),
    ("nested structural value", "{{subject}}", {"subject": "Oi {{first_name}}", "first_name": "Ana"}, "Oi Ana"),
    ("nested chain of 4 converges", "{{a}}", {"a": "{{b}}", "b": "{{c}}", "c": "{{d}}", "d": "fim"}, "fim"),
    ("self reference is not expanded", "{{a}}", {"a": "x{{a}}"}, "x{{a}}"),
    ("key match is case-sensitive", "{{First_Name}}", {"first_name": "Ana"}, "{{First_Name}}"),
    ("regex metachar in key", "{{a+b}}", {"a+b": "ok"}, "ok"),
    ("same token repeated", "{{x}}-{{x}}", {"x": "1"}, "1-1"),
    ("newlines untouched", "L1\n{{x}}\nL2", {"x": ""}, "L1\n\nL2"),
    ("single braces untouched", "{x} {{{x}}}", {"x": "v"}, "{x} {v}"),
]


@pytest.mark.parametrize(
    ("text", "subs", "expected"),
    [c[1:] for c in LEGACY_GOLDEN],
    ids=[c[0] for c in LEGACY_GOLDEN],
)
def test_legacy_golden(text, subs, expected):
    assert _svc()._replace_tokens(text, subs) == expected


@pytest.mark.parametrize(
    ("text", "subs", "expected"),
    [c[1:] for c in LEGACY_GOLDEN],
    ids=[c[0] for c in LEGACY_GOLDEN],
)
def test_explicit_legacy_policy_equals_default(text, subs, expected):
    assert _svc()._replace_tokens(text, subs, "legacy") == expected


def test_legacy_null_value_becomes_empty_string():
    # JSON null is accepted with merge tags; in legacy it becomes "" (never "None").
    assert _svc()._replace_tokens("a{{x}}b", {"x": None}) == "ab"


def test_v2_policy_dispatches_to_merge_tags_resolver():
    subs = {"first_name": "Ana", "unsub_link": "http://evil"}
    text = "Oi {{first_name}} {{user.cupom|X}} {{unsub_link}}"
    assert _svc()._replace_tokens(text, subs, "v2") == "Oi Ana X {{unsub_link}}"
