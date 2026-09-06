"""
Blink non-custodial funding source.

Receives through the public LNURL-pay flow of a Blink Lightning Address
(no Blink API key required) and confirms settlement via LUD-21 verify,
only trusting responses whose preimage hashes to the payment hash.

Optionally sends BOLT11 payments through the Breez Spark SDK when the
account's Spark mnemonic is configured. The mnemonic is spend authority;
the Breez API key is not.

The wallet is a thin orchestrator over a few injectable collaborators:
- `LnUrlPayClient` — LNURL-pay plumbing (metadata fetch, validation)
- `InvoiceSigner` (port) — signs D1 invoice requests; `GrantKeySigner` or
  `SparkSdkSigner` implement it
- `SignedInvoiceMinter` — D1/D2 signed-invoice requests committing to a
  caller-chosen description hash
- `PendingInvoiceTracker` — LUD-21 verify tracking + eviction + persistence
- `SendCapability` (port) — `SparkSendCapability` (seeded) or
  `NoSendCapability` (address-only)
- `SparkSdkAdapter` — the single place that touches the Breez Spark SDK

Research: https://github.com/blinkbitcoin/blink-wip/issues/1158
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx
from bolt11 import TagChar
from loguru import logger

from lnbits import bolt11 as bolt11_lib
from lnbits.settings import settings

from .base import (
    InvoiceResponse,
    PaymentFailedStatus,
    PaymentPendingStatus,
    PaymentResponse,
    PaymentStatus,
    PaymentSuccessStatus,
    StatusResponse,
    Wallet,
)

HAS_BREEZ_SPARK_SDK = find_spec("breez_sdk_spark") is not None

# LUD-21 verify polling limits (see blink-wip#1123 for the incident that
# motivates bounded, backed-off polling).
POLL_INTERVAL_NEW_SECS = 4
POLL_INTERVAL_AGE_THRESHOLD_SECS = 60
POLL_INTERVAL_AGING_SECS = 15
POLL_INTERVAL_OLD_THRESHOLD_SECS = 600
POLL_INTERVAL_OLD_SECS = 60
POLL_BACKOFF_MAX_SECS = 300
POLL_JITTER_FRACTION = 0.2
# consecutive confirmed ERROR/not-found verify responses before giving up
VERIFY_ERROR_EVICTION_THRESHOLD = 3
# keep polling past invoice expiry for this long before evicting silently
EXPIRY_GRACE_SECS = 120

DEFAULT_INVOICE_EXPIRY_SECS = 3600


def preimage_matches(preimage: str, checking_id: str) -> bool:
    """A settlement is only trusted if the preimage hashes to the hash."""
    try:
        preimage_bytes = bytes.fromhex(preimage)
    except ValueError:
        return False
    if len(preimage_bytes) != 32:
        return False
    return hashlib.sha256(preimage_bytes).hexdigest() == checking_id


def normalize_ln_address(address: str) -> str:
    address = address.strip().lower()
    if "@" in address:
        username, domain = address.split("@", 1)
    else:
        username, domain = address, "blink.sv"
    if not username or "." not in domain:
        raise ValueError(
            f"invalid Blink Lightning Address: '{address}' (expected user@blink.sv)"
        )
    return f"{username}@{domain}"


def map_sdk_status(status: Any) -> bool | None:
    """PaymentStatus enum -> bool|None, matched defensively by name."""
    name = getattr(status, "name", None) or (
        status if isinstance(status, str) else None
    )
    states = {"COMPLETED": True, "FAILED": False}
    if isinstance(name, str):
        return states.get(name.upper())
    return None


def poll_interval(now: float, created_at: float, error_streak: int) -> float:
    age = now - created_at
    if age < POLL_INTERVAL_AGE_THRESHOLD_SECS:
        base = POLL_INTERVAL_NEW_SECS
    elif age < POLL_INTERVAL_OLD_THRESHOLD_SECS:
        base = POLL_INTERVAL_AGING_SECS
    else:
        base = POLL_INTERVAL_OLD_SECS
    backoff = min(error_streak, 4)
    return min(base * (2**backoff), POLL_BACKOFF_MAX_SECS)


# --- Spark SDK adapter (DIP): the ONLY module that touches breez_sdk_spark ---


@dataclass
class SparkPaymentInfo:
    """Typed view of a Spark SDK payment (isolates duck-typing)."""

    status: bool | None
    preimage: str | None
    fee_msat: int | None
    sdk_payment_id: str | None
    htlc_hash: str | None


class SparkSdkAdapter:
    """Wraps the Breez Spark SDK behind a narrow typed interface.

    All `breez_sdk_spark` attribute access lives here; the rest of the wallet
    never duck-types SDK objects. Connects lazily on first use.
    """

    def __init__(self, mnemonic: str, api_key: str | None, data_folder: str):
        self._mnemonic = mnemonic
        self._api_key = api_key
        self._data_folder = data_folder
        self._sdk: Any = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _require_package() -> None:
        if not HAS_BREEZ_SPARK_SDK:
            raise RuntimeError(
                "breez-sdk-spark is not installed. "
                "Ask admin to run `uv sync --extra blink-spark`."
            )

    async def ensure(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        async with self._lock:
            if self._sdk is not None:
                return self._sdk
            self._require_package()
            import breez_sdk_spark  # type: ignore[reportMissingImports]

            storage_dir = Path(
                self._data_folder,
                "blink-spark",
                hashlib.sha256(self._mnemonic.encode()).hexdigest()[:16],
            )
            storage_dir.mkdir(parents=True, exist_ok=True)
            config = breez_sdk_spark.default_config(breez_sdk_spark.Network.MAINNET)
            config.api_key = self._api_key
            connect_request = breez_sdk_spark.ConnectRequest(
                config=config,
                seed=breez_sdk_spark.Seed.MNEMONIC(
                    mnemonic=self._mnemonic, passphrase=None
                ),
                storage_dir=storage_dir.as_posix(),
            )
            # never log the mnemonic
            self._sdk = await breez_sdk_spark.connect(connect_request)
            logger.info("Breez Spark SDK initialized.")
            return self._sdk

    async def disconnect(self) -> None:
        if self._sdk is not None:
            await self._sdk.disconnect()
            self._sdk = None

    async def sign_message(self, message: str) -> tuple[str, str]:
        """Returns (pubkey, signature)."""
        sdk = await self.ensure()
        import breez_sdk_spark  # type: ignore[reportMissingImports]

        signed = await sdk.sign_message(
            breez_sdk_spark.SignMessageRequest(message=message, compact=False)
        )
        return signed.pubkey, signed.signature

    async def balance_sats(self) -> int:
        sdk = await self.ensure()
        import breez_sdk_spark  # type: ignore[reportMissingImports]

        info = await sdk.get_info(breez_sdk_spark.GetInfoRequest(ensure_synced=True))
        return int(getattr(info, "balance_sats", 0) or 0)

    async def send_bolt11(
        self, bolt11: str, checking_id: str, fee_limit_msat: int
    ) -> tuple[bool | None, str | None, int | None, str | None]:
        """Returns (status, preimage, fee_msat, error). Enforces the fee limit
        and preimage verification here so callers get a typed result."""
        import breez_sdk_spark  # type: ignore[reportMissingImports]

        sdk = await self.ensure()
        prepare_response = await sdk.prepare_send_payment(
            breez_sdk_spark.PrepareSendPaymentRequest(
                payment_request=breez_sdk_spark.PaymentRequest.INPUT(bolt11)
            )
        )
        fee_sats = getattr(
            getattr(prepare_response, "payment_method", None),
            "lightning_fee_sats",
            None,
        )
        if fee_sats is None:
            return None, None, None, "Could not determine lightning fee quote"
        if fee_sats * 1000 > fee_limit_msat:
            return (
                None,
                None,
                None,
                f"Fee quote {fee_sats * 1000} msat exceeds limit {fee_limit_msat} msat",
            )
        send_response = await sdk.send_payment(
            breez_sdk_spark.SendPaymentRequest(
                prepare_response=prepare_response,
                options=breez_sdk_spark.SendPaymentOptions.BOLT11_INVOICE(
                    prefer_spark=False,
                    completion_timeout_secs=settings.blink_noncustodial_payment_timeout_secs,
                ),
                # deterministic idempotency derived from the payment hash so
                # retries of the same invoice cannot double-spend
                idempotency_key=str(uuid.UUID(bytes=bytes.fromhex(checking_id)[:16])),
            )
        )
        info = self._payment_info(getattr(send_response, "payment", None))
        return info.status, info.preimage, info.fee_msat, None

    async def find_payment(self, checking_id: str) -> SparkPaymentInfo | None:
        sdk = await self.ensure()
        import breez_sdk_spark  # type: ignore[reportMissingImports]

        try:
            response = await sdk.list_payments(breez_sdk_spark.ListPaymentsRequest())
        except Exception as exc:
            logger.warning(f"could not list spark payments: {exc}")
            return None
        # list_payments returns a ListPaymentsResponse wrapper, not a list
        for payment in getattr(response, "payments", None) or []:
            info = self._payment_info(payment)
            if info.htlc_hash == checking_id:
                return info
        return None

    # --- duck-typing containment (private) ---

    @staticmethod
    def _payment_info(payment: Any) -> SparkPaymentInfo:
        if payment is None:
            return SparkPaymentInfo(None, None, None, None, None)
        details = getattr(payment, "details", None)
        htlc = getattr(details, "htlc_details", None)
        preimage = getattr(htlc, "preimage", None)
        if preimage is None:
            preimage = getattr(payment, "preimage", None)
        htlc_hash = getattr(htlc, "payment_hash", None)
        fees = getattr(payment, "fees", None)
        if fees is None:
            fees_sat = getattr(payment, "fees_sat", None)
            fee_msat = int(fees_sat) * 1000 if fees_sat is not None else None
        else:
            try:
                fee_msat = int(fees)
            except (TypeError, ValueError):
                fee_msat = None
        sdk_payment_id = getattr(payment, "id", None)
        return SparkPaymentInfo(
            status=map_sdk_status(getattr(payment, "status", None)),
            preimage=str(preimage) if preimage else None,
            fee_msat=fee_msat,
            sdk_payment_id=str(sdk_payment_id) if sdk_payment_id else None,
            htlc_hash=(
                str(htlc_hash)[2:]
                if isinstance(htlc_hash, str) and htlc_hash.startswith("0x")
                else (str(htlc_hash) if htlc_hash else None)
            ),
        )


# --- Invoice signing (DIP): how a D1 request gets signed ---


@runtime_checkable
class InvoiceSigner(Protocol):
    """Signs D1 invoice requests. Implementations supply the authority."""

    pubkey: str

    async def sign_invoice_request(self, message: str) -> str:
        """Returns the DER-hex signature over sha256(message)."""
        ...


class GrantKeySigner:
    """D2: plain coincurve ECDSA with the delegated grant key (no SDK)."""

    def __init__(self, privkey_hex: str):
        from coincurve import PrivateKey

        key_bytes = bytes.fromhex(privkey_hex.strip())
        if len(key_bytes) != 32:
            raise ValueError("must be 32 bytes")
        self._key = PrivateKey(key_bytes)
        self.pubkey = self._key.public_key.format(compressed=True).hex()

    async def sign_invoice_request(self, message: str) -> str:
        digest = hashlib.sha256(message.encode()).digest()
        return self._key.sign(digest, hasher=None).hex()


class SparkSdkSigner:
    """D1 with full authority: the Spark identity key via the Breez SDK."""

    def __init__(self, sdk: SparkSdkAdapter):
        self._sdk = sdk
        self.pubkey = ""  # resolved at sign time by the SDK

    async def sign_invoice_request(self, message: str) -> str:
        pubkey, signature = await self._sdk.sign_message(message)
        self.pubkey = pubkey
        return signature


# --- Send capability (OCP): how outgoing payments happen, if at all ---


@runtime_checkable
class SendCapability(Protocol):
    async def pay(
        self, bolt11: str, checking_id: str, fee_limit_msat: int
    ) -> PaymentResponse: ...

    async def payment_status(self, checking_id: str) -> PaymentStatus: ...

    async def status(self) -> StatusResponse: ...


class NoSendCapability:
    """Address-only mode: no spend authority."""

    async def pay(
        self, bolt11: str, checking_id: str, fee_limit_msat: int
    ) -> PaymentResponse:
        return PaymentResponse(
            ok=False,
            error_message=(
                "Sending is not supported by the Blink non-custodial wallet "
                "in address-only mode."
            ),
        )

    async def payment_status(self, checking_id: str) -> PaymentStatus:
        return PaymentPendingStatus()

    async def status(self) -> StatusResponse:
        return StatusResponse(None, 0)


class SparkSendCapability:
    """Seeded mode: send + status via the Spark SDK adapter."""

    def __init__(self, sdk: SparkSdkAdapter):
        self._sdk = sdk

    async def pay(
        self, bolt11: str, checking_id: str, fee_limit_msat: int
    ) -> PaymentResponse:
        try:
            status, preimage, fee_msat, error = await self._sdk.send_bolt11(
                bolt11, checking_id, fee_limit_msat
            )
        except Exception as exc:
            logger.warning(exc)
            return PaymentResponse(
                ok=False, error_message=f"Breez Spark SDK error: {exc}"
            )
        if error is not None:
            return PaymentResponse(ok=False, error_message=error)
        if status is True and preimage and not preimage_matches(preimage, checking_id):
            logger.warning(
                f"Spark payment {checking_id} reported paid with invalid preimage"
            )
            return PaymentResponse(ok=None, checking_id=checking_id)
        return PaymentResponse(
            ok=status, checking_id=checking_id, fee_msat=fee_msat, preimage=preimage
        )

    async def payment_status(self, checking_id: str) -> PaymentStatus:
        try:
            info = await self._sdk.find_payment(checking_id)
        except Exception as exc:
            logger.warning(exc)
            return PaymentPendingStatus()
        if info is None:
            return PaymentPendingStatus()
        if (
            info.status is True
            and info.preimage
            and not preimage_matches(info.preimage, checking_id)
        ):
            logger.warning(
                f"Spark payment {checking_id} completed with invalid preimage"
            )
            return PaymentPendingStatus()
        return PaymentStatus(
            paid=info.status, fee_msat=info.fee_msat, preimage=info.preimage
        )

    async def status(self) -> StatusResponse:
        try:
            balance_sats = await self._sdk.balance_sats()
            return StatusResponse(None, balance_sats * 1000)
        except Exception as exc:
            logger.warning(exc)
            return StatusResponse(f"Breez Spark SDK error: {exc}", 0)


# --- LNURL-pay client (SRP): metadata + request validation ---


class LnUrlPayClient:
    """Fetches and validates LNURL-pay metadata/callback responses for a
    Blink Lightning Address. Owns the host allowlist for callbacks/verify."""

    def __init__(
        self, client: httpx.AsyncClient, endpoint: str, username: str, domain: str
    ):
        self._client = client
        self.endpoint = endpoint
        self.username = username
        self.domain = domain
        self._allowed_hosts = {
            domain.lower(),
            (urlparse(endpoint).hostname or "").lower(),
        } - {""}

    async def fetch_metadata(self) -> dict:
        response = await self._client.get(
            f"{self.endpoint}/.well-known/lnurlp/{self.username}"
        )
        response.raise_for_status()
        data = response.json()
        if data.get("tag") != "payRequest":
            raise ValueError(f"unexpected LNURLp tag: '{data.get('tag')}'")
        return data

    def validate_pay_request(self, metadata: dict, amount_msat: int) -> str:
        min_sendable = int(metadata.get("minSendable", 0))
        max_sendable = int(metadata.get("maxSendable", 0))
        if amount_msat < min_sendable:
            raise ValueError(
                f"amount {amount_msat} msat below minimum {min_sendable} msat"
            )
        if max_sendable and amount_msat > max_sendable:
            raise ValueError(
                f"amount {amount_msat} msat above maximum {max_sendable} msat"
            )
        callback = metadata.get("callback")
        if not isinstance(callback, str) or not callback:
            raise ValueError("missing callback URL")
        parsed = urlparse(callback)
        if parsed.scheme != "https":
            raise ValueError("callback URL must be https")
        # allowlist: only accept callbacks served by the address's own domain
        # or a subdomain of it (e.g. lnurl.blink.sv for user@blink.sv)
        host = parsed.netloc.lower()
        if host != self.domain.lower() and not host.endswith(f".{self.domain.lower()}"):
            raise ValueError(f"callback host '{parsed.netloc}' is not allowed")
        return callback

    def validate_callback_response(self, data: dict) -> str | None:
        if not data.get("pr"):
            return "LNURL-pay callback returned no invoice"
        verify_url = data.get("verify")
        if not verify_url or not isinstance(verify_url, str):
            return "LNURL-pay callback returned no LUD-21 verify URL"
        return self.validate_verify_url(verify_url)

    def validate_verify_url(self, verify_url: str) -> str | None:
        # the verify URL drives settlement decisions, so it gets the same
        # scrutiny as the callback URL: https only, and only hosts belonging
        # to the address domain or the configured endpoint
        parsed = urlparse(verify_url)
        if parsed.scheme != "https":
            return "LUD-21 verify URL must be https"
        host = (parsed.hostname or "").lower()
        if not host:
            return "LUD-21 verify URL has no host"
        for allowed in self._allowed_hosts:
            if host == allowed or host.endswith(f".{allowed}"):
                return None
        return f"LUD-21 verify host '{host}' is not allowed"


# --- Signed invoice minter (SRP): D1/D2 signed description-hash invoices ---


class SignedInvoiceMinter:
    """Mints invoices via the fork's `POST /lnurlp/{id}/invoice/signed`,
    signing the canonical message with an injected `InvoiceSigner`."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        username: str,
        domain: str,
        signer: InvoiceSigner,
        lnurl: LnUrlPayClient,
    ):
        self._client = client
        self._endpoint = endpoint
        self._username = username
        self._domain = domain
        self._signer = signer
        self._lnurl = lnurl

    async def mint(
        self,
        amount: int,
        description_hash: bytes | None,
        unhashed_description: bytes | None,
        expiry: int,
    ) -> tuple[dict, str, int] | InvoiceResponse:
        """Returns (callback_data, desc_hash_hex, amount_msat) or an error."""
        ts = int(time.time())
        request_id = uuid.uuid4().hex
        desc_hash_hex = (
            description_hash.hex()
            if description_hash
            else hashlib.sha256(unhashed_description or b"").hexdigest()
        )
        amount_msat = int(amount) * 1000
        # one canonical builder for both signing paths: the server verifies
        # this exact byte string, drift between paths would break auth
        canonical = (
            f"lnurl-invoice-v1:{self._domain}:{self._username}:{amount_msat}:"
            f"{desc_hash_hex}:{expiry}:{request_id}"
        )
        try:
            signature = await self._signer.sign_invoice_request(f"{canonical}-{ts}")
        except Exception as exc:
            logger.warning(exc)
            return InvoiceResponse(
                ok=False, error_message=f"signed invoice error: {exc}"
            )
        pubkey = self._signer.pubkey
        body = {
            "amount_msat": amount_msat,
            "description_hash": desc_hash_hex,
            "expiry_secs": expiry,
            "request_id": request_id,
            "pubkey": pubkey,
            "timestamp": ts,
            "signature": signature,
        }
        try:
            response = await self._client.post(
                f"{self._endpoint}/lnurlp/{self._username}/invoice/signed",
                json=body,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(exc)
            return InvoiceResponse(
                ok=False, error_message=f"signed invoice error: {exc}"
            )
        error_message = self._lnurl.validate_callback_response(data)
        if error_message:
            return InvoiceResponse(ok=False, error_message=error_message)
        return data, desc_hash_hex, amount_msat


# --- Pending invoice tracker (SRP): LUD-21 verify tracking + persistence ---


@dataclass
class _InvoiceMeta:
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    error_streak: int = 0


class PendingInvoiceTracker:
    """Owns the LUD-21 verify state (pending hashes, verify URLs, error
    streaks), eviction, and disk persistence across restarts.

    Verify URLs cannot be guessed, so without persistence an invoice paid
    while LNbits is down would stay pending forever even though the funds
    arrived.
    """

    def __init__(self, store_path: Path):
        self._store_path = store_path
        self.pending_invoices: list[str] = []
        self._verify_urls: dict[str, str] = {}
        self._invoice_meta: dict[str, _InvoiceMeta] = {}
        self._load()

    # --- state accessors (kept as the wallet's single source of truth) ---

    def verify_url_for(self, payment_hash: str) -> str | None:
        return self._verify_urls.get(payment_hash)

    def register(self, payment_hash: str, verify_url: str, expires_at: float) -> None:
        self._verify_urls[payment_hash] = verify_url
        self._invoice_meta[payment_hash] = _InvoiceMeta(expires_at=expires_at)
        if payment_hash not in self.pending_invoices:
            self.pending_invoices.append(payment_hash)
        self.persist()

    def meta(self, payment_hash: str) -> _InvoiceMeta:
        return self._invoice_meta.setdefault(payment_hash, _InvoiceMeta())

    def clear_error(self, payment_hash: str) -> None:
        self.meta(payment_hash).error_streak = 0

    def register_error(
        self, checking_id: str, detail: Any, hard: bool = True
    ) -> PaymentStatus:
        meta = self.meta(checking_id)
        meta.error_streak += 1
        # only confirmed ERROR/not-found responses evict; transport and parse
        # failures just back off until the invoice expires
        if hard and meta.error_streak >= VERIFY_ERROR_EVICTION_THRESHOLD:
            logger.warning(
                f"LUD-21 verify for {checking_id} failed repeatedly "
                f"(last status: '{detail}'), giving up"
            )
            return PaymentFailedStatus()
        return PaymentPendingStatus()

    def evict(self, checking_id: str, reason: str) -> None:
        if checking_id in self.pending_invoices:
            self.pending_invoices.remove(checking_id)
        self._verify_urls.pop(checking_id, None)
        self._invoice_meta.pop(checking_id, None)
        self.persist()
        logger.debug(f"evicted invoice {checking_id} ({reason})")

    # --- persistence ---

    def _load(self) -> None:
        try:
            raw = self._store_path.read_text()
        except (FileNotFoundError, OSError):
            return
        try:
            stored = json.loads(raw)
            now = time.time()
            for payment_hash, entry in stored.items():
                expires_at = float(entry.get("expires_at", 0))
                if expires_at and now > expires_at + EXPIRY_GRACE_SECS:
                    continue  # long gone; the poller would evict anyway
                verify_url = entry.get("verify")
                if not isinstance(verify_url, str) or not verify_url:
                    continue
                self._verify_urls[payment_hash] = verify_url
                self._invoice_meta[payment_hash] = _InvoiceMeta(
                    created_at=float(entry.get("created_at", now)),
                    expires_at=expires_at,
                )
                self.pending_invoices.append(payment_hash)
        except Exception as exc:
            # never let a corrupt state file take down the funding source
            logger.warning(f"ignoring corrupt pending-invoice store: {exc}")
            self._verify_urls.clear()
            self._invoice_meta.clear()
            self.pending_invoices.clear()

    def persist(self) -> None:
        try:
            data = {
                h: {
                    "verify": self._verify_urls[h],
                    "expires_at": self._invoice_meta[h].expires_at,
                    "created_at": self._invoice_meta[h].created_at,
                }
                for h in self.pending_invoices
                if h in self._verify_urls and h in self._invoice_meta
            }
            tmp = self._store_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self._store_path)
        except OSError as exc:
            logger.warning(f"could not persist pending invoices: {exc}")


