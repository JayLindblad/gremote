"""Password session handling for the dashboard login.

The secret used to sign session cookies comes from settings.secret_key. A login
sets a signed, expiring cookie; the API dependency verifies it on each request.
"""

import hmac

from fastapi import Cookie, HTTPException, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

COOKIE_NAME = "gremote_session"

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="gremote-session")


def check_password(candidate: str) -> bool:
    return hmac.compare_digest(candidate, settings.admin_password)


def create_session_token() -> str:
    return _serializer.dumps({"authed": True})


def verify_session_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=settings.session_ttl_seconds)
        return True
    except (BadSignature, SignatureExpired):
        return False


async def require_auth(gremote_session: str | None = Cookie(default=None)) -> None:
    if not verify_session_token(gremote_session):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
