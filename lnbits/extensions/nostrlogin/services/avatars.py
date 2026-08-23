"""Fetch nostr profile metadata (kind 0) for signed-in users.

Diverges from btcpay-nostr-login intentionally: instead of downloading and
re-hosting avatar images server-side (which requires SSRF protection around
arbitrary remote URLs), only the https URL is stored on the account. Clients
render the image themselves.
"""

import asyncio
import json
import time

from loguru import logger
from websockets import connect as ws_connect

from lnbits.core.crud import get_account, update_account

# well-known profile indexers
PROFILE_RELAYS = [
    "wss://purplepag.es",
    "wss://user.kindpag.es",
    "wss://profiles.nostr1.com",
    "wss://directory.yabu.me",
]

PROFILE_FETCH_TIMEOUT = 8


def _is_https_url(url: object) -> bool:
    return isinstance(url, str) and url.startswith("https://")


async def fetch_profile(pubkey: str, relays: list[str] | None = None) -> dict | None:
    """Queries relays in parallel for a kind-0 metadata event. First wins."""
    relays = relays or PROFILE_RELAYS
    queue: asyncio.Queue[dict] = asyncio.Queue()
    tasks = [
        asyncio.create_task(_query_relay(relay, pubkey, queue)) for relay in relays
    ]
    try:
        deadline = time.time() + PROFILE_FETCH_TIMEOUT
        while time.time() < deadline:
            try:
                event = await asyncio.wait_for(
                    queue.get(), timeout=max(0.1, deadline - time.time())
                )
            except asyncio.TimeoutError:
                break
            if event.get("pubkey") != pubkey or event.get("kind") != 0:
                continue
            try:
                profile = json.loads(event.get("content", ""))
                if isinstance(profile, dict):
                    return profile
            except (json.JSONDecodeError, ValueError):
                continue
        return None
    finally:
        for task in tasks:
            task.cancel()


async def _query_relay(relay: str, pubkey: str, queue: asyncio.Queue) -> None:
    try:
        async with ws_connect(relay, open_timeout=5) as ws:
            sub_id = f"nostrlogin-profile-{relay[-8:]}"
            await ws.send(
                json.dumps(
                    [
                        "REQ",
                        sub_id,
                        {"kinds": [0], "authors": [pubkey], "limit": 1},
                    ]
                )
            )
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(msg, list) and len(msg) >= 3 and msg[0] == "EVENT":
                    await queue.put(msg[2])
                    return
                if msg[0] == "EOSE":
                    return
    except Exception as e:
        logger.debug(f"NostrLogin profile fetch via {relay} failed: {e!s}")


async def sync_profile(user_id: str, pubkey: str) -> None:
    """Updates the account picture/display name from the nostr profile."""
    profile = await fetch_profile(pubkey)
    if not profile:
        return
    account = await get_account(user_id)
    if not account:
        return
    if account.extra is None:
        return
    changed = False
    picture = profile.get("picture")
    if _is_https_url(picture) and not account.extra.picture:
        account.extra.picture = picture
        changed = True
    name = profile.get("display_name") or profile.get("name")
    if isinstance(name, str) and name and not account.extra.display_name:
        account.extra.display_name = name
        changed = True
    if changed:
        await update_account(account)
        logger.info(f"NostrLogin: synced nostr profile for user {user_id}")


def sync_profile_in_background(user_id: str, pubkey: str) -> None:
    asyncio.get_running_loop().create_task(sync_profile(user_id, pubkey))
