from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import InvalidTokenError

from app.core.config import get_settings

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def hash_password(password: str, *, iterations: int = 600_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def _verify_hash(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, digest_raw = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_raw.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_raw.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_raw))
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def verify_admin_credentials(username: str, password: str) -> bool:
    settings = get_settings()
    username_ok = secrets.compare_digest(username, settings.app_admin_username)
    if settings.app_admin_password_hash:
        password_ok = _verify_hash(password, settings.app_admin_password_hash)
    else:
        password_ok = secrets.compare_digest(password, settings.app_admin_password)
    return username_ok and password_ok


def create_access_token(subject: str) -> tuple[str, int]:
    settings = get_settings()
    expires = datetime.now(UTC) + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode(
        {"sub": subject, "exp": expires, "iat": datetime.now(UTC)},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return token, settings.jwt_expire_minutes * 60


def get_current_user(token: Annotated[str | None, Depends(oauth2_scheme)]) -> str:
    settings = get_settings()
    if settings.auth_disabled:
        return settings.app_admin_username
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        subject = payload.get("sub")
        if subject != settings.app_admin_username:
            raise InvalidTokenError("unexpected subject")
        return str(subject)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        ) from exc


CurrentUser = Annotated[str, Depends(get_current_user)]
