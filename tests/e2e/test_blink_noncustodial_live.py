"""Live E2E tests for the Blink non-custodial funding source (D1/D2 flows).

NO MOCKS by design: these run against a real deployment (real LNURL server,
real Spark network, real sats). They are skipped unless explicitly enabled.

Run (on or against a configured dev box):

    BLINK_E2E=1 \
    BLINK_E2E_LNURL_BASE=https://lnurl.example.com \
    BLINK_E2E_IDENTIFIER=alice \
    BLINK_E2E_GRANT_PRIVKEY=<64 hex chars> \
    .venv/bin/python -m pytest tests/e2e/test_blink_noncustodial_live.py -q

Optional (LNbits-side API checks, incl. a small real send):
    BLINK_E2E_LNBITS_BASE=http://127.0.0.1:5000
    BLINK_E2E_LNBITS_ADMINKEY / BLINK_E2E_LNBITS_INKEY
    BLINK_E2E_SEND=1            # allow spending BLINK_E2E_SEND_SATS (default 21)

Never commit credentials. The suite stays under the server's 10 req/min
per-IP budget by pacing requests.
"""

import hashlib
import os
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("BLINK_E2E") != "1", reason="live E2E disabled (set BLINK_E2E=1)"
)

coincurve = pytest.importorskip("coincurve")

LNURL_BASE = os.environ.get("BLINK_E2E_LNURL_BASE", "").rstrip("/")
IDENTIFIER = os.environ.get("BLINK_E2E_IDENTIFIER", "")
GRANT_PRIVKEY = os.environ.get("BLINK_E2E_GRANT_PRIVKEY", "")
DOMAIN = httpx.URL(LNURL_BASE).host if LNURL_BASE else ""
PACE = 6.5  # seconds between limiter-consuming calls (10/min shared budget)

LNBITS_BASE = os.environ.get("BLINK_E2E_LNBITS_BASE", "").rstrip("/")
LNBITS_ADMINKEY = os.environ.get("BLINK_E2E_LNBITS_ADMINKEY", "")
LNBITS_INKEY = os.environ.get("BLINK_E2E_LNBITS_INKEY", "")
ALLOW_SEND = os.environ.get("BLINK_E2E_SEND") == "1"
SEND_SATS = int(os.environ.get("BLINK_E2E_SEND_SATS", "21"))


def _grant_key():
    return coincurve.PrivateKey(bytes.fromhex(GRANT_PRIVKEY))


def _sign(priv, message: str) -> str:
    digest = hashlib.sha256(message.encode()).digest()
    return priv.sign(digest, hasher=None).hex()


def _signed_body(
    priv,
    pub_hex,
    *,
    amount_msat=21000,
    desc_hash=None,
    expiry_secs=3600,
    request_id=None,
    ts=None,
    identifier=IDENTIFIER,
    domain=DOMAIN,
):
    ts = ts if ts is not None else int(time.time())
    request_id = request_id or uuid.uuid4().hex
    desc_hash = desc_hash or hashlib.sha256(b"e2e").hexdigest()
    expiry_tag = "none" if expiry_secs is None else str(expiry_secs)
    canonical = (
        f"lnurl-invoice-v1:{domain}:{identifier}:{amount_msat}:"
        f"{desc_hash}:{expiry_tag}:{request_id}"
    )
    body = {
        "amount_msat": amount_msat,
        "description_hash": desc_hash,
        "request_id": request_id,
        "pubkey": pub_hex,
        "timestamp": ts,
        "signature": _sign(priv, f"{canonical}-{ts}"),
    }
    if expiry_secs is not None:
        body["expiry_secs"] = expiry_secs
    return body


def _post(client, body, identifier=IDENTIFIER):
    time.sleep(PACE)
    return client.post(f"{LNURL_BASE}/lnurlp/{identifier}/invoice/signed", json=body)


def _lnurl_rejected(resp, reason_part):
    """LNURL-style errors are HTTP 200 with a status:ERROR body."""
    if resp.status_code == 400:
        return True
    try:
        data = resp.json()
    except Exception:
        return False
    return data.get("status") == "ERROR" and reason_part in data.get("reason", "")


@pytest.fixture
def client():
    with httpx.Client(timeout=60) as c:
        yield c


@pytest.fixture
def grant():
    return _grant_key(), _grant_key().public_key.format(compressed=True).hex()


def test_positive_delegated_invoice_mints(client, grant):
    key, pub = grant
    r = _post(client, _signed_body(key, pub))
    assert r.status_code == 200
    data = r.json()
    assert data.get("pr") and data.get("verify")


def test_replay_same_request_id_rejected(client, grant):
    key, pub = grant
    body = _signed_body(key, pub)
    r1 = _post(client, body)
    assert r1.status_code == 200
    r2 = _post(client, body)
    assert _lnurl_rejected(r2, "already used")


