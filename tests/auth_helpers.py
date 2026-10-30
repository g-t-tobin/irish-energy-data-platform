"""Controlled RSA keys and token factory; no real Cognito or JWKS access."""

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.settings import CognitoConfig

CONFIG = CognitoConfig(
    region="eu-west-1",
    user_pool_id="eu-west-1_TestPool1",
    client_id="testclientid1234567890",
)
KID = "test-key-1"

_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk_for(private_key=_PRIVATE, kid: str = KID) -> dict[str, Any]:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    return {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


def jwks(*keys: dict[str, Any]) -> dict[str, Any]:
    return {"keys": list(keys) or [jwk_for()]}


def claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    base: dict[str, Any] = {
        "sub": "user-123",
        "iss": CONFIG.issuer,
        "client_id": CONFIG.client_id,
        "token_use": "access",
        "iat": now,
        "exp": now + 600,
        "cognito:groups": ["viewer"],
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def token(
    *,
    groups: Any = "default",
    signing_key=_PRIVATE,
    kid: str | None = KID,
    algorithm: str = "RS256",
    **overrides: Any,
) -> str:
    payload = claims(**overrides)
    if groups != "default":
        payload.pop("cognito:groups", None)
        if groups is not None:
            payload["cognito:groups"] = groups
    headers = {"kid": kid} if kid else {}
    return jwt.encode(payload, signing_key, algorithm=algorithm, headers=headers)


def wrongly_signed(**overrides: Any) -> str:
    return token(signing_key=_OTHER_PRIVATE, **overrides)


def unsigned(**overrides: Any) -> str:
    return jwt.encode(claims(**overrides), None, algorithm="none", headers={"kid": KID})


def hs256_forged_with_public_key(**overrides: Any) -> str:
    """Classic algorithm-confusion token, built by hand (PyJWT refuses to)."""
    def b64(data: bytes) -> bytes:
        return base64.urlsafe_b64encode(data).rstrip(b"=")

    public_pem = _PRIVATE.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    signing_input = (
        b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
        + b"."
        + b64(json.dumps(claims(**overrides)).encode())
    )
    signature = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + b64(signature)).decode()


def auth_header(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}
