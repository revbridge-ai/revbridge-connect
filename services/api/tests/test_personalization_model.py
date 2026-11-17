"""Personalization: token_policy + substitutions that accept JSON scalars.

A numeric value in substitutions (e.g. placeholder values sent by a webhook) is
coerced to text instead of rejecting the whole send with a 422.
"""

import pytest
from mail_api.models.email_models import Personalization, SendEmailRequest
from pydantic import ValidationError

TO = [{"address": "a@b.com", "name": "A"}]


def test_token_policy_defaults_to_legacy():
    p = Personalization(to=TO)
    assert p.token_policy == "legacy"
    assert p.substitutions == {}


def test_token_policy_accepts_v2():
    assert Personalization(to=TO, token_policy="v2").token_policy == "v2"


def test_token_policy_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Personalization(to=TO, token_policy="liquid")


def test_string_values_are_untouched():
    subs = {"first_name": "Maria", "code": r"\N", "empty": "", "num_text": "007"}
    assert Personalization(to=TO, substitutions=subs).substitutions == subs


def test_scalars_are_coerced_to_str():
    p = Personalization(
        to=TO,
        substitutions={"i": 150, "f": 4.5, "t": True, "fl": False, "z": 0, "n": None},
    )
    assert p.substitutions == {
        "i": "150",
        "f": "4.5",
        "t": "true",
        "fl": "false",
        "z": "0",
        "n": None,
    }


@pytest.mark.parametrize("bad", [[1, 2], {"a": "b"}])
def test_non_scalar_values_are_rejected(bad):
    with pytest.raises(ValidationError):
        Personalization(to=TO, substitutions={"k": bad})


def test_json_request_with_numeric_substitution_validates():
    # The real path: FastAPI validates the JSON body of POST /send.
    req = SendEmailRequest.model_validate_json(
        '{"personalizations":[{"to":[{"address":"a@b.com"}],'
        '"substitutions":{"user.pontos":150,"user.nota":4.5,"user.vip":true,"user.x":null},'
        '"token_policy":"v2"}],'
        '"from":{"address":"x@y.com"},"subject":"s",'
        '"content":[{"type":"text/html","value":"<p>{{user.pontos}}</p>"}]}'
    )
    p = req.personalizations[0]
    assert p.token_policy == "v2"
    assert p.substitutions == {
        "user.pontos": "150",
        "user.nota": "4.5",
        "user.vip": "true",
        "user.x": None,
    }


def test_model_copy_keeps_token_policy():
    # suppression_filter and volume_governor rebuild personalizations with
    # model_copy(update={"to": ...}) — the policy must survive.
    p = Personalization(to=TO, token_policy="v2", substitutions={"a": 1})
    q = p.model_copy(update={"to": []})
    assert q.token_policy == "v2"
    assert q.substitutions == {"a": "1"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_floats_are_rejected(bad):
    with pytest.raises(ValidationError):
        Personalization(to=TO, substitutions={"k": bad})
