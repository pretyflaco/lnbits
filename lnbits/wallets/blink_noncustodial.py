"""
Blink non-custodial funding source.

Receives through the public LNURL-pay flow of a Blink Lightning Address
(no Blink API key required) and confirms settlement via LUD-21 verify,
only trusting responses whose preimage hashes to the payment hash.

Optionally sends BOLT11 payments through the Breez Spark SDK when the
account's Spark mnemonic is configured. The mnemonic is spend authority;
the Breez API key is not.

Research: https://github.com/blinkbitcoin/blink-wip/issues/1158
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
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


@dataclass
class _InvoiceMeta:
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    error_streak: int = 0


class BlinkNonCustodialWallet(Wallet):
    """https://github.com/blinkbitcoin/blink-wip/issues/1158"""

    def __init__(self):
        if not settings.blink_noncustodial_ln_address:
            raise ValueError(
                "cannot initialize BlinkNonCustodialWallet: "
                "missing blink_noncustodial_ln_address"
            )

        self.ln_address = self._normalize_ln_address(
            settings.blink_noncustodial_ln_address
        )
        self.username, self.domain = self.ln_address.split("@", 1)
        self.endpoint = (
            settings.blink_noncustodial_lnurl_endpoint or f"https://{self.domain}"
        ).rstrip("/")

        self._has_seed = bool(settings.blink_noncustodial_spark_mnemonic)

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
        elif settings.lnbits_watchdog_switch_to_voidwallet:
            # address-only mode always reports balance 0 which would trigger
            # an incorrect VoidWallet switch on the first watchdog run
            raise ValueError(
                "cannot initialize BlinkNonCustodialWallet in receive-only mode "
                "while lnbits_watchdog_switch_to_voidwallet is enabled: this "
                "funding source has no backend balance to compare against and "
                "would be switched to VoidWallet. Disable the watchdog or "
                "configure a Spark seed."
            )

        self.client = httpx.AsyncClient(
            headers={"User-Agent": f"{settings.user_agent} BlinkNonCustodialWallet"},
            timeout=15,
        )

        self.pending_invoices: list[str] = []
        self._verify_urls: dict[str, str] = {}
        self._invoice_meta: dict[str, _InvoiceMeta] = {}

        self._sdk: Any = None
        self._sdk_lock = asyncio.Lock()
        self._sdk_payment_ids: dict[str, str] = {}  # payment_hash -> sdk payment id

    async def cleanup(self):
        try:
            await self.client.aclose()
        except RuntimeError as e:
            logger.warning(f"Error closing wallet connection: {e}")

        if self._sdk is not None:
            try:
                await asyncio.wait_for(self._disconnect_sdk(), timeout=5)
            except Exception as e:
                logger.warning(f"Error disconnecting Breez Spark SDK: {e}")

    async def status(self) -> StatusResponse:
        if self._has_seed:
            return await self._status_seeded()
        try:
            await self._fetch_lnurlp_metadata()
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
            hint = (
                "configure a Spark seed (signed invoice support) to use "
                "description-hash invoices"
                if self._has_seed
                else "address-only mode cannot commit custom metadata; "
                "configure a Spark seed to enable LNURLp/zaps"
            )
            return InvoiceResponse(
                ok=False,
                error_message=(
                    "Blink non-custodial wallet does not support "
                    "description-hash invoices without signing authority: "
                    f"{hint}"
                ),
            )

        amount_msat = int(amount) * 1000
        try:
            metadata = await self._fetch_lnurlp_metadata()
            callback = self._validate_pay_request(metadata, amount_msat)

            params: dict[str, str | int] = {"amount": amount_msat}
            if kwargs.get("expiry"):
                # supported by blink-lnurl-server but not part of LUD-06,
                # so the returned invoice expiry is validated below regardless
                params["expiry"] = int(kwargs["expiry"])
            if memo:
                params["comment"] = memo

            response = await self.client.get(callback, params=params)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(exc)
            return InvoiceResponse(ok=False, error_message=f"LNURL-pay error: {exc}")

        error_message = self._validate_callback_response(data)
        if error_message:
            return InvoiceResponse(ok=False, error_message=error_message)

        payment_request: str = data["pr"]

        try:
            decoded = bolt11_lib.decode(payment_request)
        except Exception as exc:
            return InvoiceResponse(
                ok=False, error_message=f"Invalid invoice from callback: {exc}"
            )

        error_message = self._validate_callback_invoice(decoded, amount_msat)
        if error_message:
            return InvoiceResponse(ok=False, error_message=error_message)

        payment_hash: str | None = decoded.payment_hash
        expiry_secs = getattr(decoded, "expiry", None)
        if not expiry_secs or expiry_secs <= 0:
            expiry_secs = 3600
        issued_at = getattr(decoded, "date", None) or time.time()
        expires_at = float(issued_at) + float(expiry_secs)

        assert payment_hash is not None
        self._verify_urls[payment_hash] = data["verify"]
        self._invoice_meta[payment_hash] = _InvoiceMeta(expires_at=expires_at)
        self.pending_invoices.append(payment_hash)

        return InvoiceResponse(
            ok=True, checking_id=payment_hash, payment_request=payment_request
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

        try:
            import breez_sdk_spark  # type: ignore[reportMissingImports]
        except ImportError:
            return PaymentResponse(
                ok=False,
                error_message=(
                    "breez-sdk-spark is not installed. "
                    "Ask admin to run `uv sync --extra blink-spark`."
                ),
            )

        try:
            sdk = await self._ensure_sdk()

            prepare_response = await sdk.prepare_send_payment(
                breez_sdk_spark.PrepareSendPaymentRequest(
                    payment_request=breez_sdk_spark.PaymentRequest.INPUT(bolt11)
                )
            )
            fee_sats = self._extract_lightning_fee(prepare_response)
            if fee_sats is None:
                return PaymentResponse(
                    ok=False,
                    error_message="Could not determine lightning fee quote",
                )
            if fee_sats * 1000 > fee_limit_msat:
                return PaymentResponse(
                    ok=False,
                    error_message=(
                        f"Fee quote {fee_sats * 1000} msat exceeds limit "
                        f"{fee_limit_msat} msat"
                    ),
                )

            send_response = await sdk.send_payment(
                breez_sdk_spark.SendPaymentRequest(
                    prepare_response=prepare_response,
                    options=breez_sdk_spark.SendPaymentOptions.BOLT11_INVOICE(
                        prefer_spark=False,
                        completion_timeout_secs=settings.blink_noncustodial_payment_timeout_secs,
                    ),
                    # deterministic idempotency: the SDK requires a valid UUID,
                    # derived from the payment hash so retries of the same
                    # invoice cannot double-spend
                    idempotency_key=str(
                        uuid.UUID(bytes=bytes.fromhex(checking_id)[:16])
                    ),
                )
            )
        except Exception as exc:
            logger.warning(exc)
            return PaymentResponse(
                ok=False, error_message=f"Breez Spark SDK error: {exc}"
            )

        payment = getattr(send_response, "payment", None)
        status = self._map_sdk_status(getattr(payment, "status", None))
        preimage = self._extract_preimage(payment)
        fee_msat = self._extract_fee_msat(payment)

        if payment is not None and getattr(payment, "id", None):
            self._sdk_payment_ids[checking_id] = str(payment.id)

        if (
            status is True
            and preimage
            and not self._preimage_matches(preimage, checking_id)
        ):
            # never trust a success without a preimage proving the hash
            logger.warning(
                f"Spark payment {checking_id} reported paid with invalid preimage"
            )
            return PaymentResponse(ok=None, checking_id=checking_id)

        return PaymentResponse(
            ok=status,
            checking_id=checking_id,
            fee_msat=fee_msat,
            preimage=preimage,
        )

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

        self._invoice_meta.setdefault(checking_id, _InvoiceMeta()).error_streak = 0

        settled = data.get("settled") is True
        preimage = data.get("preimage")
        if not settled:
            return PaymentPendingStatus()
        if not preimage or not self._preimage_matches(preimage, checking_id):
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

        try:
            sdk = await self._ensure_sdk()
        except Exception as exc:
            logger.warning(exc)
            return PaymentPendingStatus()

        payment = await self._find_sdk_payment(sdk, checking_id)
        if payment is None:
            return PaymentPendingStatus()

        status = self._map_sdk_status(getattr(payment, "status", None))
        preimage = self._extract_preimage(payment)
        if (
            status is True
            and preimage
            and not self._preimage_matches(preimage, checking_id)
        ):
            logger.warning(
                f"Spark payment {checking_id} completed with invalid preimage"
            )
            return PaymentPendingStatus()
        return PaymentStatus(
            paid=status,
            fee_msat=self._extract_fee_msat(payment),
            preimage=preimage,
        )

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        last_poll: dict[str, float] = {}
        while settings.lnbits_running:
            now = time.time()
            next_wake = now + POLL_INTERVAL_NEW_SECS
            for checking_id in list(self.pending_invoices):
                meta = self._invoice_meta.setdefault(checking_id, _InvoiceMeta())

                if meta.expires_at and now > meta.expires_at + EXPIRY_GRACE_SECS:
                    self._evict_invoice(checking_id, reason="expired")
                    continue

                interval = self._poll_interval(now, meta.created_at, meta.error_streak)
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
                elif status.failed:
                    logger.warning(f"invoice {checking_id} failed, evicting")
                    self._evict_invoice(checking_id, reason="failed")
            await asyncio.sleep(max(0.5, next_wake - time.time()))

    async def _fetch_lnurlp_metadata(self) -> dict:
        response = await self.client.get(
            f"{self.endpoint}/.well-known/lnurlp/{self.username}"
        )
        response.raise_for_status()
        data = response.json()
        if data.get("tag") != "payRequest":
            raise ValueError(f"unexpected LNURLp tag: '{data.get('tag')}'")
        return data

    def _validate_pay_request(self, metadata: dict, amount_msat: int) -> str:
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

    @staticmethod
    def _validate_callback_response(data: dict) -> str | None:
        if not data.get("pr"):
            return "LNURL-pay callback returned no invoice"
        verify_url = data.get("verify")
        if not verify_url or not isinstance(verify_url, str):
            return "LNURL-pay callback returned no LUD-21 verify URL"
        return None

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
        age = now - created_at
        if age < POLL_INTERVAL_AGE_THRESHOLD_SECS:
            base = POLL_INTERVAL_NEW_SECS
        elif age < POLL_INTERVAL_OLD_THRESHOLD_SECS:
            base = POLL_INTERVAL_AGING_SECS
        else:
            base = POLL_INTERVAL_OLD_SECS
        backoff = min(error_streak, 4)
        return min(base * (2**backoff), POLL_BACKOFF_MAX_SECS)

    def _register_verify_error(
        self, checking_id: str, detail: Any, hard: bool = True
    ) -> PaymentStatus:
        meta = self._invoice_meta.setdefault(checking_id, _InvoiceMeta())
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

    def _evict_invoice(self, checking_id: str, reason: str) -> None:
        if checking_id in self.pending_invoices:
            self.pending_invoices.remove(checking_id)
        self._verify_urls.pop(checking_id, None)
        self._invoice_meta.pop(checking_id, None)
        logger.debug(f"evicted invoice {checking_id} ({reason})")

    @staticmethod
    def _normalize_ln_address(address: str) -> str:
        address = address.strip().lower()
        if "@" in address:
            username, domain = address.split("@", 1)
        else:
            username, domain = address, "blink.sv"
        if not username or "." not in domain:
            raise ValueError(
                f"invalid Blink Lightning Address: '{address}' "
                "(expected user@blink.sv)"
            )
        return f"{username}@{domain}"

    @staticmethod
    def _preimage_matches(preimage: str, checking_id: str) -> bool:
        try:
            preimage_bytes = bytes.fromhex(preimage)
        except ValueError:
            return False
        if len(preimage_bytes) != 32:
            return False
        return hashlib.sha256(preimage_bytes).hexdigest() == checking_id

    @staticmethod
    def _extract_lightning_fee(prepare_response: Any) -> int | None:
        # SendPaymentMethod.BOLT11_INVOICE carries lightning_fee_sats
        fee_sats = getattr(prepare_response, "payment_method", None)
        return getattr(fee_sats, "lightning_fee_sats", None)

    @staticmethod
    def _extract_preimage(payment: Any) -> str | None:
        details = getattr(payment, "details", None)
        htlc = getattr(details, "htlc_details", None)
        preimage = getattr(htlc, "preimage", None)
        if preimage is None:
            preimage = getattr(payment, "preimage", None)
        return str(preimage) if preimage else None

    @staticmethod
    def _extract_fee_msat(payment: Any) -> int | None:
        fees = getattr(payment, "fees", None)
        if fees is None:
            fees_sat = getattr(payment, "fees_sat", None)
            return int(fees_sat) * 1000 if fees_sat is not None else None
        try:
            return int(fees)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _map_sdk_status(status: Any) -> bool | None:
        # PaymentStatus enum values, matched defensively by name
        name = getattr(status, "name", None) or (
            status if isinstance(status, str) else None
        )
        states = {
            "COMPLETED": True,
            "FAILED": False,
        }
        if isinstance(name, str):
            return states.get(name.upper())
        return None

    # --- Breez Spark SDK lifecycle (seeded mode only) ---

    async def _ensure_sdk(self) -> Any:
        assert settings.blink_noncustodial_spark_mnemonic is not None
        if self._sdk is not None:
            return self._sdk
        async with self._sdk_lock:
            if self._sdk is not None:
                return self._sdk
            import breez_sdk_spark  # type: ignore[reportMissingImports]

            mnemonic = settings.blink_noncustodial_spark_mnemonic
            storage_dir = Path(
                settings.lnbits_data_folder,
                "blink-spark",
                hashlib.sha256(mnemonic.encode()).hexdigest()[:16],
            )
            storage_dir.mkdir(parents=True, exist_ok=True)

            config = breez_sdk_spark.default_config(breez_sdk_spark.Network.MAINNET)
            config.api_key = settings.blink_noncustodial_breez_api_key
            connect_request = breez_sdk_spark.ConnectRequest(
                config=config,
                seed=breez_sdk_spark.Seed.MNEMONIC(
                    mnemonic=mnemonic,
                    passphrase=None,
                ),
                storage_dir=storage_dir.as_posix(),
            )
            # never log the mnemonic
            self._sdk = await breez_sdk_spark.connect(connect_request)
            logger.info("Breez Spark SDK initialized.")
            return self._sdk

    async def _disconnect_sdk(self) -> None:
        if self._sdk is not None:
            await self._sdk.disconnect()
            self._sdk = None

    async def _find_sdk_payment(self, sdk: Any, checking_id: str) -> Any | None:
        sdk_payment_id = self._sdk_payment_ids.get(checking_id)
        if sdk_payment_id:
            try:
                import breez_sdk_spark  # type: ignore[reportMissingImports]

                return await sdk.get_payment(
                    breez_sdk_spark.GetPaymentRequest(payment_id=sdk_payment_id)
                )
            except Exception as exc:
                logger.warning(f"could not get spark payment by id: {exc}")
        try:
            import breez_sdk_spark  # type: ignore[reportMissingImports]

            payments = await sdk.list_payments(breez_sdk_spark.ListPaymentsRequest())
        except Exception as exc:
            logger.warning(f"could not list spark payments: {exc}")
            return None
        for payment in payments:
            htlc_hash = self._extract_htlc_hash(payment)
            if htlc_hash == checking_id and getattr(payment, "id", None):
                self._sdk_payment_ids[checking_id] = str(payment.id)
                return payment
        return None

    @staticmethod
    def _extract_htlc_hash(payment: Any) -> str | None:
        details = getattr(payment, "details", None)
        htlc = getattr(details, "htlc_details", None)
        htlc_hash = getattr(htlc, "payment_hash", None)
        if htlc_hash is None:
            return None
        htlc_hash = str(htlc_hash)
        return htlc_hash[2:] if htlc_hash.startswith("0x") else htlc_hash

    async def _status_seeded(self) -> StatusResponse:
        try:
            sdk = await self._ensure_sdk()
            import breez_sdk_spark  # type: ignore[reportMissingImports]

            info = await sdk.get_info(
                breez_sdk_spark.GetInfoRequest(ensure_synced=True)
            )
            balance_sats = getattr(info, "balance_sats", 0)
            return StatusResponse(None, int(balance_sats or 0) * 1000)
        except Exception as exc:
            logger.warning(exc)
            return StatusResponse(f"Breez Spark SDK error: {exc}", 0)
