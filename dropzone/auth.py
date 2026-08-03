"""Shared-token auth.

The token bootstraps via ?t=<token> (the only thing a QR code can carry), is
immediately exchanged for an HttpOnly cookie, and is then stripped from the
address bar client-side. Tokens left in URLs leak through Referer headers,
browser history and logs.

The cookie is also what makes SSE authenticatable at all — EventSource cannot
set custom headers.
"""

import secrets
from fastapi import HTTPException, Request

from dropzone.config import settings

COOKIE_NAME = "dz_token"


def token_from(request: Request) -> str:
    """Explicit credentials win over the stored cookie.

    The token is regenerated on every restart, so a browser that visited an
    earlier run still holds a stale cookie. Checking the cookie first would
    let that dead value shadow the fresh token in the link and lock the user
    out of a URL that is perfectly valid.
    """
    return (
        request.query_params.get("t")
        or request.headers.get("x-dropzone-token")
        or request.cookies.get(COOKIE_NAME)
        or ""
    )


def is_valid(token: str) -> bool:
    # Constant-time so the token can't be recovered by timing the comparison.
    return secrets.compare_digest(token, settings.DROPZONE_TOKEN)


async def require_auth(request: Request):
    """FastAPI dependency guarding every data route."""
    if not is_valid(token_from(request)):
        raise HTTPException(status_code=401, detail="bad or missing token")


def clear_cookie(response):
    """Remove a stale token cookie left by an earlier run."""
    response.delete_cookie(COOKIE_NAME, path="/")


def set_cookie(response, request: Request):
    """Persist the token as a cookie once it has been validated.

    `secure` is set only when the request arrived over HTTPS: a Secure cookie
    is dropped by the browser on a plain-http LAN origin, which would break
    the escape-hatch mode entirely.
    """
    https = request.url.scheme == "https" or request.headers.get(
        "x-forwarded-proto"
    ) == "https"
    response.set_cookie(
        COOKIE_NAME,
        settings.DROPZONE_TOKEN,
        httponly=True,
        secure=https,
        # Lax, not Strict: the tablet arrives here by following a QR link from
        # another app, and Strict withholds the cookie on such cross-site
        # navigations — the page would load unauthenticated on first tap.
        # Lax still blocks cross-site POSTs, which is the CSRF case that matters.
        samesite="lax",
        max_age=24 * 60 * 60,
        path="/",
    )
