import json
import logging

import pytest

pytest.importorskip("jwt")
import jwt

from src import auth
from src.auth import AuthError, JwksKeyProvider, TokenVerifier
from tests import auth_helpers as h


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_verifier(fetch=h.jwks):
    provider = JwksKeyProvider("https://unused.invalid/jwks.json", fetch=fetch)
    return TokenVerifier(h.CONFIG, provider)


def reject(token_value, verifier=None):
    with pytest.raises(AuthError) as exc:
        (verifier or make_verifier()).verify(token_value)
    return exc.value.reason


def test_valid_token_yields_principal():
    principal = make_verifier().verify(h.token(groups=["viewer"]))

    assert principal.sub == "user-123"
    assert principal.role == "viewer"
    assert principal.groups == frozenset({"viewer"})


@pytest.mark.parametrize("value", ["", "garbage", "a.b", "a.b.c", "....", "Bearer x"])
def test_malformed_tokens_rejected(value):
    reject(value)


def test_oversized_token_rejected():
    assert reject("a" * (auth.MAX_TOKEN_LENGTH + 1)) == "bad_token_size"


def test_expired_token_rejected():
    reason = reject(h.token(exp=1, iat=0))

    assert "ExpiredSignatureError" in reason


def test_token_just_inside_leeway_is_accepted():
    import time

    now = int(time.time())

    assert make_verifier().verify(h.token(exp=now - 5, iat=now - 100))


def test_future_issued_token_rejected():
    import time

    reject(h.token(iat=int(time.time()) + 3600, exp=int(time.time()) + 7200))


def test_incorrectly_signed_token_rejected():
    assert "InvalidSignatureError" in reject(h.wrongly_signed())


def test_tampered_payload_rejected():
    header, _payload, signature = h.token(groups=["viewer"]).split(".")
    forged = jwt.api_jws.base64url_encode(
        json.dumps({**h.claims(), "cognito:groups": ["operator"]}).encode()
    ).decode()

    reject(f"{header}.{forged}.{signature}")


def test_unsigned_token_rejected():
    assert reject(h.unsigned()) == "unsupported_algorithm"


def test_hs256_algorithm_confusion_rejected():
    assert reject(h.hs256_forged_with_public_key()) == "unsupported_algorithm"


def test_other_rsa_algorithm_rejected():
    assert reject(h.token(algorithm="RS512")) == "unsupported_algorithm"


def test_wrong_issuer_rejected():
    assert "InvalidIssuerError" in reject(h.token(iss="https://evil.example.com/pool"))


def test_issuer_for_other_pool_rejected():
    other = f"https://cognito-idp.{h.CONFIG.region}.amazonaws.com/eu-west-1_Other"

    reject(h.token(iss=other))


def test_wrong_client_id_rejected():
    assert reject(h.token(client_id="someotherclient123")) == "wrong_client_id"


def test_missing_client_id_rejected():
    assert "MissingRequiredClaimError" in reject(h.token(client_id=None))


def test_id_token_rejected():
    id_token = h.token(token_use="id", aud=h.CONFIG.client_id, client_id=None)

    reject(id_token)


def test_id_token_with_client_id_still_rejected():
    assert reject(h.token(token_use="id")) == "not_access_token"


@pytest.mark.parametrize("missing", ["exp", "iat", "iss", "sub", "token_use"])
def test_required_claims_enforced(missing):
    reject(h.token(**{missing: None}))


def test_empty_subject_rejected():
    assert reject(h.token(sub="")) == "bad_subject"


def test_missing_kid_rejected():
    assert reject(h.token(kid=None)) == "missing_kid"


def test_unknown_kid_rejected():
    assert reject(h.token(kid="not-in-jwks")) == "unknown_kid"


@pytest.mark.parametrize(
    "groups,role",
    [
        (None, None),
        ([], None),
        (["viewer"], "viewer"),
        (["operator"], "operator"),
        (["viewer", "operator"], "operator"),
        (["admin", "operator"], "operator"),
        (["Viewer"], None),
        (["OPERATOR"], None),
        ([" operator"], None),
        (["operators"], None),
        ("operator", None),
        ([1, 2], None),
        (["viewer", 5], None),
        ({"operator": True}, None),
        (7, None),
    ],
)
def test_roles_come_only_from_exact_verified_group_names(groups, role):
    principal = make_verifier().verify(h.token(groups=groups))

    assert principal.role == role


