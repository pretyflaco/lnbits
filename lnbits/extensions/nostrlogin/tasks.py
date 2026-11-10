import asyncio

from loguru import logger

from .crud import purge_expired_replay_events
from .services.nip46 import get_nostrlogin_service
from .views_api import _rate_limiter


async def wait_for_cleanup():
    while True:
        try:
            await run_by_the_minute_cleanup()
        except Exception as e:
            logger.warning(e)
        await asyncio.sleep(60)


async def run_by_the_minute_cleanup():
    get_nostrlogin_service().remove_expired_sessions()
    _rate_limiter.sweep()
    await purge_expired_replay_events()
