import base64
import json
import os
import time
from uuid import uuid4

import pytest
from coincurve import PrivateKey
from httpx import AsyncClient

from lnbits.core.models import Account, UserExtra
from lnbits.core.services import create_user_account
from lnbits.utils.nostr import sign_event


def _nip98_event(method: str = "POST", url: str | None = None) -> tuple[dict, str]:
    private_key = PrivateKey(os.urandom(32))
    pubkey_hex = private_key.public_key.format().hex()[2:]
    event = {
        "created_at": int(time.time()),
        "kind": 27235,
        "tags": [["u", url or ""], ["method", method]],
        "content": "",
    }
    return sign_event(event, pubkey_hex, private_key), pubkey_hex


@pytest.mark.anyio
async def test_create_login_session(http_client: AsyncClient):
    response = await http_client.post("/nostrlogin/api/v1/session")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["connect_uri"].startswith("nostrconnect://")
    assert data["relays"], "Default relays configured."
    assert "nostrlogin_bind" in response.cookies


@pytest.mark.anyio
async def test_session_status_requires_binding_cookie(http_client: AsyncClient):
    response = await http_client.post("/nostrlogin/api/v1/session")
    session_id = response.json()["id"]
    # drop the binding cookie to simulate a different browser
    http_client.cookies.clear()
    status = await http_client.get(f"/nostrlogin/api/v1/session/{session_id}/status")
    assert status.status_code == 401

    bound = await http_client.get(
        f"/nostrlogin/api/v1/session/{session_id}/status",
        cookies={"nostrlogin_bind": response.cookies["nostrlogin_bind"]},
    )
    assert bound.status_code == 200
    assert bound.json()["status"] == "pending"


