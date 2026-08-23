"""Server-side NIP-46 (Nostr Connect) client.

Modeled after btcpay-nostr-login v0.6.0 (NostrLoginService.cs / RelayPool.cs):

1. An ephemeral client keypair and one-time secret are minted per session and
   encoded into a `nostrconnect://` URI which the user scans with their signer.
2. The server subscribes to kind-24133 gift-wrap style RPC events addressed to
   the ephemeral client pubkey on the configured relays.
3. Flow: connect ack (result must echo the secret) -> get_public_key ->
   sign_event of a NIP-98 (kind 27235) challenge, with a legacy kind 22242
   fallback when the signer refuses to sign 27235.
"""

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from coincurve import PrivateKey, PublicKey
from loguru import logger
from websockets import connect as ws_connect

from lnbits.utils.nostr import (
    decrypt_content,
    encrypt_content,
    sign_event,
)

from .validate import validate_challenge_event, validate_nip98_event

NIP46_KIND = 24133

SESSION_TTL_SECONDS = 300
RELAY_CONNECT_TIMEOUT = 5
SIGN_TIMEOUT_SECONDS = 20
REPUBLISH_INTERVAL = 4
RPC_TIMEOUT_SECONDS = 60


class Nip46Error(Exception):
    pass


class Nip46TimeoutError(Nip46Error):
    pass


def build_connect_uri(
    client_pubkey_hex: str,
    relays: list[str],
    secret: str,
    app_name: str,
    instance_url: str | None = None,
    image_url: str | None = None,
) -> str:
    """Builds a nostrconnect:// pairing URI (NIP-46)."""
    params = [f"relay={quote(relay, safe='')}" for relay in relays]
    params.append(f"secret={quote(secret, safe='')}")
    params.append("perms=sign_event%3A27235,get_public_key")
    params.append(f"name={quote(app_name, safe='')}")
    if instance_url:
        params.append(f"url={quote(instance_url, safe='')}")
    if image_url:
        params.append(f"image={quote(image_url, safe='')}")
    return f"nostrconnect://{client_pubkey_hex}?{'&'.join(params)}"


def _xonly_public_key(hex_key: str) -> PublicKey:
    # Nostr public keys are x-only; coincurve needs a compressed point.
    return PublicKey(bytes.fromhex("02" + hex_key))


def _is_nip04(content: str) -> bool:
    return "?iv=" in content


@dataclass
class Nip46Session:
    id: str
    purpose: str  # "login" or "link"
    connect_uri: str
    client_private_key_hex: str
    secret: str
    nip98_nonce: str
    login_url: str  # absolute URL used for the 'u' tag of the signed event
    binding_nonce_hash: str
    relays: list[str] = field(default_factory=list)
    diagnostic: bool = False
    status: str = "pending"  # pending | approved | failed | auth_required
    user_pubkey: str | None = None
    auth_url: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    task: asyncio.Task | None = field(default=None, repr=False)


