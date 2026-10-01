"""Verifies Google ID tokens for "Masuk dengan Google" (buyers only).

The frontend uses Google Identity Services (GIS) to get an ID token
directly in the browser, then sends just that token here -- we never see
the user's Google password, only a signed token we verify against
Google's public keys.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")

_request = google_requests.Request()


class GoogleLoginNotConfigured(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Login dengan Google belum dikonfigurasi (GOOGLE_CLIENT_ID belum diisi).",
        )


def is_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID)


def verify_google_id_token(token: str) -> dict:
    """Returns {sub, email, name}. Raises HTTPException(401) if the token is
    invalid, expired, or wasn't issued for our GOOGLE_CLIENT_ID."""
    if not is_configured():
        raise GoogleLoginNotConfigured()
    try:
        claims = google_id_token.verify_oauth2_token(token, _request, GOOGLE_CLIENT_ID)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token Google tidak valid") from exc

    if not claims.get("email_verified", False):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email Google belum terverifikasi")

    return {
        "sub": claims["sub"],
        "email": claims["email"],
        "name": claims.get("name") or claims["email"].split("@")[0],
    }
