"""Validation of signed Nostr events used for authentication.

Ports the verification logic of btcpay-nostr-login (Nip98.cs /
NostrLoginService.ValidateSignedEvent) to LNbits.
"""

import time
from urllib.parse import urlsplit

from lnbits.utils.nostr import verify_event

NIP98_KIND = 27235
CHALLENGE_KIND = 22242

# Tolerance for clock skew into the future.
FUTURE_TOLERANCE_SECONDS = 60


class EventValidationError(Exception):
    pass


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, strip default ports and trailing slashes."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host
    default_port = (scheme == "http" and parts.port == 80) or (
        scheme == "https" and parts.port == 443
    )
    if parts.port and not default_port:
        netloc += f":{parts.port}"
    path = parts.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def urls_match(a: str, b: str) -> bool:
    try:
        return normalize_url(a) == normalize_url(b)
    except ValueError:
        return False


def get_tag(event: dict, name: str) -> str | None:
    for tag in event.get("tags", []):
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == name:
            return tag[1]
    return None


def _check_common(
    event: dict,
    expected_kind: int,
    expected_pubkey: str | None,
    max_age_seconds: int,
) -> None:
    pubkey = event.get("pubkey", "")
    created_at = event.get("created_at")
    if not isinstance(created_at, int):
        raise EventValidationError("Missing 'created_at' field.")
    if expected_pubkey and pubkey.lower() != expected_pubkey.lower():
        raise EventValidationError("Event is not signed by the expected key.")
    age = int(time.time()) - created_at
    if age > max_age_seconds:
        raise EventValidationError("Event has expired.")
    if age < -FUTURE_TOLERANCE_SECONDS:
        raise EventValidationError("Event creation date is in the future.")
    if not verify_event(event):
        raise EventValidationError("Event signature verification failed.")


def validate_nip98_event(
    event: dict,
    *,
    expected_pubkey: str | None = None,
    expected_urls: list[str] | None = None,
    expected_method: str = "POST",
    expected_nonce: str | None = None,
    max_age_seconds: int = 600,
) -> dict:
    """
    Validates a NIP-98 (kind 27235) http auth event.

    The signature check runs last so that bogus events cannot pollute the
    replay store with consumed ids.
    """
    if event.get("kind") != NIP98_KIND:
        raise EventValidationError(f"Expected kind {NIP98_KIND} event.")

    u_tag = get_tag(event, "u")
    if not u_tag:
        raise EventValidationError("Event is missing 'u' tag.")
    if expected_urls and not any(urls_match(u_tag, e) for e in expected_urls):
        raise EventValidationError("'u' tag does not match the request URL.")

    method = get_tag(event, "method")
    if method != expected_method.upper():
        raise EventValidationError(f"Expected '{expected_method.upper()}' method tag.")

    if expected_nonce is not None:
        challenge = get_tag(event, "challenge")
        if not challenge or expected_nonce not in challenge.split(","):
            raise EventValidationError("Challenge mismatch.")

    _check_common(event, NIP98_KIND, expected_pubkey, max_age_seconds)
    return event


def validate_challenge_event(
    event: dict,
    *,
    expected_pubkey: str,
    expected_nonce: str,
    max_age_seconds: int = 600,
) -> dict:
    """Validates a legacy kind-22242 challenge event."""
    if event.get("kind") != CHALLENGE_KIND:
        raise EventValidationError(f"Expected kind {CHALLENGE_KIND} event.")

    challenge = get_tag(event, "challenge")
    if not challenge or expected_nonce not in challenge.split(","):
        raise EventValidationError("Challenge mismatch.")

    _check_common(event, CHALLENGE_KIND, expected_pubkey, max_age_seconds)
    return event