class RelayPool:
    """Connects to all relays in parallel and fans kind-24133 events into a queue."""

    def __init__(self, relays: list[str], client_pubkey_hex: str):
        self.relays = relays
        self.client_pubkey_hex = client_pubkey_hex
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self._sub_id = "nostrlogin_" + secrets.token_hex(8)
        self._tasks: list[asyncio.Task] = []
        self._connections: dict[str, object] = {}
        self._seen_event_ids: set[str] = set()
        self._closing = False

    async def start(self) -> None:
        for relay in self.relays:
            task = asyncio.create_task(self._run_relay(relay))
            self._tasks.append(task)

    @property
    def connected_count(self) -> int:
        return len(self._connections)

    async def publish(self, event: dict) -> None:
        payload = json.dumps(["EVENT", event])
        for relay, ws in list(self._connections.items()):
            try:
                await ws.send(payload)  # type: ignore[attr-defined]
            except Exception as e:
                logger.debug(f"Publish to relay {relay} failed: {e!s}")

    async def close(self) -> None:
        self._closing = True
        for task in self._tasks:
            task.cancel()
        self._connections.clear()

    async def _run_relay(self, relay: str) -> None:
        while not self._closing:
            try:
                async with ws_connect(relay, open_timeout=RELAY_CONNECT_TIMEOUT) as ws:
                    if self._closing:
                        return
                    self._connections[relay] = ws
                    subscription = {
                        "kinds": [NIP46_KIND],
                        "#p": [self.client_pubkey_hex],
                        "since": int(time.time()) - 5,
                    }
                    await ws.send(json.dumps(["REQ", self._sub_id, subscription]))
                    async for raw in ws:
                        await self._on_message(raw)
            except Exception as e:
                logger.debug(f"NostrLogin relay {relay}: {e!s}")
            finally:
                self._connections.pop(relay, None)
            if not self._closing:
                await asyncio.sleep(1)

    async def _on_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, list) or len(msg) < 3 or msg[0] != "EVENT":
            return
        event = msg[2]
        if not isinstance(event, dict) or event.get("kind") != NIP46_KIND:
            return
        event_id = event.get("id")
        if not event_id or event_id in self._seen_event_ids:
            return
        self._seen_event_ids.add(event_id)
        p_tags = [
            tag[1] for tag in event.get("tags", []) if tag and tag[0] == "p"
        ]
        if self.client_pubkey_hex not in p_tags:
            return
        await self.queue.put(event)


