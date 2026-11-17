"""Parity of the merge tags v2 resolver with the normative fixture (46 cases).

The fixture (fixtures/merge-tags-cases.json) is the reference output every
merge tags v2 implementation must reproduce.
"""

import json
from pathlib import Path

import pytest
from mail_api.services.merge_tags import resolve_tokens_v2

FIXTURE = Path(__file__).parent / "fixtures" / "merge-tags-cases.json"
DOC = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES = DOC["cases"]


def test_fixture_has_all_cases():
    # Guards against a truncated/wrong copy of the fixture: the spec has exactly 46 cases.
    assert DOC["version"] == 1
    assert len(CASES) == 46


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixture_case(case):
    assert resolve_tokens_v2(case["text"], case["values"]) == case["expected"]


def test_empty_token_collapse_is_linear_on_large_html():
    # 2,000 empty tokens in a large HTML: the "last emitted char is a space"
    # check must not join the whole output on every token.
    text = "<p>" + " {{user.x}} ".join(["a"] * 2000) + "</p>"
    out = resolve_tokens_v2(text, {})
    assert out == "<p>" + " ".join(["a"] * 2000) + "</p>"


# --- HTML context ("HTML escaping" contract) ----------------------------------
# Profile/event/recipient values are escaped in text/html; fallbacks (they come
# from the template, already escaped by the compiler) and structural values go in raw.

EVIL = 'x" onmouseover="alert(1)<b>&'
EVIL_ESC = "x&quot; onmouseover=&quot;alert(1)&lt;b&gt;&amp;"


def test_html_escapes_user_value_inside_href():
    out = resolve_tokens_v2('<a href="{{user.url}}">v</a>', {"user.url": EVIL}, html=True)
    assert out == f'<a href="{EVIL_ESC}">v</a>'


@pytest.mark.parametrize(
    ("token", "key"),
    [
        ("user.nome", "user.nome"),
        ("event.produto", "event.produto"),
        ("first_name", "first_name"),
        ("name", "name"),
        ("email", "email"),
        ("phone", "phone"),
        ("apelido", "user.apelido"),  # flat alias -> user.apelido
        ("webhook_key", "webhook_key"),  # flat key sent by a webhook
    ],
)
def test_html_escapes_non_structural_values_in_body(token, key):
    out = resolve_tokens_v2(f"<p>Oi {{{{{token}}}}}!</p>", {key: "<b>A & 'B'</b>"}, html=True)
    assert out == "<p>Oi &lt;b&gt;A &amp; &#x27;B&#x27;&lt;/b&gt;!</p>"


def test_html_fallback_is_not_escaped():
    out = resolve_tokens_v2("<p>{{user.x|<b>R&amp;D</b>}}</p>", {}, html=True)
    assert out == "<p><b>R&amp;D</b></p>"


def test_html_structural_value_is_raw():
    body = '<table><tr><td class="c">A &amp; B</td></tr></table>'
    out = resolve_tokens_v2("<div>{{body}}</div>", {"body": body}, html=True)
    assert out == f"<div>{body}</div>"


def test_html_token_nested_in_structural_value_is_escaped_once():
    subs = {"headline": "<h1>Oi {{first_name}}</h1>", "first_name": "Ana & Bia"}
    out = resolve_tokens_v2("{{headline}}", subs, html=True)
    assert out == "<h1>Oi Ana &amp; Bia</h1>"


def test_html_unsub_link_untouched():
    out = resolve_tokens_v2('<a href="{{unsub_link}}">s</a>', {"unsub_link": '"x'}, html=True)
    assert out == '<a href="{{unsub_link}}">s</a>'


def test_html_default_is_false_raw_values():
    assert resolve_tokens_v2("{{user.x}}", {"user.x": "<b>&"}) == "<b>&"
