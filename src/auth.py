"""Cognito access-token verification and role-based FastAPI dependencies."""

import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from fastapi import Depends, HTTPException, Request

from src.settings import CognitoConfig

logger = logging.getLogger(__name__)

ALGORITHM = "RS256"
MAX_TOKEN_LENGTH = 8192
CLOCK_LEEWAY_SECONDS = 30
JWKS_TTL_SECONDS = 3600
JWKS_COOLDOWN_SECONDS = 60
JWKS_TIMEOUT_SECONDS = 5
JWKS_MAX_BYTES = 1_000_000
REQUIRED_CLAIMS = ["exp", "iat", "iss", "sub", "token_use", "client_id"]
ROLES = ("viewer", "operator")


class AuthError(Exception):
    """Authentication failed; `reason` is for server logs only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Principal:
    sub: str
    groups: frozenset[str]
    role: str | None  # highest verified role, or None


class KeyProvider(Protocol):
    def get_key(self, kid: str) -> Any: ...


def _fetch_jwks(url: str) -> dict[str, Any]:
    if not url.startswith("https://"):
        raise AuthError("jwks_url_not_https")
    with urllib.request.urlopen(url, timeout=JWKS_TIMEOUT_SECONDS) as response:  # noqa: S310
        return json.loads(response.read(JWKS_MAX_BYTES))


class JwksKeyProvider:
    """Cached JWKS keys; refetches are rate limited so bad tokens can't flood."""

    def __init__(
        self,
        jwks_url: str,
        fetch: Callable[[], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch or (lambda: _fetch_jwks(jwks_url))
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None

    def get_key(self, kid: str) -> Any:
        with self._lock:
            now = self._clock()
            if self._fresh(now) and kid in self._keys:
                return self._keys[kid]
            if self._last_attempt is None or now - self._last_attempt >= JWKS_COOLDOWN_SECONDS:
                self._refresh(now)
            if self._fresh(now) and kid in self._keys:
                return self._keys[kid]
            raise AuthError("unknown_kid")

    def _fresh(self, now: float) -> bool:
        return self._fetched_at is not None and now - self._fetched_at < JWKS_TTL_SECONDS

    def _refresh(self, now: float) -> None:
        self._last_attempt = now
        try:
            document = self._fetch()
            raw_keys = document["keys"]
            if not isinstance(raw_keys, list):
                raise TypeError("keys must be a list")
        except Exception as exc:
            logger.warning("JWKS retrieval failed: %s", type(exc).__name__)
            return

        keys: dict[str, Any] = {}
        for raw in raw_keys:
            try:
                if (
                    raw.get("kty") == "RSA"
                    and isinstance(raw.get("kid"), str)
                    and raw.get("use", "sig") == "sig"
                    and raw.get("alg", ALGORITHM) == ALGORITHM
                ):
                    keys[raw["kid"]] = jwt.PyJWK(raw).key
            except Exception:
                continue
        if keys:
            self._keys = keys
            self._fetched_at = now


class TokenVerifier:
    def __init__(self, config: CognitoConfig, keys: KeyProvider) -> None:
        self._config = config
        self._keys = keys

    def verify(self, token: str) -> Principal:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise AuthError("bad_token_size")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError("malformed_token") from exc
        if header.get("alg") != ALGORITHM:
            raise AuthError("unsupported_algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthError("missing_kid")

        key = self._keys.get_key(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                issuer=self._config.issuer,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"invalid_token:{type(exc).__name__}") from exc

        if claims.get("token_use") != "access":
            raise AuthError("not_access_token")
        if claims.get("client_id") != self._config.client_id:
            raise AuthError("wrong_client_id")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise AuthError("bad_subject")

        groups = _verified_groups(claims.get("cognito:groups"))
        role = next((r for r in reversed(ROLES) if r in groups), None)
        return Principal(sub=sub, groups=groups, role=role)


def _verified_groups(claim: Any) -> frozenset[str]:
    # A malformed claim grants nothing; names are matched exactly.
    if isinstance(claim, list) and all(isinstance(item, str) for item in claim):
        return frozenset(claim)
    return frozenset()


def too_many_requests(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="Too many requests.",
        headers={"Retry-After": str(retry_after)},
    )


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(request: Request) -> str | None:
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return None
    scheme, _, token = values[0].partition(" ")
    if scheme.lower() != "bearer" or not token or token != token.strip() or " " in token:
        return None
    return token


def _reject(request: Request, reason: str) -> HTTPException:
    """401 for the caller; repeated failures from one address become 429."""
    logger.info("Authentication rejected: %s", reason)
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None:
        client = request.client.host if request.client else "unknown"
        decision = limiter.consume("auth_failure", client)
        if not decision.allowed:
            return too_many_requests(decision.retry_after)
    return _unauthenticated()


def authenticate(request: Request) -> Principal:
    verifier: TokenVerifier | None = getattr(request.app.state, "verifier", None)
    if verifier is None:
        # Fail closed: unconfigured authentication never allows access.
        raise HTTPException(status_code=503, detail="Authentication is not configured.")

    token = _bearer_token(request)
    if token is None:
        raise _reject(request, "missing_or_malformed_header")
    try:
        principal = verifier.verify(token)
    except AuthError as exc:
        raise _reject(request, exc.reason) from None
    request.state.principal_sub = principal.sub
    return principal


def require_viewer(principal: Principal = Depends(authenticate)) -> Principal:
    if principal.role not in ROLES:
        raise HTTPException(status_code=403, detail="Insufficient permissions.")
    return principal


def require_operator(principal: Principal = Depends(authenticate)) -> Principal:
    if principal.role != "operator":
        raise HTTPException(status_code=403, detail="Insufficient permissions.")
    return principal
