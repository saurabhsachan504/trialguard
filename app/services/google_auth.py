"""Verify a Google Identity Services ID token.

The browser gets the token straight from Google (no redirect, no client
secret); we only have to prove it is genuine and meant for us. Google signs it
with rotating RSA keys published at a well-known JWKS endpoint, so PyJWT -
already a dependency - can check the signature locally.

Three things are checked, and all three matter:
  1. SIGNATURE - proves Google issued it and nobody tampered with the claims.
  2. AUDIENCE  - proves it was minted for *our* client id.
  3. ISSUER    - accounts.google.com, with or without the https:// prefix.

Expiry is enforced by PyJWT itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

from app.config import settings

logger = logging.getLogger("trialguard.google")

_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

_jwk_client: PyJWKClient | None = None


class GoogleAuthError(Exception):
    """The token is missing, malformed, expired or not meant for us."""


@dataclass(slots=True)
class GoogleIdentity:
    sub: str
    email: str
    email_verified: bool
    name: str | None
    picture: str | None


def _client() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        _jwk_client = PyJWKClient(_JWKS_URL, cache_keys=True)
    return _jwk_client


def verify_id_token(credential: str) -> GoogleIdentity:
    """Return the identity inside a Google ID token, or raise GoogleAuthError."""
    if not settings.GOOGLE_CLIENT_ID:
        raise GoogleAuthError("Google sign-in is not configured on this server.")
    if not credential or len(credential) > 8192:
        raise GoogleAuthError("Invalid Google credential.")

    try:
        signing_key = _client().get_signing_key_from_jwt(credential)
        claims = jwt.decode(
            credential,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.GOOGLE_CLIENT_ID,
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise GoogleAuthError("This Google sign-in has expired. Please try again.")
    except jwt.InvalidAudienceError:
        raise GoogleAuthError("This Google sign-in was not issued for this site.")
    except Exception as exc:
        logger.warning("Google ID token rejected: %s", exc)
        raise GoogleAuthError("Could not verify this Google sign-in.")

    if claims.get("iss") not in _ISSUERS:
        raise GoogleAuthError("Unexpected token issuer.")

    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise GoogleAuthError("Google did not return an email address.")

    return GoogleIdentity(
        sub=str(claims["sub"]),
        email=email,
        email_verified=claims.get("email_verified") in (True, "true", "True"),
        name=(claims.get("name") or None),
        picture=(claims.get("picture") or None),
    )
