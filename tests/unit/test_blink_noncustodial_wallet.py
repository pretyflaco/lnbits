import hashlib
import os
import time
from types import SimpleNamespace

import pytest

from lnbits.settings import settings
from lnbits.wallets.blink_noncustodial import (
    BlinkNonCustodialWallet,
    GrantKeySigner,
    InvoiceSigner,
    NoSendCapability,
    PendingInvoiceTracker,
    SignedInvoiceMinter,
    SparkSdkAdapter,
    SparkSdkSigner,
    _InvoiceMeta,
)


def make_wallet(monkeypatch, tmp_path, **overrides):
    monkeypatch.setattr(settings, "lnbits_data_folder", str(tmp_path))
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


def test_normalize_ln_address_bare_username(monkeypatch, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path, ln_address="hanzy")
    assert wallet.ln_address == "hanzy@blink.sv"
    assert wallet.username == "hanzy"
    assert wallet.domain == "blink.sv"
    assert wallet.endpoint == "https://blink.sv"


def test_normalize_ln_address_full_address(monkeypatch, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path, ln_address="Hanzy@Blink.SV")
    assert wallet.ln_address == "hanzy@blink.sv"


def test_invalid_ln_address_rejected(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        make_wallet(monkeypatch, tmp_path, ln_address="@bad")


def test_missing_ln_address_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "blink_noncustodial_ln_address", None)
    with pytest.raises(ValueError):
        BlinkNonCustodialWallet()


def test_receive_only_mode_rejects_watchdog_voidwallet_switch(monkeypatch, tmp_path):
    with pytest.raises(ValueError) as excinfo:
        make_wallet(monkeypatch, tmp_path, watchdog=True)
    assert "watchdog" in str(excinfo.value)


def test_seed_requires_backup_confirmation(monkeypatch, tmp_path):
    monkeypatch.setattr("lnbits.wallets.blink_noncustodial.HAS_BREEZ_SPARK_SDK", True)
    with pytest.raises(ValueError) as excinfo:
        make_wallet(monkeypatch, tmp_path, seed="word " * 12)
    assert "backup" in str(excinfo.value)


def test_seed_without_sdk_package_rejected(monkeypatch, tmp_path):
    # breez-sdk-spark is not installed in the unit-test environment
    from lnbits.wallets.blink_noncustodial import HAS_BREEZ_SPARK_SDK

    if HAS_BREEZ_SPARK_SDK:
        pytest.skip("breez-sdk-spark is installed")
    with pytest.raises(ValueError) as excinfo:
        make_wallet(monkeypatch, tmp_path, seed="word " * 12, backup=True)
    assert "breez-sdk-spark" in str(excinfo.value)


# --- description-hash fail fast ---


