"""Single-admin authentication: credential checks and bearer-token claims."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import hash_password

ME = "/api/v1/auth/me"
TOKEN = "/api/v1/auth/token"


def _login(client, username: str, password: str):
    return client.post(TOKEN, data={"username": username, "password": password})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(**claims) -> str:
    now = datetime.now(UTC)
    payload = {"sub": "admin", "iat": now, "exp": now + timedelta(minutes=5), **claims}
    payload = {key: value for key, value in payload.items() if value is not None}
    return jwt.encode(payload, get_settings().jwt_secret, algorithm="HS256")


def test_login_issues_token_accepted_by_me(client) -> None:
    response = _login(client, "admin", "test-password")
    assert response.status_code == 200
    body = response.json()
    assert body["expires_in"] == get_settings().jwt_expire_minutes * 60
    me = client.get(ME, headers=_bearer(body["access_token"]))
    assert me.status_code == 200
    assert me.json()["username"] == "admin"


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("admin", "wrong-password"),
        ("someone", "test-password"),
        # Non-ASCII input used to crash compare_digest with a 500.
        ("管理员", "test-password"),
        ("admin", "密码"),
        ("adminé", "test-password"),
        ("ａｄｍｉｎ", "test-password"),
    ],
)
def test_wrong_credentials_are_401_never_500(client, username: str, password: str) -> None:
    response = _login(client, username, password)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_non_ascii_credentials_can_be_configured(client, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "app_admin_username", "师承")
    monkeypatch.setattr(settings, "app_admin_password", "跟师-口令")
    assert _login(client, "师承", "跟师-口令").status_code == 200
    assert _login(client, "师承", "跟师-口令x").status_code == 401


def test_password_hash_path_accepts_only_the_hashed_password(client, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "app_admin_password_hash", hash_password("跟师", iterations=1000))
    # The plaintext development password is ignored once a hash is set.
    assert _login(client, "admin", "test-password").status_code == 401
    assert _login(client, "admin", "跟师x").status_code == 401
    assert _login(client, "admin", "跟师").status_code == 200


def test_malformed_password_hash_fails_closed(client, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "app_admin_password_hash", "pbkdf2_sha256$not-a-hash")
    assert _login(client, "admin", "test-password").status_code == 401


def test_clinical_reads_require_a_token(client) -> None:
    assert client.get(ME).status_code == 401
    assert client.get("/api/v1/patients").status_code == 401


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: "not-a-jwt", id="garbage"),
        pytest.param(
            lambda: _token(exp=datetime.now(UTC) - timedelta(seconds=1)), id="expired"
        ),
        pytest.param(lambda: _token(sub="intruder"), id="wrong-subject"),
        pytest.param(
            lambda: jwt.encode(
                {"sub": "admin", "iat": datetime.now(UTC), "exp": datetime.now(UTC)
                 + timedelta(minutes=5)},
                "a-different-secret-that-is-long-enough-for-hs256",
                algorithm="HS256",
            ),
            id="foreign-signature",
        ),
        # A token without an expiry would never lapse; tokens issued here
        # always carry exp/iat/sub, so missing claims mean it is not ours.
        pytest.param(lambda: _token(exp=None), id="missing-exp"),
        pytest.param(lambda: _token(iat=None), id="missing-iat"),
        pytest.param(lambda: _token(sub=None), id="missing-sub"),
    ],
)
def test_untrusted_tokens_are_rejected(client, token) -> None:
    response = client.get(ME, headers=_bearer(token()))
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or expired token"}
