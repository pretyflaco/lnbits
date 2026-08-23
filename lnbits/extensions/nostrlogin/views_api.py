import base64
import hashlib
import hmac
import json
import secrets
from http import HTTPStatus
from time import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

from lnbits.core.crud import (
    get_account,
    get_account_by_pubkey,
    update_account,
)
from lnbits.core.models import Account, User, UserExtra
from lnbits.core.services import create_user_account
from lnbits.decorators import check_admin, check_user_exists, optional_user_id
from lnbits.helpers import create_access_token
from lnbits.settings import settings
from lnbits.utils.nostr import normalize_public_key

from .crud import (
    consume_replay_event,
    get_nostrlogin_settings,
    update_nostrlogin_settings,
)
from .models import NostrLoginSettings
from .services.nip46 import (
    SESSION_TTL_SECONDS,
    get_nostrlogin_service,
)
from .services.ratelimit import SlidingWindowRateLimiter
from .services.validate import EventValidationError, validate_nip98_event

nostrlogin_ext_api = APIRouter()

BIND_COOKIE_NAME = "nostrlogin_bind"
BIND_COOKIE_MAX_AGE = 600

_NIP98_LOGIN_PATH = "/nostrlogin/api/v1/nip98/login"

# freshness of the signed event for the URL-borne magic link (seconds)
_GET_LINK_MAX_AGE = 90

_rate_limiter = SlidingWindowRateLimiter(limit=10, window_seconds=60)


# ---------------------------------------------------------------------------
# Session lifecycle


@nostrlogin_ext_api.post(
    "/api/v1/session", description="Start a NostrConnect login"
)
async def create_login_session(request: Request) -> JSONResponse:
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limiter.allow(client_ip):
        raise HTTPException(
            HTTPStatus.TOO_MANY_REQUESTS, "Too many login attempts, try again later."
        )
    ext_settings = await get_nostrlogin_settings()
    binding_nonce = new_binding_nonce()
    session = get_nostrlogin_service().create_session(
        purpose="login",
        relays=ext_settings.relays,
        binding_nonce_hash=_binding_nonce_hash(binding_nonce),
        login_url=_request_base_url(request) + _NIP98_LOGIN_PATH,
        app_name=_signer_app_name(request),
        instance_url=_request_base_url(request),
        image_url=ext_settings.signer_app_image,
        diagnostic=ext_settings.enable_diagnostic_logging,
    )
    return _session_response(session, ext_settings.relays, binding_nonce)


@nostrlogin_ext_api.post(
    "/api/v1/link/session",
    description="Start a NostrConnect session to link a key to the logged-in account",
)
async def create_link_session(
    request: Request, user: User = Depends(check_user_exists)
) -> JSONResponse:
    ext_settings = await get_nostrlogin_settings()
    binding_nonce = new_binding_nonce()
    session = get_nostrlogin_service().create_session(
        purpose="link",
        relays=ext_settings.relays,
        binding_nonce_hash=_binding_nonce_hash(binding_nonce),
        login_url=_request_base_url(request) + _NIP98_LOGIN_PATH,
        app_name=_signer_app_name(request),
        instance_url=_request_base_url(request),
        image_url=ext_settings.signer_app_image,
        diagnostic=ext_settings.enable_diagnostic_logging,
    )
    return _session_response(session, ext_settings.relays, binding_nonce)


def _session_response(
    session,
    relays: list[str],
    binding_nonce: str,
) -> JSONResponse:
    response = JSONResponse(
        {
            "id": session.id,
            "connect_uri": session.connect_uri,
            "relays": relays,
            "expires_in": SESSION_TTL_SECONDS,
        }
    )
    _set_bind_cookie(response, binding_nonce)
    return response