def test_tampered_signature_rejected(client, grant):
    key, pub = grant
    body = _signed_body(key, pub)
    body["signature"] = body["signature"][:-2] + (
        "00" if not body["signature"].endswith("00") else "01"
    )
    assert _lnurl_rejected(_post(client, body), "signature")


def test_stale_timestamp_rejected(client, grant):
    key, pub = grant
    body = _signed_body(key, pub, ts=int(time.time()) - 1200)
    assert _lnurl_rejected(_post(client, body), "timestamp")


def test_uppercase_hash_rejected(client, grant):
    key, pub = grant
    body = _signed_body(key, pub, desc_hash="A" * 64)
    assert _lnurl_rejected(_post(client, body), "hash")


def test_extra_field_smuggling_rejected(client, grant):
    key, pub = grant
    body = _signed_body(key, pub)
    body["metadata"] = "evil"
    r = _post(client, body)
    assert r.status_code == 422


def test_non_sat_multiple_amount_rejected(client, grant):
    key, pub = grant
    assert _lnurl_rejected(
        _post(client, _signed_body(key, pub, amount_msat=1500)), "range"
    )


def test_unknown_identifier_not_found(client, grant):
    key, pub = grant
    r = _post(
        client, _signed_body(key, pub, identifier="nosuchuser"), identifier="nosuchuser"
    )
    assert r.status_code == 404


def test_absent_expiry_does_not_alias_zero(client, grant):
    key, pub = grant
    ok_body = _signed_body(key, pub, expiry_secs=None)
    r = _post(client, ok_body)
    assert r.status_code == 200, "absent expiry (canonical 'none') must verify"
    aliased = dict(ok_body, expiry_secs=0, request_id=uuid.uuid4().hex)
    r = _post(client, aliased)
    assert _lnurl_rejected(
        r, "signature"
    ), "expiry 0 must not reuse the 'none' signature"


def test_cross_owner_regrant_is_conflict(client, grant):
    """S1 regression: a foreign account must not rebind our delegated key."""
    _, pub = grant
    attacker = coincurve.PrivateKey()
    apub = attacker.public_key.format(compressed=True).hex()
    auser = f"e2e{uuid.uuid4().hex[:10]}"
    ts = int(time.time())
    time.sleep(PACE)
    r = client.post(
        f"{LNURL_BASE}/lnurlpay/{apub}",
        json={
            "username": auser,
            "signature": _sign(attacker, f"{auser}-{ts}"),
            "timestamp": ts,
            "description": "e2e throwaway",
        },
    )
    assert r.status_code in (200, 201)
    ts = int(time.time())
    time.sleep(PACE)
    r = client.post(
        f"{LNURL_BASE}/lnurlpay/{apub}/grant",
        json={
            "delegated_pubkey": pub,
            "expiry_secs": 3600,
            "timestamp": ts,
            "signature": _sign(attacker, f"grant:{pub}:3600-{ts}"),
        },
    )
    assert r.status_code == 409


@pytest.mark.skipif(
    not (LNBITS_BASE and LNBITS_ADMINKEY and LNBITS_INKEY),
    reason="LNbits API keys not configured",
)
def test_lnbits_receive_and_settle_detection(client):
    """Create an invoice through LNbits; it must appear and stay queryable."""
    r = client.post(
        f"{LNBITS_BASE}/api/v1/payments",
        headers={"X-Api-Key": LNBITS_INKEY},
        json={"out": False, "amount": SEND_SATS, "memo": "e2e", "unit": "sat"},
    )
    assert r.status_code == 201
    payment_hash = r.json()["payment_hash"]
    r = client.get(
        f"{LNBITS_BASE}/api/v1/payments/{payment_hash}",
        headers={"X-Api-Key": LNBITS_INKEY},
    )
    assert r.status_code == 200


@pytest.mark.skipif(
    not (LNBITS_BASE and LNBITS_ADMINKEY and ALLOW_SEND),
    reason="send disabled (set BLINK_E2E_SEND=1)",
)
def test_lnbits_seeded_send_with_preimage(client):
    """Pay a real invoice through the seeded send path; preimage must validate."""
    r = client.post(
        f"{LNBITS_BASE}/api/v1/payments",
        headers={"X-Api-Key": LNBITS_INKEY},
        json={"out": False, "amount": SEND_SATS, "memo": "e2e-send", "unit": "sat"},
    )
    assert r.status_code == 201
    inv = r.json()
    r = client.post(
        f"{LNBITS_BASE}/api/v1/payments",
        headers={"X-Api-Key": LNBITS_ADMINKEY},
        json={"out": True, "bolt11": inv["bolt11"]},
    )
    assert r.status_code == 201, r.text
    for _ in range(24):
        time.sleep(5)
        st = client.get(
            f"{LNBITS_BASE}/api/v1/payments/{inv['payment_hash']}",
            headers={"X-Api-Key": LNBITS_INKEY},
        ).json()
        if st.get("paid"):
            return
    pytest.fail("payment not settled within 120s")