@pytest.mark.anyio
async def test_nip98_login_creates_session(http_client: AsyncClient, settings):
    settings.nostr_absolute_request_urls = ["http://testserver"]
    try:
        event, pubkey_hex = _nip98_event(
            url="http://testserver/nostrlogin/api/v1/nip98/login"
        )
        account = Account(
            id=uuid4().hex,
            pubkey=pubkey_hex,
            extra=UserExtra(provider="nostr-nip46"),
        )
        await create_user_account(account)
        encoded = base64.b64encode(json.dumps(event).encode()).decode("ascii")
        response = await http_client.post(
            "/nostrlogin/api/v1/nip98/login",
            headers={"Authorization": f"nostr {encoded}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "approved"
        assert "cookie_access_token" in response.cookies
    finally:
        settings.nostr_absolute_request_urls = [
            "http://127.0.0.1:5000",
            "http://localhost:5000",
        ]


@pytest.mark.anyio
async def test_nip98_login_replay_rejected(http_client: AsyncClient, settings):
    from lnbits.extensions.nostrlogin.views_api import _rate_limiter

    _rate_limiter._attempts.clear()
    settings.nostr_absolute_request_urls = ["http://testserver"]
    try:
        event, _ = _nip98_event(url="http://testserver/nostrlogin/api/v1/nip98/login")
        encoded = base64.b64encode(json.dumps(event).encode()).decode("ascii")
        first = await http_client.post(
            "/nostrlogin/api/v1/nip98/login",
            headers={"Authorization": f"nostr {encoded}"},
        )
        assert first.status_code in (200, 401)  # auto-create may be off
        replay = await http_client.post(
            "/nostrlogin/api/v1/nip98/login",
            headers={"Authorization": f"nostr {encoded}"},
        )
        assert replay.status_code == 401
        assert "replay" in replay.json()["detail"].lower()
    finally:
        settings.nostr_absolute_request_urls = [
            "http://127.0.0.1:5000",
            "http://localhost:5000",
        ]


@pytest.mark.anyio
async def test_nip98_login_existing_account(http_client: AsyncClient, settings):
    settings.nostr_absolute_request_urls = ["http://testserver"]
    try:
        event, pubkey_hex = _nip98_event(
            url="http://testserver/nostrlogin/api/v1/nip98/login"
        )
        account = Account(
            id=uuid4().hex,
            pubkey=pubkey_hex,
            extra=UserExtra(provider="nostr-nip46"),
        )
        await create_user_account(account)
        encoded = base64.b64encode(json.dumps(event).encode()).decode("ascii")
        response = await http_client.post(
            "/nostrlogin/api/v1/nip98/login",
            headers={"Authorization": f"nostr {encoded}"},
        )
        assert response.status_code == 200, response.text
        access_token = response.cookies["cookie_access_token"]

        me = await http_client.get(
            "/api/v1/auth", headers={"Authorization": f"Bearer {access_token}"}
        )
        assert me.status_code == 200
        assert me.json()["pubkey"] == pubkey_hex
    finally:
        settings.nostr_absolute_request_urls = [
            "http://127.0.0.1:5000",
            "http://localhost:5000",
        ]


@pytest.mark.anyio
async def test_nip98_auto_creation_gated(http_client: AsyncClient, settings):
    settings.nostr_absolute_request_urls = ["http://testserver"]
    try:
        event, _ = _nip98_event(url="http://testserver/nostrlogin/api/v1/nip98/login")
        encoded = base64.b64encode(json.dumps(event).encode()).decode("ascii")
        response = await http_client.post(
            "/nostrlogin/api/v1/nip98/login",
            headers={"Authorization": f"nostr {encoded}"},
        )
        # default: no automatic account creation
        assert response.status_code == 401
        assert "linked" in response.json()["detail"].lower()
    finally:
        settings.nostr_absolute_request_urls = [
            "http://127.0.0.1:5000",
            "http://localhost:5000",
        ]


@pytest.mark.anyio
async def test_login_session_rate_limited(http_client: AsyncClient):
    from lnbits.extensions.nostrlogin.views_api import _rate_limiter

    _rate_limiter._attempts.clear()
    last = None
    for _ in range(12):
        last = await http_client.post("/nostrlogin/api/v1/session")
    assert last.status_code == 429


@pytest.mark.anyio
async def test_admin_settings_roundtrip(http_client: AsyncClient, settings):
    superuser_token = None
    login = await http_client.post(
        "/api/v1/auth",
        json={"username": "superadmin", "password": "secret1234"},
    )
    if login.status_code == 200:
        superuser_token = login.json().get("access_token")
    assert superuser_token, "Superuser login required."
    http_client.cookies.clear()

    get_resp = await http_client.get(
        "/nostrlogin/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {superuser_token}"},
    )
    assert get_resp.status_code == 200, get_resp.text
    current = get_resp.json()

    put_resp = await http_client.put(
        "/nostrlogin/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {superuser_token}"},
        json={**current, "allow_auto_user_creation": True},
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["allow_auto_user_creation"] is True

    # restore
    await http_client.put(
        "/nostrlogin/api/v1/admin/settings",
        headers={"Authorization": f"Bearer {superuser_token}"},
        json=current,
    )


@pytest.mark.anyio
async def test_admin_settings_requires_admin(http_client: AsyncClient):
    response = await http_client.get("/nostrlogin/api/v1/admin/settings")
    assert response.status_code in (401, 403)


@pytest.mark.anyio
async def test_profile_sync_updates_account(http_client: AsyncClient, monkeypatch):
    from lnbits.core.crud import get_account
    from lnbits.extensions.nostrlogin.services import avatars

    account = Account(id=uuid4().hex, extra=UserExtra(provider="nostr-nip46"))
    await create_user_account(account)

    async def fake_fetch(pubkey, relays=None):
        return {"picture": "https://img.example.com/a.png", "name": "Satoshi"}

    monkeypatch.setattr(avatars, "fetch_profile", fake_fetch)
    await avatars.sync_profile(account.id, "aa" * 32)

    updated = await get_account(account.id)
    assert updated.extra.picture == "https://img.example.com/a.png"
    assert updated.extra.display_name == "Satoshi"
