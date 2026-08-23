import asyncio
import json
import secrets
import time
from urllib.parse import urlparse

import pytest
from coincurve import PrivateKey, PublicKey

from lnbits.extensions.nostrlogin.services.nip46 import (
    NIP46_KIND,
    NostrLoginService,
    build_connect_uri,
    hash_binding_nonce,
)
from lnbits.extensions.nostrlogin.services.ratelimit import SlidingWindowRateLimiter
from lnbits.extensions.nostrlogin.services.validate import (
    CHALLENGE_KIND,
    EventValidationError,
    normalize_url,
    urls_match,
    validate_challenge_event,
    validate_nip98_event,
)
from lnbits.utils.nostr import sign_event


def _make_key() -> tuple[str, PrivateKey]:
    private_key = PrivateKey(secrets.token_bytes(32))
    return private_key.public_key.format()[1:].hex(), private_key


def _signed(kind: int, tags: list[list[str]], key: PrivateKey) -> dict:
    pubkey_hex = key.public_key.format()[1:].hex()
    return sign_event(
        {
            "created_at": int(time.time()),
            "kind": kind,
            "tags": tags,
            "content": "",
        },
        pubkey_hex,
        key,
    )


# ---------------------------------------------------------------------------
# Connect URI


def test_build_connect_uri():
    relays = ["wss://a.example", "wss://b.example"]
    uri = build_connect_uri("a" * 64, relays, "s3cret", "LNbits")
    parsed = urlparse(uri)
    assert parsed.scheme == "nostrconnect"
    assert parsed.netloc == "a" * 64
    query = parsed.query.split("&")
    assert "relay=wss%3A%2F%2Fa.example" in query
    assert "secret=s3cret" in query
    assert "perms=sign_event%3A27235,get_public_key" in query
    assert "name=LNbits" in query


# ---------------------------------------------------------------------------
# URL normalization


def test_normalize_url_strips_defaults():
    assert normalize_url("HTTP://Example.com:80/path/") == "http://example.com/path"
    assert normalize_url("https://example.com:443") == "https://example.com"
    assert normalize_url("https://example.com:8080/x") == "https://example.com:8080/x"


def test_urls_match():
    assert urls_match("https://example.com/nostr", "HTTPS://EXAMPLE.COM/nostr/")
    assert not urls_match("https://example.com/a", "https://example.com/b")
    assert not urls_match("https://example.com", "http://example.com")


# ---------------------------------------------------------------------------
# NIP-98 validation


def test_validate_nip98_ok():
    _, key = _make_key()
    event = _signed(27235, [["u", "https://x.com/nostr"], ["method", "POST"]], key)
    assert validate_nip98_event(
        event,
        expected_pubkey=event["pubkey"],
        expected_urls=["https://X.com/nostr/"],
    )


def test_validate_nip98_wrong_kind():
    _, key = _make_key()
    event = _signed(22242, [], key)
    with pytest.raises(EventValidationError):
        validate_nip98_event(event)


def test_validate_nip98_wrong_url():
    _, key = _make_key()
    event = _signed(27235, [["u", "https://evil.com"], ["method", "POST"]], key)
    with pytest.raises(EventValidationError):
        validate_nip98_event(event, expected_urls=["https://x.com"])


def test_validate_nip98_wrong_method():
    _, key = _make_key()
    event = _signed(27235, [["u", "https://x.com"], ["method", "GET"]], key)
    with pytest.raises(EventValidationError):
        validate_nip98_event(event, expected_urls=["https://x.com"])


def test_validate_nip98_nonce_mismatch():
    _, key = _make_key()
    event = _signed(
        27235, [["u", "https://x.com"], ["method", "POST"], ["challenge", "aaa"]], key
    )
    with pytest.raises(EventValidationError):
        validate_nip98_event(
            event, expected_urls=["https://x.com"], expected_nonce="bbb"
        )


def test_validate_nip98_expired():
    _, key = _make_key()
    pubkey_hex = key.public_key.format()[1:].hex()
    event = sign_event(
        {
            "created_at": int(time.time()) - 3600,
            "kind": 27235,
            "tags": [["u", "https://x.com"], ["method", "POST"]],
            "content": "",
        },
        pubkey_hex,
        key,
    )
    with pytest.raises(EventValidationError):
        validate_nip98_event(event, expected_urls=["https://x.com"])


