import hashlib
import os
import time
from types import SimpleNamespace

import pytest

from lnbits.settings import settings
from lnbits.wallets.blink_noncustodial import (
    BlinkNonCustodialWallet,
    _InvoiceMeta,
)


def make_wallet(monkeypatch, **overrides):
    monkeypatch.setattr(
        settings, "blink_noncustodial_ln_address", overrides.pop("ln_address", "hanzy")
    )
    monkeypatch.setattr(
        settings,
        "blink_noncustodial_lnurl_endpoint",
        overrides.pop("lnurl_endpoint", None),
    )
    monkeypatch.setattr(
        settings, "blink_noncustodial_spark_mnemonic", overrides.pop("seed", None)
    )
    monkeypatch.setattr(
        settings,
        "blink_noncustodial_mnemonic_backup_confirmed",
        overrides.pop("backup", False),
    )
    monkeypatch.setattr(
        settings,
        "lnbits_watchdog_switch_to_voidwallet",
        overrides.pop("watchdog", False),
    )
    return BlinkNonCustodialWallet()


def fake_decoded(
    currency="bc",
    amount_msat=100_000,
    payment_hash=None,
    expiry=3600,
):
    return SimpleNamespace(
        currency=currency,
        amount_msat=amount_msat,
        payment_hash=payment_hash or "a" * 64,
        expiry=expiry,
        date=time.time(),
    )


def pay_request_metadata(min_sendable=1000, max_sendable=10_000_000_000):
    return {
        "tag": "payRequest",
        "callback": "https://blink.sv/lnurlp/hanzy/invoice",
        "minSendable": min_sendable,
        "maxSendable": max_sendable,
    }


def mock_http_response(json_data=None, status_code=200):
    resp = SimpleNamespace(status_code=status_code)

    def raise_for_status():
        if resp.status_code >= 400:
            from httpx import HTTPStatusError

            raise HTTPStatusError(
                f"HTTP {resp.status_code}", request=None, response=None
            )

    resp.raise_for_status = raise_for_status
    if isinstance(json_data, Exception):
        resp.json = lambda: (_ for _ in ()).throw(json_data)
    else:
        resp.json = lambda: json_data
    return resp


def settle_response(checking_id, preimage_hex):
    return {
        "status": "OK",
        "settled": True,
        "preimage": preimage_hex,
        "pr": "lnbc...",
    }


def make_preimage_pair():
    """returns (checking_id, preimage_hex) where sha256(preimage) == checking_id"""
    preimage = os.urandom(32)
    return hashlib.sha256(preimage).hexdigest(), preimage.hex()


# --- configuration / initialization ---


def test_normalize_ln_address_bare_username(monkeypatch):
    wallet = make_wallet(monkeypatch, ln_address="hanzy")
    assert wallet.ln_address == "hanzy@blink.sv"
    assert wallet.username == "hanzy"
    assert wallet.domain == "blink.sv"
    assert wallet.endpoint == "https://blink.sv"


def test_normalize_ln_address_full_address(monkeypatch):
    wallet = make_wallet(monkeypatch, ln_address="Hanzy@Blink.SV")
    assert wallet.ln_address == "hanzy@blink.sv"


def test_invalid_ln_address_rejected(monkeypatch):
    with pytest.raises(ValueError):
        make_wallet(monkeypatch, ln_address="@bad")


def test_missing_ln_address_rejected(monkeypatch):
    monkeypatch.setattr(settings, "blink_noncustodial_ln_address", None)
    with pytest.raises(ValueError):
        BlinkNonCustodialWallet()


def test_receive_only_mode_rejects_watchdog_voidwallet_switch(monkeypatch):
    with pytest.raises(ValueError) as excinfo:
        make_wallet(monkeypatch, watchdog=True)
    assert "watchdog" in str(excinfo.value)


def test_seed_requires_backup_confirmation(monkeypatch):
    monkeypatch.setattr("lnbits.wallets.blink_noncustodial.HAS_BREEZ_SPARK_SDK", True)
    with pytest.raises(ValueError) as excinfo:
        make_wallet(monkeypatch, seed="word " * 12)
    assert "backup" in str(excinfo.value)


def test_seed_without_sdk_package_rejected(monkeypatch):
    # breez-sdk-spark is not installed in the unit-test environment
    from lnbits.wallets.blink_noncustodial import HAS_BREEZ_SPARK_SDK

    if HAS_BREEZ_SPARK_SDK:
        pytest.skip("breez-sdk-spark is installed")
    with pytest.raises(ValueError) as excinfo:
        make_wallet(monkeypatch, seed="word " * 12, backup=True)
    assert "breez-sdk-spark" in str(excinfo.value)