@nostrlogin_ext_api.get("/api/v1/session/{session_id}/status")
async def session_status(
    session_id: str,
    nostrlogin_bind: Annotated[str | None, Cookie()] = None,
    user_id: Annotated[str | None, Depends(optional_user_id)] = None,
):
    service = get_nostrlogin_service()
    session = service.get_session(session_id)
    if not session:
        raise HTTPException(HTTPStatus.NOT_FOUND, "Session not found or expired.")

    # Anti-QRLjacking: only the browser that started the session may poll it.
    cookie_value = nostrlogin_bind
    if not cookie_value or not hmac.compare_digest(
        _binding_nonce_hash(cookie_value), session.binding_nonce_hash
    ):
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "Invalid session binding.")

    if session.status == "pending":
        return {"status": "pending"}
    if session.status == "auth_required":
        return {"status": "auth_required", "auth_url": session.auth_url}
    if session.status == "failed":
        service.remove_session(session_id)
        return {"status": "failed", "reason": session.error}

    # approved - single use
    pubkey = session.user_pubkey
    purpose = session.purpose
    service.remove_session(session_id)
    assert pubkey is not None

    if purpose == "link":
        from lnbits.core.crud.users import get_user

        user = await get_user(user_id) if user_id else None
        if user is None:
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "Not logged in.")
        existing = await get_account_by_pubkey(pubkey)
        if existing and existing.id != user.id:
            return JSONResponse(
                {
                    "status": "failed",
                    "reason": "Key already linked to another account.",
                }
            )
        account = await get_account(user.id)
        account.pubkey = normalize_public_key(pubkey)
        await update_account(account)
        response = JSONResponse(
            {"status": "approved", "redirect": "/nostrlogin/account"}
        )
        response.delete_cookie(BIND_COOKIE_NAME)
        return response

    account = await resolve_account_for_pubkey(pubkey)
    await _maybe_sync_profile(account.id, pubkey)
    return _mint_login_response(account)


# ---------------------------------------------------------------------------
# Open NIP-98 endpoints


@nostrlogin_ext_api.post(
    "/api/v1/nip98/login",
    description="Login via a NIP-98 auth event in the Authorization header",
)
async def nip98_login(request: Request) -> JSONResponse:
    event = _parse_nip98_authorization(request)
    account = await _validate_and_resolve_nip98(request, event, expected_method="POST")
    return _mint_login_response(account)


@nostrlogin_ext_api.get(
    "/api/v1/nip98/link",
    description="Login via a NIP-98 auth event provided as a magic link",
)
async def nip98_magic_link(request: Request) -> JSONResponse:
    event_b64 = request.query_params.get("event")
    if not event_b64:
        raise HTTPException(HTTPStatus.BAD_REQUEST, "Missing 'event' parameter.")
    event = _decode_event(event_b64)
    account = await _validate_and_resolve_nip98(
        request, event, expected_method="GET", max_age_seconds=_GET_LINK_MAX_AGE
    )
    return _mint_login_response(account)


# ---------------------------------------------------------------------------
# Admin settings


@nostrlogin_ext_api.get(
    "/api/v1/admin/settings",
    description="Get extension settings",
    dependencies=[Depends(check_admin)],
)
async def api_get_settings() -> NostrLoginSettings:
    return await get_nostrlogin_settings()


@nostrlogin_ext_api.put(
    "/api/v1/admin/settings",
    description="Update extension settings",
    dependencies=[Depends(check_admin)],
)
async def api_put_settings(data: NostrLoginSettings) -> NostrLoginSettings:
    return await update_nostrlogin_settings(data)


# ---------------------------------------------------------------------------
# Helpers


async def _maybe_sync_profile(user_id: str, pubkey: str) -> None:
    ext_settings = await get_nostrlogin_settings()
    if ext_settings.sync_profile_pictures:
        from .services.avatars import sync_profile_in_background

        sync_profile_in_background(user_id, pubkey)


def new_binding_nonce() -> str:
    return secrets.token_urlsafe(32)


def _binding_nonce_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _set_bind_cookie(response: JSONResponse, nonce_value: str) -> None:
    """
    The browser holds the raw one-time nonce while the server keeps only its
    hash, so only the browser that started the session can poll it.
    """
    response.set_cookie(
        BIND_COOKIE_NAME,
        nonce_value,
        httponly=True,
        secure=settings.auth_https_only,
        samesite="strict",
        max_age=BIND_COOKIE_MAX_AGE,
    )


