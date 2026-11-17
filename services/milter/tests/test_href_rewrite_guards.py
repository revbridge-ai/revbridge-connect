"""The milter's click wrapper versus the output of merge tags.

A merge tag with no value and no fallback, such as `href="{{user.page_url}}"`,
renders as `href=""`. Replacing with `full_tag.replace(href, tracking_url)` would
break on an empty href (`str.replace("", x)` inserts x between every character and
destroys the tag). These tests pin that an empty/blank href is not rewritten and
that the href is replaced by position (only the matched attribute value).
"""

import email.utils as _email_utils
import sys
import types

import pytest

# pymilter (the C module `Milter`) only exists in the milter image; same stub as
# test_milter_per_message_reset.py, installed before importing milter_server.
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
    _utils = types.ModuleType("Milter.utils")
    _utils.parseaddr = _email_utils.parseaddr
    _stub.utils = _utils
    sys.modules["Milter"] = _stub
    sys.modules["Milter.utils"] = _utils

from mail_milter.tracking_milter.milter_server import (
    A_HREF_URL_RE,
    href_is_untrackable,
    splice_href,
)


def _wrap_all(html: str) -> str:
    """Same contract as eom()'s _rewrite_href_link, with a fake tracking URL."""

    def _rw(m):
        if href_is_untrackable(m.group(1)):
            return m.group(0)
        return splice_href(m, "https://t.example.com/c/TOKEN")

    return A_HREF_URL_RE.sub(_rw, html)


@pytest.mark.parametrize("href", ["", " ", "   "])
def test_empty_or_blank_href_is_untrackable(href):
    assert href_is_untrackable(href) is True


@pytest.mark.parametrize(
    "href", ["mailto:a@b.com", "tel:+5511", "ftp:x", "file:x", "javascript:void(0)", "#top"]
)
def test_special_schemes_stay_untrackable(href):
    assert href_is_untrackable(href) is True


@pytest.mark.parametrize("href", ["https://shop.example/p?c=ABC", "http://x", "shop.example", "{{link}}"])
def test_regular_hrefs_are_trackable(href):
    # "{{link}}" (a structural tag without a value) is still wrapped.
    assert href_is_untrackable(href) is False


def test_empty_href_tag_is_left_intact():
    html = '<a href="" class="btn">View</a>'
    assert _wrap_all(html) == html


def test_splice_replaces_only_the_href_value():
    html = '<a title="https://x.com" href="https://x.com" data-u="https://x.com">x</a>'
    assert _wrap_all(html) == (
        '<a title="https://x.com" href="https://t.example.com/c/TOKEN" data-u="https://x.com">x</a>'
    )


def test_regular_link_rewrite_is_unchanged():
    html = '<p><a href="https://shop.example/p?c=ABC" style="color:red">view</a></p>'
    assert _wrap_all(html) == (
        '<p><a href="https://t.example.com/c/TOKEN" style="color:red">view</a></p>'
    )