def test_custom_role_claims_are_ignored():
    principal = make_verifier().verify(
        h.token(groups=None, role="operator", **{"custom:role": "operator"}, scope="operator")
    )

    assert principal.role is None


def test_rejections_never_log_the_token(caplog):
    caplog.set_level(logging.DEBUG)
    bad = h.wrongly_signed()

    reject(bad)

    assert bad not in caplog.text
    assert bad.split(".")[1] not in caplog.text


# JWKS provider behaviour


def test_keys_are_cached_between_verifications():
    calls = []

    def fetch():
        calls.append(1)
        return h.jwks()

    verifier = make_verifier(fetch)
    for _ in range(3):
        verifier.verify(h.token())

    assert len(calls) == 1


def test_unknown_kids_cannot_trigger_unlimited_refetches():
    clock = Clock()
    calls = []
    provider = JwksKeyProvider("https://unused.invalid", fetch=lambda: calls.append(1) or h.jwks(), clock=clock)

    for _ in range(5):
        with pytest.raises(AuthError):
            provider.get_key("nope")
    assert len(calls) == 1

    clock.now += auth.JWKS_COOLDOWN_SECONDS
    with pytest.raises(AuthError):
        provider.get_key("nope")
    assert len(calls) == 2


def test_key_rotation_is_picked_up_after_cooldown():
    clock = Clock()
    document = {"keys": [h.jwk_for(kid="old")]}
    provider = JwksKeyProvider("https://unused.invalid", fetch=lambda: document, clock=clock)
    provider.get_key("old")

    document["keys"] = [h.jwk_for(kid="new")]
    with pytest.raises(AuthError):
        provider.get_key("new")  # still inside the cooldown
    clock.now += auth.JWKS_COOLDOWN_SECONDS

    assert provider.get_key("new")


def test_fetch_failure_fails_closed_and_is_rate_limited(caplog):
    calls = []

    def failing():
        calls.append(1)
        raise OSError("network down: https://secret.invalid")

    verifier = make_verifier(failing)

    for _ in range(3):
        assert reject(h.token(), verifier) == "unknown_kid"
    assert len(calls) == 1
    assert "secret.invalid" not in caplog.text


@pytest.mark.parametrize(
    "document",
    [{}, {"keys": "x"}, {"keys": [None]}, {"keys": [{"kty": "oct", "kid": "k"}]}, {"keys": []}, []],
)
def test_malformed_jwks_documents_fail_closed(document):
    assert reject(h.token(), make_verifier(lambda: document)) == "unknown_kid"


def test_non_signing_or_wrong_algorithm_keys_are_ignored():
    bad = {**h.jwk_for(), "use": "enc"}
    wrong_alg = {**h.jwk_for(), "alg": "RS512"}

    assert reject(h.token(), make_verifier(lambda: {"keys": [bad]})) == "unknown_kid"
    assert reject(h.token(), make_verifier(lambda: {"keys": [wrong_alg]})) == "unknown_kid"


def test_expired_cache_with_failing_refresh_fails_closed():
    clock = Clock()
    state = {"fail": False}

    def fetch():
        if state["fail"]:
            raise OSError("down")
        return h.jwks()

    provider = JwksKeyProvider("https://unused.invalid", fetch=fetch, clock=clock)
    assert provider.get_key(h.KID)

    state["fail"] = True
    clock.now += auth.JWKS_TTL_SECONDS + 1

    with pytest.raises(AuthError):
        provider.get_key(h.KID)


def test_non_https_jwks_url_is_refused():
    with pytest.raises(AuthError):
        auth._fetch_jwks("http://insecure.example.com/jwks.json")


def test_real_network_is_blocked_in_tests():
    provider = JwksKeyProvider("https://cognito-idp.eu-west-1.amazonaws.com/x/.well-known/jwks.json")

    with pytest.raises(AuthError):  # fetch failure is swallowed, guard raised inside
        provider.get_key("any")