def test_validate_nip98_bad_signature():
    _, key = _make_key()
    event = _signed(27235, [["u", "https://x.com"], ["method", "POST"]], key)
    event["sig"] = "00" * 64
    with pytest.raises(EventValidationError):
        validate_nip98_event(event, expected_urls=["https://x.com"])


# ---------------------------------------------------------------------------
# Kind-22242 challenge validation


def test_validate_challenge_ok():
    _, key = _make_key()
    event = _signed(CHALLENGE_KIND, [["challenge", "nonce123"]], key)
    assert validate_challenge_event(
        event, expected_pubkey=event["pubkey"], expected_nonce="nonce123"
    )


def test_validate_challenge_wrong_nonce():
    _, key = _make_key()
    event = _signed(CHALLENGE_KIND, [["challenge", "other"]], key)
    with pytest.raises(EventValidationError):
        validate_challenge_event(
            event, expected_pubkey=event["pubkey"], expected_nonce="nonce123"
        )


# ---------------------------------------------------------------------------
# Rate limiter


def test_rate_limiter_blocks_after_limit():
    limiter = SlidingWindowRateLimiter(limit=3, window_seconds=60)
    assert all(limiter.allow("ip") for _ in range(3))
    assert not limiter.allow("ip")
    assert limiter.allow("other")


# ---------------------------------------------------------------------------
# Avatar / profile sync


def test_is_https_url():
    from lnbits.extensions.nostrlogin.services.avatars import _is_https_url

    assert _is_https_url("https://example.com/a.png")
    assert not _is_https_url("http://example.com/a.png")
    assert not _is_https_url("ftp://x")
    assert not _is_https_url(None)
    assert not _is_https_url(123)


# ---------------------------------------------------------------------------
# NIP-46 flow against an in-memory fake signer


class FakePool:
    """Stands in for RelayPool; plays the role of the remote signer."""

    def __init__(self, relays, client_pubkey_hex, signer_priv, behavior="ok"):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.client_pubkey_hex = client_pubkey_hex
        self.signer_priv = signer_priv
        self.signer_pubkey_hex = signer_priv.public_key.format()[1:].hex()
        self.behavior = behavior  # "ok" | "fallback" | "auth_url"
        self._tasks: list[asyncio.Task] = []

    @property
    def connected_count(self) -> int:
        return 1

    async def start(self) -> None:
        # A real signer sends the connect acknowledgement unprompted.
        self._tasks.append(asyncio.create_task(self._send_ack()))

    async def _send_ack(self) -> None:
        await asyncio.sleep(0.01)
        await self._emit(
            {"id": "connect", "result": "ack"}, {"pubkey": self.client_pubkey_hex}
        )

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()

    def _user_keys(self):
        if not hasattr(self, "_user_priv"):
            self._user_priv = PrivateKey(secrets.token_bytes(32))
        return (
            self._user_priv.public_key.format()[1:].hex(),
            self._user_priv,
        )

    async def publish(self, event: dict) -> None:
        self._tasks.append(asyncio.create_task(self._respond(event)))

    def _decrypt(self, event: dict) -> dict:
        from lnbits.wallets.nwc import NIP44Encryption

        plaintext = NIP44Encryption.decrypt(
            event["content"],
            PublicKey(bytes.fromhex("02" + event["pubkey"])),
            self.signer_priv.to_hex(),
        )
        return json.loads(plaintext)

    def _encrypt(self, payload: str, client_pubkey_hex: str) -> str:
        from lnbits.utils.nostr import encrypt_content
        from lnbits.wallets.nwc import NIP44Encryption

        if getattr(self, "use_nip04", False):
            return encrypt_content(
                payload,
                PublicKey(bytes.fromhex("02" + client_pubkey_hex)),
                self.signer_priv.to_hex(),
            )
        return NIP44Encryption.encrypt(
            payload,
            PublicKey(bytes.fromhex("02" + client_pubkey_hex)),
            self.signer_priv.to_hex(),
        )

    async def _respond(self, event: dict) -> None:
        await asyncio.sleep(0.01)
        request = self._decrypt(event)
        response = {"id": request.get("id")}
        method = request.get("method")

        if method == "connect":
            response["result"] = "ack"
        elif method == "get_public_key":
            pubkey_hex, _ = self._user_keys()
            response["result"] = pubkey_hex
        elif method == "sign_event":
            unsigned = json.loads(request["params"][0])
            if unsigned["kind"] == 27235 and self.behavior == "fallback":
                response["error"] = "not permitted"
            elif self.behavior == "auth_url":
                response["result"] = "https://auth.example.com/challenge"
                response["error"] = "auth_url"
            else:
                pubkey_hex, priv = self._user_keys()
                unsigned["pubkey"] = pubkey_hex
                from lnbits.utils.nostr import sign_event as _sign

                signed = _sign(unsigned, pubkey_hex, priv)
                response["result"] = json.dumps(signed)
        else:
            response["error"] = f"unknown method {method}"

        # auth_url: send the error first, then the real answer
        if self.behavior == "auth_url" and response.get("error") == "auth_url":
            await self._emit(response, event)
            await asyncio.sleep(0.02)
            pubkey_hex, priv = self._user_keys()
            unsigned = json.loads(request["params"][0])
            unsigned["pubkey"] = pubkey_hex
            from lnbits.utils.nostr import sign_event as _sign

            signed = _sign(unsigned, pubkey_hex, priv)
            final = {"id": request["id"], "result": json.dumps(signed)}
            await self._emit(final, event)
            return
        await self._emit(response, event)

    async def _emit(self, response: dict, request_event: dict) -> None:
        from lnbits.utils.nostr import sign_event as _sign

        content = self._encrypt(json.dumps(response), request_event["pubkey"])
        reply_event = _sign(
            {
                "created_at": int(time.time()),
                "kind": NIP46_KIND,
                "tags": [["p", request_event["pubkey"]]],
                "content": content,
            },
            self.signer_pubkey_hex,
            self.signer_priv,
        )
        await self.queue.put(reply_event)


