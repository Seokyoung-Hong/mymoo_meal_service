"""JWT/JWKS authentication helpers for FastAPI dependencies."""

from __future__ import annotations

import json
from dataclasses import dataclass
from time import time
from typing import Any

import httpx
from fastapi import HTTPException
from jwcrypto import jwk, jwt  # type: ignore[import-untyped]
from jwcrypto.common import (  # type: ignore[import-untyped]
    JWException,
    base64url_decode,
    json_decode,
)

from app.config import Config, logger


BEARER_CHALLENGE = "Bearer"
JWKS_CACHE_TTL_SECONDS = 300


@dataclass
class AuthenticatedPrincipal:
    """Validated auth identity and roles extracted from a request."""

    user_id: str
    global_admin: bool
    meal_admin: bool
    claims: dict[str, Any]


_cached_jwks: jwk.JWKSet | None = None
_cached_jwks_uri: str | None = None
_cached_at = 0.0


def bearer_unauthorized(
    detail: str = "Invalid authentication credentials",
) -> HTTPException:
    """Build a 401 response with the required Bearer challenge header."""
    return HTTPException(
        status_code=Config.HttpStatus.UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": BEARER_CHALLENGE},
    )


def dev_header_fallback_allowed() -> bool:
    """Return whether X-User-ID fallback is enabled for this environment."""
    return Config.AUTH_DEV_HEADER_FALLBACK_ENABLED and Config.ENV.lower() in {
        "local",
        "test",
        "development",
    }


async def get_jwks(force_refresh: bool = False) -> jwk.JWKSet:
    """Fetch and cache the OIDC JWKS configured for bearer-token validation."""
    global _cached_at, _cached_jwks, _cached_jwks_uri  # noqa: PLW0603

    if not Config.KEYCLOAK_DISCOVERY_URL:
        raise bearer_unauthorized("JWT discovery URL is not configured")

    now = time()
    if (
        _cached_jwks is not None
        and _cached_jwks_uri == Config.KEYCLOAK_DISCOVERY_URL
        and now - _cached_at < JWKS_CACHE_TTL_SECONDS
        and not force_refresh
    ):
        return _cached_jwks

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            discovery_response = await client.get(Config.KEYCLOAK_DISCOVERY_URL)
            discovery_response.raise_for_status()
            jwks_uri = discovery_response.json().get("jwks_uri")
            if not jwks_uri:
                raise ValueError("OIDC discovery document does not include jwks_uri")

            jwks_response = await client.get(jwks_uri)
            jwks_response.raise_for_status()
            keys = jwk.JWKSet.from_json(json.dumps(jwks_response.json()))
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("JWT JWKS discovery failed: %s", exc)
        raise bearer_unauthorized() from exc

    _cached_jwks = keys
    _cached_jwks_uri = Config.KEYCLOAK_DISCOVERY_URL
    _cached_at = now
    return keys


def clear_jwks_cache() -> None:
    """Clear cached JWKS metadata; intended for deterministic tests."""
    global _cached_at, _cached_jwks, _cached_jwks_uri  # noqa: PLW0603

    _cached_jwks = None
    _cached_jwks_uri = None
    _cached_at = 0.0


def _token_header(token: str) -> dict[str, Any]:
    try:
        header_segment = token.split(".", 1)[0]
        return json_decode(base64url_decode(header_segment).decode("utf-8"))
    except (IndexError, ValueError, TypeError, JWException) as exc:
        raise bearer_unauthorized() from exc


def _validated_token_header(token: str) -> dict[str, Any]:
    header = _token_header(token)
    algorithm = header.get("alg")
    if algorithm not in Config.JWT_ALLOWED_ALGORITHMS:
        raise bearer_unauthorized()
    if not header.get("kid"):
        raise bearer_unauthorized()
    return header


def _jwks_has_kid(keys: jwk.JWKSet, kid: str) -> bool:
    return keys.get_key(kid) is not None


def _claims_from_token(token: str, keys: jwk.JWKSet) -> dict[str, Any]:
    _ = _validated_token_header(token)

    try:
        verified = jwt.JWT(
            jwt=token,
            key=keys,
            algs=Config.JWT_ALLOWED_ALGORITHMS,
            check_claims={
                "exp": None,
                "iat": None,
                "iss": Config.JWT_ISSUER,
                "sub": None,
                "aud": Config.JWT_AUDIENCE,
            },
            expected_type="JWS",
        )
        claims = json.loads(verified.claims)
    except (JWException, json.JSONDecodeError, KeyError, ValueError) as exc:
        raise bearer_unauthorized() from exc

    if not str(claims.get("sub") or "").strip():
        raise bearer_unauthorized()
    return claims


def _roles_from_claims(claims: dict[str, Any]) -> tuple[bool, bool]:
    realm_roles = claims.get("realm_access", {}).get("roles", [])
    resource_roles = (
        claims.get("resource_access", {}).get(Config.JWT_CLIENT_ID, {}).get("roles", [])
    )
    return (
        Config.REALM_GLOBAL_ADMIN_ROLE in realm_roles,
        Config.MEAL_CLIENT_ADMIN_ROLE in resource_roles,
    )


async def principal_from_authorization(
    authorization: str | None,
) -> AuthenticatedPrincipal:
    """Validate Authorization: Bearer and return the authenticated principal."""
    if not authorization:
        raise bearer_unauthorized("Authorization Bearer token is required")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise bearer_unauthorized("Authorization Bearer token is required")

    token_value = token.strip()
    header = _validated_token_header(token_value)
    kid = str(header["kid"])
    keys = await get_jwks()
    if not _jwks_has_kid(keys, kid):
        keys = await get_jwks(force_refresh=True)
        if not _jwks_has_kid(keys, kid):
            raise bearer_unauthorized()

    claims = _claims_from_token(token_value, keys)
    global_admin, meal_admin = _roles_from_claims(claims)
    return AuthenticatedPrincipal(
        user_id=str(claims["sub"]),
        global_admin=global_admin,
        meal_admin=meal_admin,
        claims=claims,
    )