# --- The wallet: a thin orchestrator over the collaborators above ---


class BlinkNonCustodialWallet(Wallet):
    """https://github.com/blinkbitcoin/blink-wip/issues/1158"""

    def __init__(self):
        if not settings.blink_noncustodial_ln_address:
            raise ValueError(
                "cannot initialize BlinkNonCustodialWallet: "
                "missing blink_noncustodial_ln_address"
            )

        self.ln_address = normalize_ln_address(settings.blink_noncustodial_ln_address)
        self.username, self.domain = self.ln_address.split("@", 1)
        self.endpoint = (
            settings.blink_noncustodial_lnurl_endpoint or f"https://{self.domain}"
        ).rstrip("/")

        self._has_seed = bool(settings.blink_noncustodial_spark_mnemonic)

        self.client = httpx.AsyncClient(
            headers={"User-Agent": f"{settings.user_agent} BlinkNonCustodialWallet"},
            timeout=15,
        )

        self._lnurl = LnUrlPayClient(
            self.client, self.endpoint, self.username, self.domain
        )
        # expose the allowlist for backwards-compat introspection/tests
        self._allowed_hosts = self._lnurl._allowed_hosts

        # --- mode selection (once, here): signer + send capability ---
        self._sdk: SparkSdkAdapter | None = None
        self._signer: InvoiceSigner | None = None
        self._grant_key = None  # kept for introspection/backwards-compat
        self._grant_pubkey: str | None = None

        grant_privkey_hex = settings.blink_noncustodial_grant_privkey
        if grant_privkey_hex:
            try:
                signer = GrantKeySigner(grant_privkey_hex)
                self._signer = signer
                self._grant_key = signer._key
                self._grant_pubkey = signer.pubkey
            except Exception as exc:
                raise ValueError(
                    "cannot initialize BlinkNonCustodialWallet: invalid "
                    f"blink_noncustodial_grant_privkey: {exc}"
                ) from exc

        if self._has_seed:
            if not HAS_BREEZ_SPARK_SDK:
                raise ValueError(
                    "cannot initialize BlinkNonCustodialWallet: seed is configured "
                    "but the breez-sdk-spark package is not installed. "
                    "Ask admin to run `uv sync --extra blink-spark` to install it."
                )
            if not settings.blink_noncustodial_mnemonic_backup_confirmed:
                raise ValueError(
                    "cannot initialize BlinkNonCustodialWallet: the Spark seed "
                    "controls funds, confirm you have backed it up with "
                    "blink_noncustodial_mnemonic_backup_confirmed=true"
                )
            if not settings.blink_noncustodial_breez_api_key:
                raise ValueError(
                    "cannot initialize BlinkNonCustodialWallet: missing "
                    "blink_noncustodial_breez_api_key required by the "
                    "Breez Spark SDK"
                )
            self._sdk = SparkSdkAdapter(
                settings.blink_noncustodial_spark_mnemonic,
                settings.blink_noncustodial_breez_api_key,
                settings.lnbits_data_folder,
            )
            # grant key takes precedence for description-hash signing when present
            if self._signer is None:
                self._signer = SparkSdkSigner(self._sdk)
            self._send: SendCapability = SparkSendCapability(self._sdk)
        else:
            if settings.lnbits_watchdog_switch_to_voidwallet:
                # address-only mode always reports balance 0 which would trigger
                # an incorrect VoidWallet switch on the first watchdog run
                raise ValueError(
                    "cannot initialize BlinkNonCustodialWallet in receive-only mode "
                    "while lnbits_watchdog_switch_to_voidwallet is enabled: this "
                    "funding source has no backend balance to compare against and "
                    "would be switched to VoidWallet. Disable the watchdog or "
                    "configure a Spark seed."
                )
            self._send = NoSendCapability()

        self._minter = (
            SignedInvoiceMinter(
                self.client,
                self.endpoint,
                self.username,
                self.domain,
                self._signer,
                self._lnurl,
            )
            if self._signer is not None
            else None
        )

        self._tracker = PendingInvoiceTracker(
            Path(settings.lnbits_data_folder, "blink-noncustodial-pending.json")
        )

        self._sdk_payment_ids: dict[str, str] = {}  # payment_hash -> sdk payment id

    # --- state accessors exposed for the existing call sites / tests ---

    @property
    def pending_invoices(self) -> list[str]:
        return self._tracker.pending_invoices

    @pending_invoices.setter
    def pending_invoices(self, value: list[str]) -> None:
        self._tracker.pending_invoices = value

    @property
    def _verify_urls(self) -> dict[str, str]:
        return self._tracker._verify_urls

    @_verify_urls.setter
    def _verify_urls(self, value: dict[str, str]) -> None:
        self._tracker._verify_urls = value

    @property
    def _invoice_meta(self) -> dict[str, _InvoiceMeta]:
        return self._tracker._invoice_meta

    @_invoice_meta.setter
    def _invoice_meta(self, value: dict[str, _InvoiceMeta]) -> None:
        self._tracker._invoice_meta = value

    # persistence / eviction forwards (kept as methods for test compat)
    def _load_pending(self) -> None:
        self._tracker._load()

    def _persist_pending(self) -> None:
        self._tracker.persist()

    def _register_pending(
        self, payment_hash: str, verify_url: str, expires_at: float
    ) -> None:
        self._tracker.register(payment_hash, verify_url, expires_at)

    def _register_verify_error(
        self, checking_id: str, detail: Any, hard: bool = True
    ) -> PaymentStatus:
        return self._tracker.register_error(checking_id, detail, hard)

    def _evict_invoice(self, checking_id: str, reason: str) -> None:
        self._tracker.evict(checking_id, reason)

    # --- Wallet ABC ---

    async def cleanup(self):
        try:
            await self.client.aclose()
        except RuntimeError as e:
            logger.warning(f"Error closing wallet connection: {e}")
        if self._sdk is not None:
            try:
                await asyncio.wait_for(self._sdk.disconnect(), timeout=5)
            except Exception as e:
                logger.warning(f"Error disconnecting Breez Spark SDK: {e}")

    async def status(self) -> StatusResponse:
        if self._has_seed:
            return await self._send.status()
        try:
            await self._lnurl.fetch_metadata()
            # no public balance exists for a non-custodial Blink account
            return StatusResponse(None, 0)
        except Exception as exc:
            logger.warning(exc)
            return StatusResponse(
                f"Blink non-custodial LNURL health check failed: '{exc}'", 0
            )

    async def create_invoice(
        self,
        amount: int,
        memo: str | None = None,
        description_hash: bytes | None = None,
        unhashed_description: bytes | None = None,
        **kwargs,
    ) -> InvoiceResponse:
        if description_hash or unhashed_description:
            if self._minter is None:
                return InvoiceResponse(
                    ok=False,
                    error_message=(
                        "Blink non-custodial wallet does not support "
                        "description-hash invoices without signing authority: "
                        "configure a Spark seed or a delegated receive grant "
                        "(blink_noncustodial_grant_privkey) to enable "
                        "LNURLp/zaps"
                    ),
                )
            return await self._create_signed_description_hash_invoice(
                amount=amount,
                description_hash=description_hash,
                unhashed_description=unhashed_description,
                **kwargs,
            )

        amount_msat = int(amount) * 1000
        try:
            metadata = await self._lnurl.fetch_metadata()
            callback = self._lnurl.validate_pay_request(metadata, amount_msat)

            params: dict[str, str | int] = {"amount": amount_msat}
            if kwargs.get("expiry"):
                # supported by blink-lnurl-server but not part of LUD-06;
                # the server is free to round or clamp it, so polling uses
                # the expiry decoded from the returned invoice instead
                params["expiry"] = int(kwargs["expiry"])
            if memo:
                params["comment"] = memo

            response = await self.client.get(callback, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(exc)
            return InvoiceResponse(ok=False, error_message=f"LNURL-pay error: {exc}")

        error_message = self._lnurl.validate_callback_response(data)
        if error_message:
            return InvoiceResponse(ok=False, error_message=error_message)

        return self._finalize_invoice(data["pr"], data["verify"], amount_msat)

    def _finalize_invoice(
        self,
        payment_request: str,
        verify_url: str,
        amount_msat: int,
        expected_desc_hash: str | None = None,
    ) -> InvoiceResponse:
        """Validate a minted invoice and register it for settlement polling."""
        try:
            decoded = bolt11_lib.decode(payment_request)
        except Exception as exc:
            return InvoiceResponse(
                ok=False, error_message=f"Invalid invoice from callback: {exc}"
            )

        error_message = self._validate_callback_invoice(decoded, amount_msat)
        if error_message:
            return InvoiceResponse(ok=False, error_message=error_message)

        if expected_desc_hash is not None:
            # the whole point of D1: the invoice must commit to OUR hash
            desc_tag = decoded.tags.get(TagChar.description_hash)
            invoice_desc_hash = getattr(desc_tag, "data", None)
            if invoice_desc_hash != expected_desc_hash:
                return InvoiceResponse(
                    ok=False,
                    error_message=(
                        "server returned invoice with wrong description hash "
                        f"({invoice_desc_hash})"
                    ),
                )

        payment_hash: str | None = decoded.payment_hash
        expiry_secs = getattr(decoded, "expiry", None)
        if not expiry_secs or expiry_secs <= 0:
            expiry_secs = DEFAULT_INVOICE_EXPIRY_SECS
        issued_at = getattr(decoded, "date", None) or time.time()
        expires_at = float(issued_at) + float(expiry_secs)

        assert payment_hash is not None
        self._register_pending(payment_hash, verify_url, expires_at)

        return InvoiceResponse(
            ok=True, checking_id=payment_hash, payment_request=payment_request
        )

    async def _create_signed_description_hash_invoice(
        self,
        amount: int,
        description_hash: bytes | None = None,
        unhashed_description: bytes | None = None,
        **kwargs,
    ) -> InvoiceResponse:
        """Signs a D1/D2 invoice request committing to the caller-chosen
        description hash via the injected minter/signer."""
        assert self._minter is not None
        result = await self._minter.mint(
            amount=amount,
            description_hash=description_hash,
            unhashed_description=unhashed_description,
            expiry=int(kwargs.get("expiry") or DEFAULT_INVOICE_EXPIRY_SECS),
        )
        if isinstance(result, InvoiceResponse):
            return result
        data, desc_hash_hex, amount_msat = result
        return self._finalize_invoice(
            data["pr"], data["verify"], amount_msat, expected_desc_hash=desc_hash_hex
        )

    # kept as a public hook for any external caller (delegates to the minter)
    async def _build_signed_invoice_request(
        self,
        amount: int,
        description_hash: bytes | None,
        unhashed_description: bytes | None,
        expiry: int,
    ) -> tuple[dict, str, int] | InvoiceResponse:
        assert self._minter is not None
        return await self._minter.mint(
            amount, description_hash, unhashed_description, expiry
        )

    async def pay_invoice(self, bolt11: str, fee_limit_msat: int) -> PaymentResponse:
        if not self._has_seed:
            return PaymentResponse(
                ok=False,
                error_message=(
                    "Sending is not supported by the Blink non-custodial wallet "
                    "in address-only mode."
                ),
            )
        try:
            decoded = bolt11_lib.decode(bolt11)
            checking_id = decoded.payment_hash
        except Exception as exc:
            return PaymentResponse(ok=False, error_message=f"Invalid invoice: {exc}")
        if not checking_id:
            return PaymentResponse(ok=False, error_message="Invoice has no hash")
        return await self._send.pay(bolt11, checking_id, fee_limit_msat)

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        verify_url = self._verify_urls.get(checking_id)
        if not verify_url:
            return PaymentPendingStatus()

        try:
            response = await self.client.get(verify_url)
        except Exception as exc:
            logger.warning(f"LUD-21 verify request failed for {checking_id}: {exc}")
            self._register_verify_error(checking_id, "transport error", hard=False)
            return PaymentPendingStatus()

        if response.status_code == 404:
            return self._register_verify_error(checking_id, "not found")

        try:
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(f"LUD-21 verify parse failed for {checking_id}: {exc}")
            self._register_verify_error(checking_id, "malformed response", hard=False)
            return PaymentPendingStatus()

        if data.get("status") != "OK":
            return self._register_verify_error(checking_id, data.get("status"))

        self._tracker.clear_error(checking_id)

        settled = data.get("settled") is True
        preimage = data.get("preimage")
        if not settled:
            return PaymentPendingStatus()
        if not preimage or not preimage_matches(preimage, checking_id):
            # settled without a valid preimage must never be reported paid
            logger.warning(
                f"LUD-21 verify reports settled without valid preimage "
                f"for {checking_id}, keeping pending"
            )
            return PaymentPendingStatus()
        return PaymentSuccessStatus(preimage=preimage)

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        if not self._has_seed:
            return PaymentPendingStatus()
        return await self._send.payment_status(checking_id)

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        last_poll: dict[str, float] = {}
        while settings.lnbits_running:
            now = time.time()
            next_wake = now + POLL_INTERVAL_NEW_SECS
            for checking_id in list(self.pending_invoices):
                meta = self._tracker.meta(checking_id)

                if meta.expires_at and now > meta.expires_at + EXPIRY_GRACE_SECS:
                    self._evict_invoice(checking_id, reason="expired")
                    last_poll.pop(checking_id, None)
                    continue

                interval = poll_interval(now, meta.created_at, meta.error_streak)
                due = last_poll.get(checking_id, 0.0) + interval
                if due > now:
                    next_wake = min(next_wake, due)
                    continue

                # one shared loop keeps the global request rate bounded
                last_poll[checking_id] = now + random.uniform(  # noqa: S311
                    0, interval * POLL_JITTER_FRACTION
                )
                try:
                    status = await self.get_invoice_status(checking_id)
                except Exception as exc:
                    meta.error_streak += 1
                    logger.error(
                        f"could not get status of invoice {checking_id}: '{exc}'"
                    )
                    continue

                if status.paid:
                    yield checking_id
                    self._evict_invoice(checking_id, reason="paid")
                    last_poll.pop(checking_id, None)
                elif status.failed:
                    logger.warning(f"invoice {checking_id} failed, evicting")
                    self._evict_invoice(checking_id, reason="failed")
                    last_poll.pop(checking_id, None)
            await asyncio.sleep(max(0.5, next_wake - time.time()))

    # --- static helpers kept on the class (referenced by tests & call sites) ---

    async def _fetch_lnurlp_metadata(self) -> dict:
        return await self._lnurl.fetch_metadata()

    def _validate_pay_request(self, metadata: dict, amount_msat: int) -> str:
        return self._lnurl.validate_pay_request(metadata, amount_msat)

    def _validate_callback_response(self, data: dict) -> str | None:
        return self._lnurl.validate_callback_response(data)

    def _validate_verify_url(self, verify_url: str) -> str | None:
        return self._lnurl.validate_verify_url(verify_url)

    @staticmethod
    def _validate_callback_invoice(decoded: Any, amount_msat: int) -> str | None:
        if decoded.currency != "bc":
            return f"Expected mainnet invoice, got '{decoded.currency}'"
        if not decoded.amount_msat or decoded.amount_msat != amount_msat:
            return (
                f"Invoice amount {decoded.amount_msat} msat does not match "
                f"requested {amount_msat} msat"
            )
        if not decoded.payment_hash:
            return "Invoice has no hash"
        return None

    @staticmethod
    def _poll_interval(now: float, created_at: float, error_streak: int) -> float:
        return poll_interval(now, created_at, error_streak)

    @staticmethod
    def _normalize_ln_address(address: str) -> str:
        return normalize_ln_address(address)

    @staticmethod
    def _preimage_matches(preimage: str, checking_id: str) -> bool:
        return preimage_matches(preimage, checking_id)

    @staticmethod
    def _map_sdk_status(status: Any) -> bool | None:
        return map_sdk_status(status)

    # --- deprecated internal forwards (kept to avoid breaking introspection) ---

    async def _ensure_sdk(self) -> Any:
        assert self._sdk is not None
        return await self._sdk.ensure()

    async def _disconnect_sdk(self) -> None:
        if self._sdk is not None:
            await self._sdk.disconnect()

    async def _find_sdk_payment(self, sdk: Any, checking_id: str) -> Any | None:
        assert self._sdk is not None
        return await self._sdk.find_payment(checking_id)

    async def _status_seeded(self) -> StatusResponse:
        return await self._send.status()