def _new_session(
    service: NostrLoginService,
    purpose: str = "login",
    behavior: str = "ok",
    monkeypatch=None,
):
    from lnbits.extensions.nostrlogin.services import nip46 as nip46_mod

    signer_priv = PrivateKey(secrets.token_bytes(32))
    if monkeypatch:
        monkeypatch.setattr(
            nip46_mod,
            "RelayPool",
            lambda relays, pk: FakePool(relays, pk, signer_priv, behavior),
        )
    return service.create_session(
        purpose=purpose,
        relays=["wss://fake.relay"],
        binding_nonce_hash=hash_binding_nonce(b"x"),
        login_url="https://lnbits.test/nostrlogin/api/v1/nip98/login",
        app_name="LNbits",
        autostart=False,
    )


@pytest.mark.anyio
async def test_nip46_flow_approves(monkeypatch):
    from lnbits.extensions.nostrlogin.services import nip46 as nip46_mod

    service = NostrLoginService()
    session = _new_session(service, monkeypatch=monkeypatch)
    await asyncio.wait_for(
        nip46_mod.NostrLoginService._run_session(service, session), 15
    )
    assert session.status == "approved"
    assert session.user_pubkey is not None


@pytest.mark.parametrize("behavior", ["fallback", "auth_url"])
@pytest.mark.anyio
async def test_nip46_flow_fallback_and_auth_url(behavior, monkeypatch):
    from lnbits.extensions.nostrlogin.services import nip46 as nip46_mod

    service = NostrLoginService()
    session = _new_session(service, behavior=behavior, monkeypatch=monkeypatch)
    await asyncio.wait_for(
        nip46_mod.NostrLoginService._run_session(service, session), 20
    )
    assert session.status == "approved"
    if behavior == "auth_url":
        assert session.auth_url == "https://auth.example.com/challenge"


@pytest.mark.anyio
async def test_nip46_timeout_when_silent(monkeypatch):
    from lnbits.extensions.nostrlogin.services import nip46 as nip46_mod

    monkeypatch.setattr(nip46_mod, "SESSION_TTL_SECONDS", 1)
    monkeypatch.setattr(nip46_mod, "RPC_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(nip46_mod, "SIGN_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(nip46_mod, "REPUBLISH_INTERVAL", 1)

    service = NostrLoginService()

    class SilentPool(FakePool):
        async def publish(self, event):
            return

    monkeypatch.setattr(
        nip46_mod,
        "RelayPool",
        lambda r, p: SilentPool(r, p, PrivateKey(secrets.token_bytes(32))),
    )
    session = service.create_session(
        purpose="login",
        relays=["wss://fake.relay"],
        binding_nonce_hash=hash_binding_nonce(b"x"),
        login_url="https://lnbits.test/nostrlogin/api/v1/nip98/login",
        app_name="LNbits",
        autostart=False,
    )
    await asyncio.wait_for(
        nip46_mod.NostrLoginService._run_session(service, session), 30
    )
    assert session.status == "failed"