@pytest.mark.anyio
async def test_description_hash_fails_fast_in_address_only_mode(monkeypatch, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
    response = await wallet.create_invoice(1000, description_hash=b"\x01" * 32)
    assert response.ok is False
    assert "description-hash" in (response.error_message or "")


@pytest.mark.anyio
async def test_unhashed_description_fails_fast_in_address_only_mode(
    monkeypatch, tmp_path
):
    wallet = make_wallet(monkeypatch, tmp_path)
    response = await wallet.create_invoice(1000, unhashed_description=b"metadata")
    assert response.ok is False


# --- invoice creation through LNURL-pay ---


@pytest.mark.anyio
async def test_create_invoice_success(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
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
async def test_create_invoice_amount_below_minimum(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
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
async def test_create_invoice_amount_above_maximum(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
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
async def test_create_invoice_rejects_http_callback(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
    metadata = pay_request_metadata()
    metadata["callback"] = "http://evil.example/callback"

    async def fake_get(url, params=None):
        return mock_http_response(metadata)

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "https" in (response.error_message or "")


@pytest.mark.anyio
async def test_create_invoice_rejects_foreign_callback_host(
    monkeypatch, mocker, tmp_path
):
    wallet = make_wallet(monkeypatch, tmp_path)
    metadata = pay_request_metadata()
    metadata["callback"] = "https://evil.example/callback"

    async def fake_get(url, params=None):
        return mock_http_response(metadata)

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "not allowed" in (response.error_message or "")


@pytest.mark.anyio
async def test_create_invoice_accepts_subdomain_callback(monkeypatch, mocker, tmp_path):
    """blink.sv serves callbacks from lnurl.blink.sv - must be allowed"""
    wallet = make_wallet(monkeypatch, tmp_path)
    decoded = fake_decoded()
    mocker.patch(
        "lnbits.wallets.blink_noncustodial.bolt11_lib.decode", return_value=decoded
    )
    metadata = pay_request_metadata()
    metadata["callback"] = "https://lnurl.blink.sv/lnurlp/hanzy/invoice"

    def route(url, params=None):
        if "invoice" in url:
            return mock_http_response(
                {"pr": "lnbc...", "verify": "https://lnurl.blink.sv/v/a"}
            )
        return mock_http_response(metadata)

    mocker.patch.object(wallet.client, "get", side_effect=route)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is True


@pytest.mark.anyio
async def test_create_invoice_rejects_lookalike_host(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
    metadata = pay_request_metadata()
    metadata["callback"] = "https://evilblink.sv/callback"

    async def fake_get(url, params=None):
        return mock_http_response(metadata)

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "not allowed" in (response.error_message or "")


@pytest.mark.anyio
async def test_create_invoice_rejects_wrong_tag(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)

    async def fake_get(url, params=None):
        return mock_http_response({"tag": "withdrawRequest"})

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False


@pytest.mark.anyio
async def test_create_invoice_rejects_missing_verify_url(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
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
async def test_create_invoice_rejects_non_mainnet_invoice(
    monkeypatch, mocker, tmp_path
):
    wallet = make_wallet(monkeypatch, tmp_path)
    decoded = fake_decoded(currency="tb")
    mocker.patch(
        "lnbits.wallets.blink_noncustodial.bolt11_lib.decode", return_value=decoded
    )

    def route(url, params=None):
        if "invoice" in url:
            return mock_http_response(
                {"pr": "lntb...", "verify": "https://blink.sv/v/a"}
            )
        return mock_http_response(pay_request_metadata())

    mocker.patch.object(wallet.client, "get", side_effect=route)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "mainnet" in (response.error_message or "")


@pytest.mark.anyio
async def test_create_invoice_rejects_amount_mismatch(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
    decoded = fake_decoded(amount_msat=999)
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
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "does not match" in (response.error_message or "")


# --- LUD-21 settlement verification ---


@pytest.mark.anyio
async def test_invoice_paid_with_valid_preimage(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
    checking_id, preimage = make_preimage_pair()
    wallet._verify_urls[checking_id] = "https://blink.sv/v/a"

    async def fake_get(url, params=None):
        return mock_http_response(settle_response(checking_id, preimage))

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    status = await wallet.get_invoice_status(checking_id)
    assert status.paid is True
    assert status.preimage == preimage


@pytest.mark.anyio
async def test_invoice_with_invalid_preimage_stays_pending(
    monkeypatch, mocker, tmp_path
):
    wallet = make_wallet(monkeypatch, tmp_path)
    checking_id = "c" * 64
    wallet._verify_urls[checking_id] = "https://blink.sv/v/a"

    async def fake_get(url, params=None):
        return mock_http_response(settle_response(checking_id, "ff" * 32))

    mocker.patch.object(wallet.client, "get", side_effect=fake_get)
    status = await wallet.get_invoice_status(checking_id)
    assert status.paid is None


@pytest.mark.anyio
async def test_unsettled_invoice_stays_pending(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
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
async def test_transport_error_stays_pending(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
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
async def test_repeated_error_responses_evict(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
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
async def test_unknown_checking_id_is_pending(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
    status = await wallet.get_invoice_status("9" * 64)
    assert status.paid is None


# --- send guardrails ---


@pytest.mark.anyio
async def test_pay_invoice_unsupported_in_address_only_mode(
    monkeypatch, mocker, tmp_path
):
    wallet = make_wallet(monkeypatch, tmp_path)
    response = await wallet.pay_invoice("lnbc...", fee_limit_msat=1000)
    assert response.ok is False
    assert "not supported" in (response.error_message or "").lower()


@pytest.mark.anyio
async def test_payment_status_pending_in_address_only_mode(
    monkeypatch, mocker, tmp_path
):
    wallet = make_wallet(monkeypatch, tmp_path)
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


# --- verify URL validation ---


@pytest.mark.anyio
async def test_create_invoice_rejects_http_verify_url(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
    decoded = fake_decoded()
    mocker.patch(
        "lnbits.wallets.blink_noncustodial.bolt11_lib.decode", return_value=decoded
    )

    def route(url, params=None):
        if "invoice" in url:
            return mock_http_response(
                {"pr": "lnbc...", "verify": "http://blink.sv/v/a"}
            )
        return mock_http_response(pay_request_metadata())

    mocker.patch.object(wallet.client, "get", side_effect=route)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "https" in (response.error_message or "")


@pytest.mark.anyio
async def test_create_invoice_rejects_foreign_verify_host(
    monkeypatch, mocker, tmp_path
):
    wallet = make_wallet(monkeypatch, tmp_path)
    decoded = fake_decoded()
    mocker.patch(
        "lnbits.wallets.blink_noncustodial.bolt11_lib.decode", return_value=decoded
    )

    def route(url, params=None):
        if "invoice" in url:
            return mock_http_response(
                {"pr": "lnbc...", "verify": "https://evil.example/v/a"}
            )
        return mock_http_response(pay_request_metadata())

    mocker.patch.object(wallet.client, "get", side_effect=route)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "not allowed" in (response.error_message or "")


@pytest.mark.anyio
async def test_create_invoice_rejects_lookalike_verify_host(
    monkeypatch, mocker, tmp_path
):
    wallet = make_wallet(monkeypatch, tmp_path)
    decoded = fake_decoded()
    mocker.patch(
        "lnbits.wallets.blink_noncustodial.bolt11_lib.decode", return_value=decoded
    )

    def route(url, params=None):
        if "invoice" in url:
            return mock_http_response(
                {"pr": "lnbc...", "verify": "https://blink.sv.evil.example/v/a"}
            )
        return mock_http_response(pay_request_metadata())

    mocker.patch.object(wallet.client, "get", side_effect=route)
    response = await wallet.create_invoice(amount=100)
    assert response.ok is False
    assert "not allowed" in (response.error_message or "")


# --- pending-invoice persistence ---


async def _create_one_invoice(wallet, mocker, payment_hash):
    decoded = fake_decoded(payment_hash=payment_hash)
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
    return await wallet.create_invoice(amount=100)


@pytest.mark.anyio
async def test_pending_invoices_survive_restart(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
    payment_hash = "b" * 64
    response = await _create_one_invoice(wallet, mocker, payment_hash)
    assert response.ok is True
    assert (tmp_path / "blink-noncustodial-pending.json").exists()

    # a fresh instance (post-restart) reloads the pending invoice
    reloaded = make_wallet(monkeypatch, tmp_path)
    assert payment_hash in reloaded.pending_invoices
    assert reloaded._verify_urls[payment_hash] == "https://blink.sv/v/a"
    assert reloaded._invoice_meta[payment_hash].expires_at > 0


@pytest.mark.anyio
async def test_evicted_invoice_is_not_reloaded(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
    payment_hash = "c" * 64
    response = await _create_one_invoice(wallet, mocker, payment_hash)
    assert response.ok is True
    wallet._evict_invoice(payment_hash, reason="paid")

    reloaded = make_wallet(monkeypatch, tmp_path)
    assert payment_hash not in reloaded.pending_invoices
    assert payment_hash not in reloaded._verify_urls


@pytest.mark.anyio
async def test_expired_invoice_is_not_reloaded(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
    payment_hash = "d" * 64
    response = await _create_one_invoice(wallet, mocker, payment_hash)
    assert response.ok is True
    # age the entry past expiry + grace, then reload
    meta = wallet._invoice_meta[payment_hash]
    meta.expires_at = time.time() - 10_000
    wallet._persist_pending()

    reloaded = make_wallet(monkeypatch, tmp_path)
    assert payment_hash not in reloaded.pending_invoices


def test_corrupt_pending_store_is_tolerated(monkeypatch, tmp_path):
    (tmp_path / "blink-noncustodial-pending.json").write_text("{not json")
    wallet = make_wallet(monkeypatch, tmp_path)
    assert wallet.pending_invoices == []


@pytest.mark.anyio
async def test_duplicate_payment_hash_registered_once(monkeypatch, mocker, tmp_path):
    wallet = make_wallet(monkeypatch, tmp_path)
    payment_hash = "e" * 64
    for _ in range(2):
        response = await _create_one_invoice(wallet, mocker, payment_hash)
        assert response.ok is True
    assert wallet.pending_invoices.count(payment_hash) == 1


# --- SOLID seams: signers, send capability, adapter, minter, tracker ---

GRANT_PRIVKEY = "11" * 32  # deterministic 32-byte key for tests


def test_grant_key_signer_satisfies_signer_port():
    signer = GrantKeySigner(GRANT_PRIVKEY)
    assert isinstance(signer, InvoiceSigner)
    assert signer.pubkey  # compressed pubkey exposed at construction


@pytest.mark.anyio
async def test_grant_key_signer_signs_sha256_der():
    signer = GrantKeySigner(GRANT_PRIVKEY)
    sig = await signer.sign_invoice_request("grant:abc:123-456")
    assert isinstance(sig, str) and sig
    # coincurve ECDSA, no SDK involved
    assert sig != signer.pubkey


@pytest.mark.anyio
async def test_spark_sdk_signer_delegates_to_adapter(monkeypatch):
    class FakeAdapter:
        async def sign_message(self, message):
            assert message.endswith("-789")
            return "02ab", "sig-hex"

    signer = SparkSdkSigner(FakeAdapter())
    sig = await signer.sign_invoice_request("lnurl-invoice-v1:d:u:1:h:0:rid-789")
    assert sig == "sig-hex"
    assert signer.pubkey == "02ab"  # pubkey resolved at sign time


@pytest.mark.anyio
async def test_no_send_capability_refuses_send_and_reports_zero():
    cap = NoSendCapability()
    response = await cap.pay("lnbc...", "a" * 64, 1000)
    assert response.ok is False
    assert "not supported" in (response.error_message or "").lower()
    status = await cap.status()
    assert status.balance_msat == 0
    payment_status = await cap.payment_status("a" * 64)
    assert payment_status.paid is not True


def test_spark_adapter_payment_info_contains_duck_typing():
    # status enum with .name, htlc details, fees as int, id, 0x-prefixed hash
    payment = SimpleNamespace(
        status=SimpleNamespace(name="COMPLETED"),
        details=SimpleNamespace(
            htlc_details=SimpleNamespace(
                preimage="ab" * 32, payment_hash="0x" + "cc" * 32
            )
        ),
        fees=2500,
        id="sdk-id-1",
    )
    info = SparkSdkAdapter._payment_info(payment)
    assert info.status is True
    assert info.preimage == "ab" * 32
    assert info.fee_msat == 2500
    assert info.sdk_payment_id == "sdk-id-1"
    assert info.htlc_hash == "cc" * 32  # 0x prefix stripped


def test_spark_adapter_payment_info_handles_none_and_missing_fields():
    info = SparkSdkAdapter._payment_info(None)
    assert info.status is None and info.preimage is None and info.fee_msat is None
    # fees_sat fallback when `fees` absent
    payment = SimpleNamespace(
        status=SimpleNamespace(name="FAILED"),
        details=None,
        fees=None,
        fees_sat=3,
        id=None,
    )
    info2 = SparkSdkAdapter._payment_info(payment)
    assert info2.status is False
    assert info2.fee_msat == 3000


@pytest.mark.anyio
async def test_signed_invoice_minter_builds_canonical_and_signs(
    monkeypatch, mocker, tmp_path
):
    from lnbits.wallets.blink_noncustodial import LnUrlPayClient

    captured = {}

    class FakeSigner:
        pubkey = "02deadbeef"

        async def sign_invoice_request(self, message):
            captured["message"] = message
            return "sig"

    client = mocker.Mock()
    lnurl = LnUrlPayClient(client, "https://blink.sv", "hanzy", "blink.sv")
    minter = SignedInvoiceMinter(
        client, "https://blink.sv", "hanzy", "blink.sv", FakeSigner(), lnurl
    )

    async def fake_post(url, json=None):
        captured["url"] = url
        captured["body"] = json
        return mock_http_response({"pr": "lnbc...", "verify": "https://blink.sv/v/a"})

    mocker.patch.object(client, "post", side_effect=fake_post)
    desc_hash = "aa" * 32
    result = await minter.mint(5, bytes.fromhex(desc_hash), None, 3600)
    assert not isinstance(result, type(None))
    data, desc_hex, amount_msat = result
    assert isinstance(data, dict)
    assert desc_hex == desc_hash
    assert amount_msat == 5000
    # canonical message binds domain/identifier/amount/hash/expiry + timestamp
    assert captured["message"].startswith(
        "lnurl-invoice-v1:blink.sv:hanzy:5000:" + desc_hash + ":3600:"
    )
    assert captured["body"]["signature"] == "sig"
    assert captured["body"]["pubkey"] == "02deadbeef"
    assert captured["body"]["description_hash"] == desc_hash


def test_pending_tracker_register_persist_evict(tmp_path):
    store = tmp_path / "pending.json"
    tracker = PendingInvoiceTracker(store)
    tracker.register("h1", "https://d/v/h1", expires_at=0)
    assert "h1" in tracker.pending_invoices
    assert tracker.verify_url_for("h1") == "https://d/v/h1"

    reloaded = PendingInvoiceTracker(store)
    assert "h1" in reloaded.pending_invoices

    reloaded.evict("h1", "paid")
    assert "h1" not in reloaded.pending_invoices
    assert PendingInvoiceTracker(store).pending_invoices == []


def test_pending_tracker_error_streak_eviction(tmp_path):
    tracker = PendingInvoiceTracker(tmp_path / "pending.json")
    for _ in range(2):
        status = tracker.register_error("h2", "not found", hard=True)
        assert status.paid is not True and not status.failed
    status = tracker.register_error("h2", "not found", hard=True)
    assert status.failed  # 3rd consecutive hard error evicts


def test_pending_tracker_soft_errors_never_evict(tmp_path):
    tracker = PendingInvoiceTracker(tmp_path / "pending.json")
    for _ in range(6):
        status = tracker.register_error("h3", "transport error", hard=False)
        assert not status.failed
