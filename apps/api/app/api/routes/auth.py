from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.core.config import get_settings
from app.core.security import CurrentUser, create_access_token, verify_admin_credentials
from app.schemas.api import TokenResponse

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/token", response_model=TokenResponse)
def create_token(form: Annotated[OAuth2PasswordRequestForm, Depends()]) -> TokenResponse:
    if not verify_admin_credentials(form.username, form.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_in = create_access_token(form.username)
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me")
def me(user: CurrentUser) -> dict[str, str | bool]:
    settings = get_settings()
    return {
        "username": user,
        "environment": settings.app_env,
        "development_credentials": not bool(settings.app_admin_password_hash),
    }