# --- description-hash fail fast ---


@pytest.mark.anyio
async def test_description_hash_fails_fast_in_address_only_mode(monkeypatch):
    wallet = make_wallet(monkeypatch)
    response = await wallet.create_invoice(1000, description_hash=b"\x01" * 32)
    assert response.ok is False
    assert "description-hash" in (response.error_message or "")


@pytest.mark.anyio
async def test_unhashed_description_fails_fast_in_address_only_mode(monkeypatch):
    wallet = make_wallet(monkeypatch)
    response = await wallet.create_invoice(1000, unhashed_description=b"metadata")
    assert response.ok is False


# --- invoice creation through LNURL-pay ---


@pytest.mark.anyio
async def test_create_invoice_success(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    decoded = fake_decoded()
    mocker.patch(
        "lnbits.wallets.blink_noncustodial.bolt11_lib.decode", return_value=decoded
    )

    def route(url, params=None):
        if "invoice" in url:
            return mock_http_response(
                {"pr": "lnbc...", "verify": "https://blink.sv/v/a"}
            )
        return mock_http_response(pay_request_metadata())

    mocker.patch.object(wallet.client, "get", side_effect=route)

    response = await wallet.create_invoice(100, memo="test")
    assert response.ok is True
    assert response.checking_id == decoded.payment_hash
    assert response.payment_request == "lnbc..."
    assert wallet._verify_urls[decoded.payment_hash] == "https://blink.sv/v/a"
    assert decoded.payment_hash in wallet.pending_invoices


@pytest.mark.anyio
async def test_create_invoice_amount_below_minimum(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    mocker.patch.object(
        wallet.client,
        "get",
        side_effect=lambda url, params=None: mock_http_response(
            pay_request_metadata(min_sendable=1000)
        ),
    )
    response = await wallet.create_invoice(amount=0)
    assert response.ok is False
    assert "minimum" in (response.error_message or "") or "below" in (
        response.error_message or ""
    )


@pytest.mark.anyio
async def test_create_invoice_amount_above_maximum(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    mocker.patch.object(
        wallet.client,
        "get",
        side_effect=lambda url, params=None: mock_http_response(
            pay_request_metadata(max_sendable=50_000)
        ),
    )
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "maximum" in (response.error_message or "") or "above" in (
        response.error_message or ""
    )


@pytest.mark.anyio
async def test_create_invoice_rejects_http_callback(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    metadata = pay_request_metadata()
    metadata["callback"] = "http://evil.example/callback"

    async def fake_get(url, params=None):
        return mock_http_response(metadata)

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "https" in (response.error_message or "")


@pytest.mark.anyio
async def test_create_invoice_rejects_foreign_callback_host(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    metadata = pay_request_metadata()
    metadata["callback"] = "https://evil.example/callback"

    async def fake_get(url, params=None):
        return mock_http_response(metadata)

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "not allowed" in (response.error_message or "")


@pytest.mark.anyio
async def test_create_invoice_rejects_wrong_tag(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)

    async def fake_get(url, params=None):
        return mock_http_response({"tag": "withdrawRequest"})

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False


@pytest.mark.anyio
async def test_create_invoice_rejects_missing_verify_url(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    decoded = fake_decoded()
    mocker.patch(
        "lnbits.wallets.blink_noncustodial.bolt11_lib.decode", return_value=decoded
    )

    def route(url, params=None):
        if "invoice" in url:
            return mock_http_response({"pr": "lnbc..."})
        return mock_http_response(pay_request_metadata())

    mocker.patch.object(wallet.client, "get", side_effect=route)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "verify" in (response.error_message or "")


@pytest.mark.anyio
async def test_create_invoice_rejects_non_mainnet_invoice(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    decoded = fake_decoded(currency="tb")
    mocker.patch(
        "lnbits.wallets.blink_noncustodial.bolt11_lib.decode", return_value=decoded
    )

    def route(url, params=None):
        if "invoice" in url:
            return mock_http_response({"pr": "lntb...", "verify": "https://x/v"})
        return mock_http_response(pay_request_metadata())

    mocker.patch.object(wallet.client, "get", side_effect=route)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "mainnet" in (response.error_message or "")


@pytest.mark.anyio
async def test_create_invoice_rejects_amount_mismatch(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    decoded = fake_decoded(amount_msat=999)
    mocker.patch(
        "lnbits.wallets.blink_noncustodial.bolt11_lib.decode", return_value=decoded
    )

    def route(url, params=None):
        if "invoice" in url:
            return mock_http_response({"pr": "lnbc...", "verify": "https://x/v"})
        return mock_http_response(pay_request_metadata())

    mocker.patch.object(wallet.client, "get", side_effect=route)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "does not match" in (response.error_message or "")


# --- LUD-21 settlement verification ---


@pytest.mark.anyio
async def test_invoice_paid_with_valid_preimage(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    checking_id, preimage = make_preimage_pair()
    wallet._verify_urls[checking_id] = "https://blink.sv/v/a"

    async def fake_get(url, params=None):
        return mock_http_response(settle_response(checking_id, preimage))

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    status = await wallet.get_invoice_status(checking_id)
    assert status.paid is True
    assert status.preimage == preimage


@pytest.mark.anyio
async def test_invoice_with_invalid_preimage_stays_pending(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    checking_id = "c" * 64
    wallet._verify_urls[checking_id] = "https://blink.sv/v/a"

    async def fake_get(url, params=None):
        return mock_http_response(settle_response(checking_id, "ff" * 32))

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    status = await wallet.get_invoice_status(checking_id)
    assert status.paid is None


@pytest.mark.anyio
async def test_unsettled_invoice_stays_pending(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    checking_id = "d" * 64
    wallet._verify_urls[checking_id] = "https://blink.sv/v/a"

    async def fake_get(url, params=None):
        return mock_http_response(
            {"status": "OK", "settled": False, "preimage": "", "pr": "lnbc..."}
        )

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    status = await wallet.get_invoice_status(checking_id)
    assert status.paid is None


@pytest.mark.anyio
async def test_transport_error_stays_pending(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    checking_id = "e" * 64
    wallet._verify_urls[checking_id] = "https://blink.sv/v/a"
    wallet._invoice_meta[checking_id] = _InvoiceMeta()

    async def fake_get(url, params=None):
        raise ConnectionError("boom")

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    for _ in range(5):
        status = await wallet.get_invoice_status(checking_id)
        assert status.paid is None
    # transport failures back off but never evict as failed
    assert wallet._invoice_meta[checking_id].error_streak == 5


@pytest.mark.anyio
async def test_repeated_error_responses_evict(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    checking_id = "f" * 64
    wallet._verify_urls[checking_id] = "https://blink.sv/v/a"
    wallet._invoice_meta[checking_id] = _InvoiceMeta()

    async def fake_get(url, params=None):
        return mock_http_response({"status": "ERROR", "reason": "unknown invoice"})

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    for _ in range(2):
        status = await wallet.get_invoice_status(checking_id)
        assert status.paid is None
    status = await wallet.get_invoice_status(checking_id)
    assert status.paid is False


@pytest.mark.anyio
async def test_unknown_checking_id_is_pending(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    status = await wallet.get_invoice_status("9" * 64)
    assert status.paid is None


# --- send guardrails ---


@pytest.mark.anyio
async def test_pay_invoice_unsupported_in_address_only_mode(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    response = await wallet.pay_invoice("lnbc...", fee_limit_msat=1000)
    assert response.ok is False
    assert "not supported" in (response.error_message or "").lower()


@pytest.mark.anyio
async def test_payment_status_pending_in_address_only_mode(monkeypatch, mocker):
    wallet = make_wallet(monkeypatch)
    status = await wallet.get_payment_status("a" * 64)
    assert status.paid is None


# --- helpers ---


def test_preimage_matches_roundtrip():
    checking_id, preimage = make_preimage_pair()
    assert BlinkNonCustodialWallet._preimage_matches(preimage, checking_id)
    assert not BlinkNonCustodialWallet._preimage_matches("zz", checking_id)
    assert not BlinkNonCustodialWallet._preimage_matches("ab" * 16, checking_id)


def test_poll_interval_age_stepping():
    now = time.time()
    created = now - 30
    assert BlinkNonCustodialWallet._poll_interval(now, created, 0) == 4
    created = now - 300
    assert BlinkNonCustodialWallet._poll_interval(now, created, 0) == 15
    created = now - 3600
    assert BlinkNonCustodialWallet._poll_interval(now, created, 0) == 60


def test_poll_interval_backoff_and_cap():
    now = time.time()
    created = now - 3600
    assert BlinkNonCustodialWallet._poll_interval(now, created, 2) == 240
    assert BlinkNonCustodialWallet._poll_interval(now, created, 8) <= 300


def test_map_sdk_status():
    assert BlinkNonCustodialWallet._map_sdk_status("COMPLETED") is True
    assert BlinkNonCustodialWallet._map_sdk_status("FAILED") is False
    assert BlinkNonCustodialWallet._map_sdk_status("PENDING") is None
    assert BlinkNonCustodialWallet._map_sdk_status(None) is None