async def resolve_account_for_pubkey(pubkey: str) -> Account:
    """Finds the account for a nostr pubkey, honoring auto-create setting."""
    account = await get_account_by_pubkey(pubkey)
    if account:
        if not account.activated:
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "User is not activated.")
        return account
    ext_settings = await get_nostrlogin_settings()
    if not ext_settings.allow_auto_user_creation:
        raise HTTPException(
            HTTPStatus.UNAUTHORIZED,
            "No account is linked to this Nostr key. "
            "Link your key first or ask the admin to enable automatic "
            "account creation.",
        )
    try:
        account = Account(
            id=uuid4().hex,
            pubkey=normalize_public_key(pubkey),
            extra=UserExtra(provider="nostr-nip46"),
        )
        await create_user_account(account)
    except Exception as e:
        logger.warning(f"NostrLogin account creation failed: {e!s}")
        raise HTTPException(
            HTTPStatus.UNAUTHORIZED, "Account creation for this key failed."
        ) from e
    return account


def _request_base_url(request: Request) -> str:
    scheme = request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def _request_host(request: Request) -> str:
    host = request.headers.get("host") or request.url.netloc
    # strip a possible port for a cleaner display name
    return host.split(":")[0]


def _signer_app_name(request: Request) -> str:
    """
    Name advertised to the signer. Includes the instance host so signers that
    only render the `name` (not the `url` param) still show which site is
    requesting access.
    """
    title = settings.lnbits_site_title or "LNbits"
    host = _request_host(request)
    if host and host.lower() not in title.lower():
        return f"{title} ({host})"
    return title


def _mint_login_response(account: Account, redirect: str = "/wallet") -> JSONResponse:
    payload = {
        "sub": account.username or "",
        "usr": account.id,
        "email": account.email,
        "auth_time": int(time()),
    }
    access_token = create_access_token(data=payload)
    max_age = settings.auth_token_expire_minutes * 60
    response = JSONResponse({"status": "approved", "redirect": redirect})
    response.set_cookie(
        "cookie_access_token",
        access_token,
        httponly=True,
        secure=settings.auth_https_only,
        samesite="lax",
        max_age=max_age,
    )
    response.set_cookie(
        "is_lnbits_user_authorized",
        "true",
        secure=settings.auth_https_only,
        samesite="lax",
        max_age=max_age,
    )
    response.delete_cookie("is_access_token_expired")
    response.delete_cookie(BIND_COOKIE_NAME)
    return response


def _accepted_nip98_urls(request: Request) -> list[str]:
    urls = [
        u.rstrip("/") + _NIP98_LOGIN_PATH
        for u in settings.nostr_absolute_request_urls
    ]
    if not urls:
        urls = [_request_base_url(request) + _NIP98_LOGIN_PATH]
    return urls


def _parse_nip98_authorization(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("nostr "):
        raise HTTPException(
            HTTPStatus.UNAUTHORIZED, "Missing 'nostr' Authorization header."
        )
    return _decode_event(auth.split(" ", 1)[1])


def _decode_event(encoded: str) -> dict:
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        event = json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "Invalid event encoding.") from exc
    if not isinstance(event, dict):
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "Invalid event.")
    return event


async def _validate_and_resolve_nip98(
    request: Request,
    event: dict,
    *,
    expected_method: str,
    max_age_seconds: int = 600,
) -> Account:
    try:
        validate_nip98_event(
            event,
            expected_urls=_accepted_nip98_urls(request),
            expected_method=expected_method,
            max_age_seconds=max_age_seconds,
        )
    except EventValidationError as exc:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, str(exc)) from exc

    try:
        pubkey = normalize_public_key(event["pubkey"])
    except Exception:
        pubkey = event["pubkey"]

    # Consume the replay guard only after signature verification succeeded.
    ttl = max_age_seconds + 120
    if not await consume_replay_event(event["id"], ttl):
        raise HTTPException(HTTPStatus.UNAUTHORIZED, "Event replay detected.")

    account = await resolve_account_for_pubkey(pubkey)
    await _maybe_sync_profile(account.id, pubkey)
    return account