class NostrLoginService:
    def __init__(self) -> None:
        self._sessions: dict[str, Nip46Session] = {}

    def create_session(
        self,
        *,
        purpose: str,
        relays: list[str],
        binding_nonce_hash: str,
        login_url: str,
        app_name: str,
        instance_url: str | None = None,
        image_url: str | None = None,
        diagnostic: bool = False,
        autostart: bool = True,
    ) -> Nip46Session:
        private_key = PrivateKey()
        session_id = secrets.token_hex(16)
        secret = secrets.token_hex(16)
        uri = build_connect_uri(
            private_key.public_key.format()[1:].hex(),
            relays,
            secret,
            app_name,
            instance_url=instance_url,
            image_url=image_url,
        )
        session = Nip46Session(
            id=session_id,
            purpose=purpose,
            connect_uri=uri,
            client_private_key_hex=private_key.to_hex(),
            secret=secret,
            nip98_nonce=secrets.token_hex(16),
            login_url=login_url,
            binding_nonce_hash=binding_nonce_hash,
            relays=list(relays),
            diagnostic=diagnostic,
        )
        session.task = (
            asyncio.create_task(self._run_session(session)) if autostart else None
        )
        self._sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> Nip46Session | None:
        return self._sessions.get(session_id)

    def remove_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session and session.task and not session.task.done():
            session.task.cancel()

    def remove_expired_sessions(self) -> None:
        now = time.time()
        for session_id in list(self._sessions.keys()):
            session = self._sessions[session_id]
            if now - session.created_at > SESSION_TTL_SECONDS + 60:
                self.remove_session(session_id)

    async def shutdown(self) -> None:
        for session_id in list(self._sessions.keys()):
            self.remove_session(session_id)

    # ------------------------------------------------------------------
    # Session flow

    async def _run_session(self, session: Nip46Session) -> None:
        client_private_key = PrivateKey(bytes.fromhex(session.client_private_key_hex))
        client_pubkey_hex = client_private_key.public_key.format()[1:].hex()
        pool = RelayPool(session.relays, client_pubkey_hex)
        signer_pubkey: str | None = None
        use_nip04 = False
        try:
            await pool.start()
            self._diag(session, f"subscribed on {len(session.relays)} relay(s)")

            deadline = time.time() + SESSION_TTL_SECONDS
            signer_pubkey, use_nip04 = await self._await_connect_ack(
                session, pool, deadline
            )
            self._diag(
                session,
                f"connect ack received from {signer_pubkey} (nip04={use_nip04})",
            )

            user_pubkey = await self._rpc(
                session,
                pool,
                signer_pubkey,
                use_nip04,
                "get_public_key",
                [],
                deadline=deadline,
                timeout_seconds=SIGN_TIMEOUT_SECONDS,
            )
            session.user_pubkey = user_pubkey
            self._diag(session, f"signer user pubkey: {user_pubkey}")

            signed_event = await self._request_signed_event(
                session, pool, signer_pubkey, use_nip04, deadline
            )

            if signed_event.get("kind") == 22242:
                validate_challenge_event(
                    signed_event,
                    expected_pubkey=user_pubkey,
                    expected_nonce=session.nip98_nonce,
                )
            else:
                validate_nip98_event(
                    signed_event,
                    expected_pubkey=user_pubkey,
                    expected_urls=[session.login_url],
                    expected_method="POST",
                    expected_nonce=session.nip98_nonce,
                )
            self._diag(
                session, f"signed event kind {signed_event.get('kind')} validated"
            )
            session.status = "approved"
        except Exception as e:
            logger.warning(f"NostrLogin session {session.id} failed: {e!s}")
            session.status = "failed"
            session.error = str(e)
        finally:
            await pool.close()
            # Best-effort removal of the ephemeral key material.
            del client_private_key

    @staticmethod
    def _diag(session: Nip46Session, message: str) -> None:
        if session.diagnostic:
            logger.info(f"NostrLogin DIAG session {session.id}: {message}")

    async def _request_signed_event(
        self,
        session: Nip46Session,
        pool: RelayPool,
        signer_pubkey: str,
        use_nip04: bool,
        deadline: float,
    ) -> dict:
        unsigned = {
            "pubkey": session.user_pubkey,
            "created_at": int(time.time()),
            "kind": 27235,
            "tags": [
                ["u", session.login_url],
                ["method", "POST"],
                ["challenge", session.nip98_nonce],
            ],
            "content": "",
        }
        try:
            result = await self._rpc(
                session,
                pool,
                signer_pubkey,
                use_nip04,
                "sign_event",
                [json.dumps(unsigned)],
                deadline=deadline,
                timeout_seconds=SIGN_TIMEOUT_SECONDS,
            )
            return _coerce_event(result)
        except Nip46Error:
            # Fallback: legacy kind-22242 challenge signing.
            fallback_deadline = min(deadline, time.time() + SIGN_TIMEOUT_SECONDS)
            result = await self._rpc(
                session,
                pool,
                signer_pubkey,
                use_nip04,
                "sign_event",
                [
                    json.dumps(
                        {
                            "pubkey": session.user_pubkey,
                            "created_at": int(time.time()),
                            "kind": 22242,
                            "tags": [["challenge", session.nip98_nonce]],
                            "content": "",
                        }
                    )
                ],
                deadline=fallback_deadline,
                timeout_seconds=SIGN_TIMEOUT_SECONDS,
            )
            return _coerce_event(result)

    async def _await_connect_ack(
        self, session: Nip46Session, pool: RelayPool, deadline: float
    ) -> tuple[str, bool]:
        """
        Waits for the first response from the signer. The `result` must echo
        the one-time secret (or be "ack" for legacy signers). Returns the
        signer pubkey and whether it speaks NIP-04.
        """
        while time.time() < deadline:
            try:
                event = await asyncio.wait_for(
                    pool.queue.get(), timeout=min(5, max(0.1, deadline - time.time()))
                )
            except asyncio.TimeoutError:
                continue
            author = event.get("pubkey", "")
            content = event.get("content", "")
            try:
                plaintext = self._decrypt(content, author, session)
            except Exception:  # noqa: S112 - untrusted relay traffic
                continue
            try:
                response = json.loads(plaintext)
            except Exception:  # noqa: S112 - untrusted relay traffic
                continue
            use_nip04 = _is_nip04(content)
            result = response.get("result")
            if result == session.secret or result == "ack":
                return author, use_nip04
        raise Nip46TimeoutError("Signer did not approve the connection in time.")

    async def _rpc(  # noqa: C901
        self,
        session: Nip46Session,
        pool: RelayPool,
        signer_pubkey: str,
        use_nip04: bool,
        method: str,
        params: list,
        deadline: float,
        timeout_seconds: int,
    ) -> object:
        request_id = secrets.token_hex(8)
        payload = json.dumps({"id": request_id, "method": method, "params": params})
        event = self._build_request_event(
            session, signer_pubkey, payload, use_nip04
        )
        hard_deadline = min(
            deadline, time.time() + timeout_seconds + REPUBLISH_INTERVAL
        )
        last_publish = 0.0
        while time.time() < hard_deadline:
            if time.time() - last_publish >= REPUBLISH_INTERVAL:
                await pool.publish(event)
                last_publish = time.time()
            remaining = min(hard_deadline - time.time(), REPUBLISH_INTERVAL)
            if remaining <= 0:
                break
            try:
                response_event = await asyncio.wait_for(
                    pool.queue.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                continue
            if response_event.get("pubkey") != signer_pubkey:
                continue
            try:
                plaintext = self._decrypt(
                    response_event.get("content", ""), signer_pubkey, session
                )
                response = json.loads(plaintext)
            except Exception:  # noqa: S112 - untrusted relay traffic
                continue
            if response.get("id") != request_id:
                continue
            error = response.get("error")
            if error:
                if isinstance(error, str) and error.lower().startswith("auth"):
                    # Surface HTTPS-only auth urls to the browser and keep
                    # waiting for the signer to deliver the real response.
                    auth_url = self._extract_auth_url(
                        response.get("result")
                    ) or self._extract_auth_url(response.get("message"))
                    if auth_url:
                        session.auth_url = auth_url
                        session.status = "auth_required"
                        last_publish = 0.0
                        continue
                raise Nip46Error(str(error))
            return response.get("result")
        raise Nip46TimeoutError(f"No response for '{method}' in time.")

    # ------------------------------------------------------------------
    # Crypto helpers

    def _build_request_event(
        self,
        session: Nip46Session,
        signer_pubkey: str,
        payload: str,
        use_nip04: bool,
    ) -> dict:
        private_key = PrivateKey(bytes.fromhex(session.client_private_key_hex))
        client_pubkey_hex = private_key.public_key.format()[1:].hex()
        service_pubkey = _xonly_public_key(signer_pubkey)
        if use_nip04:
            content = encrypt_content(
                payload, service_pubkey, session.client_private_key_hex
            )
        else:
            from lnbits.wallets.nwc import NIP44Encryption

            content = NIP44Encryption.encrypt(
                payload, service_pubkey, session.client_private_key_hex
            )
        event = {
            "created_at": int(time.time()),
            "kind": NIP46_KIND,
            "tags": [["p", signer_pubkey]],
            "content": content,
        }
        return sign_event(event, client_pubkey_hex, private_key)

    def _decrypt(
        self, content: str, author_pubkey: str, session: Nip46Session
    ) -> str:
        service_pubkey = _xonly_public_key(author_pubkey)
        if _is_nip04(content):
            return decrypt_content(
                content, service_pubkey, session.client_private_key_hex
            )
        from lnbits.wallets.nwc import NIP44Encryption

        return NIP44Encryption.decrypt(
            content, service_pubkey, session.client_private_key_hex
        )

    @staticmethod
    def _extract_auth_url(value: object) -> str | None:
        if isinstance(value, str) and value.startswith("https://"):
            return value
        return None


def _coerce_event(result: object) -> dict:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception as exc:
            raise Nip46Error("sign_event returned invalid JSON.") from exc
    if not isinstance(result, dict) or "sig" not in result:
        raise Nip46Error("sign_event did not return a signed event.")
    return result


_service = NostrLoginService()


def get_nostrlogin_service() -> NostrLoginService:
    return _service


def shutdown_service() -> None:
    import asyncio as _asyncio

    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_service.shutdown())
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


def hash_binding_nonce(nonce: bytes) -> str:
    return hashlib.sha256(nonce).hexdigest()
