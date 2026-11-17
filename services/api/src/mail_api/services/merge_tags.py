"""Merge tags v2: resolver for `{{NAME|fallback}}` tokens (token_policy="v2").

The parity fixture (`services/api/tests/fixtures/merge-tags-cases.json`) is
normative: do NOT change the semantics here without changing the fixture.

Rules in short:
- `{{unsub_link}}` (any case) is never touched: it belongs to the milter.
- Structural (headline, body, subject, cta_text, cta_link, link, id): the value
  if present, otherwise the token stays byte-identical; the fallback is ignored.
- Recipient (first_name, name, email, phone), `user.*`, `event.*` and unknown
  flat names (alias of `user.<NAME>`): value → fallback → ''.
- Lookup: exact key, then case-insensitive. Missing = absent key, None, '' or
  '\\N'.
- A token that becomes '' swallows ONE following space when the last emitted
  character is already a space; line breaks are never touched.
- Literal substitution; up to 3 passes, stopping when the text stabilizes.
"""

import html as _html
import re
from collections.abc import Mapping
from typing import Any

TOKEN_RE = re.compile(
    r"\{\{\s*([A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_\-]*)*)\s*(?:\|([^|}]*))?\}\}"
)
MAX_TOKEN_LEN = 256
MAX_PASSES = 3
STRUCTURAL = frozenset({"headline", "body", "subject", "cta_text", "cta_link", "link", "id"})
RECIPIENT = frozenset({"first_name", "name", "email", "phone"})


def _clean_fallback(raw: str | None) -> str | None:
    if raw is None:
        return None
    fb = raw.strip()
    if len(fb) >= 2 and fb[0] == fb[-1] and fb[0] in ("'", '"'):
        fb = fb[1:-1]
    return fb


def _present(v: Any) -> str | None:
    if v is None:
        return None
    s = v if isinstance(v, str) else str(v)
    if s == "" or s == "\\N":
        return None
    return s


def _lookup(values: Mapping[str, Any], key: str) -> str | None:
    if key in values:
        return _present(values[key])
    low = key.lower()
    for k, v in values.items():
        if k.lower() == low:
            return _present(v)
    return None


def _resolve_name(name: str, values: Mapping[str, Any]) -> tuple[str, str | None]:
    """(kind, value-or-None); kind ∈ {'keep', 'structural', 'value'}."""
    if name.lower() == "unsub_link":
        return "keep", None
    if name in STRUCTURAL:
        return "structural", _present(values.get(name))
    if name in RECIPIENT or name.startswith(("user.", "event.")):
        return "value", _lookup(values, name)
    v = _lookup(values, name)
    if v is None:
        v = _lookup(values, "user." + name)
    return "value", v


def _emitted_ends_with_space(out: list[str]) -> bool:
    """Same as "".join(out).endswith(" ") without the O(n) join on every empty token
    (email HTML is tens of KB)."""
    for piece in reversed(out):
        if piece:
            return piece.endswith(" ")
    return False


def _one_pass(text: str, values: Mapping[str, Any], html: bool = False) -> str:
    out: list[str] = []
    pos = 0
    skip_space = False
    for m in TOKEN_RE.finditer(text):
        chunk = text[pos:m.start()]
        if skip_space and chunk.startswith(" "):
            chunk = chunk[1:]
        skip_space = False
        out.append(chunk)
        pos = m.end()
        raw = m.group(0)
        if len(raw) > MAX_TOKEN_LEN:
            out.append(raw)
            continue
        kind, value = _resolve_name(m.group(1), values)
        if kind == "keep" or (kind == "structural" and value is None):
            out.append(raw)
            continue
        if kind == "structural":
            out.append(value)
            continue
        if value is None:
            # the fallback comes from the template (already escaped when it was built): raw
            value = _clean_fallback(m.group(2)) or ""
        elif html:
            # profile/event/recipient/webhook value in an HTML context
            value = _html.escape(value, quote=True)
        out.append(value)
        if value == "" and _emitted_ends_with_space(out):
            skip_space = True
    tail = text[pos:]
    if skip_space and tail.startswith(" "):
        tail = tail[1:]
    out.append(tail)
    return "".join(out)


def header_safe(value: str) -> str:
    """Line break → space, for the subject and headers resolved in v2.

    Covers every separator that `str.splitlines()` recognizes (\\r\\n, \\r, \\n,
    \\x0b, \\x0c, \\x1c-\\x1e, \\x85, \\u2028, \\u2029): each break becomes a space.
    Profile/event values are arbitrary text (addresses, descriptions); a `\\n`
    inside the Subject makes Python's `email` package raise
    HeaderParseError/HeaderWriteError in the queue processor and the send to
    that recipient fails. Not a resolver rule (the fixture does not change): it
    is header-context hygiene, only on the v2 path."""
    return " ".join(value.splitlines())


def resolve_tokens_v2(
    text: str, values: Mapping[str, Any], html: bool = False
) -> str:
    """Resolves v2 merge tags in `text` with `values` (flat key→scalar dict).

    html=True (text/html parts): resolved values of NON-structural tokens
    (recipient, user.*, event.*, flat alias, webhook keys) go through
    html.escape(quote=True). Fallback and structural values go in raw;
    {{unsub_link}} is untouched. The parity fixture runs with html=False."""
    for _ in range(MAX_PASSES):
        new = _one_pass(text, values, html)
        if new == text:
            break
        text = new
    return text
