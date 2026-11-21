"""Login/logout endpoints and session check."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from ..auth import COOKIE_NAME, check_password, create_session_token, require_auth
from ..config import settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
async def login(body: LoginRequest, response: Response):
    if not check_password(body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid password",
        )
    response.set_cookie(
        COOKIE_NAME,
        create_session_token(),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@router.get("/me", dependencies=[Depends(require_auth)])
async def me():
    return {"ok": True}
