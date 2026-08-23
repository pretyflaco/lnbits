import time

from lnbits.db import Database

from .models import NostrLoginSettings

db = Database("ext_nostrlogin")


async def get_nostrlogin_settings() -> NostrLoginSettings:
    row = await db.fetchone("SELECT * FROM nostrlogin.settings WHERE id = 1")
    if not row:
        return NostrLoginSettings()
    return NostrLoginSettings(**row)


async def update_nostrlogin_settings(data: NostrLoginSettings) -> NostrLoginSettings:
    existing = await db.fetchone("SELECT id FROM nostrlogin.settings WHERE id = 1")
    data.id = 1
    if existing:
        await db.update("nostrlogin.settings", data, "WHERE id = 1")
    else:
        await db.insert("nostrlogin.settings", data)
    return data


async def consume_replay_event(event_id: str, ttl_seconds: int) -> bool:
    """
    Atomically marks an event id as seen. Returns True if this is the first
    time the event id is consumed, False when it is a replay.
    """
    now = int(time.time())
    result = await db.execute(
        """
        INSERT INTO nostrlogin.replay (event_id, expires_at)
        VALUES (:event_id, :expires_at)
        ON CONFLICT (event_id) DO NOTHING
        """,
        {"event_id": event_id, "expires_at": now + ttl_seconds},
    )
    return bool(result and result.rowcount and result.rowcount > 0)


async def purge_expired_replay_events() -> None:
    await db.execute(
        "DELETE FROM nostrlogin.replay WHERE expires_at < :now",
        {"now": int(time.time())},
    )
