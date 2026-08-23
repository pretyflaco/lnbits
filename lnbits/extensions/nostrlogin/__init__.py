import asyncio

from fastapi import APIRouter
from loguru import logger

from .crud import db
from .views import nostrlogin_ext_generic
from .views_api import nostrlogin_ext_api

nostrlogin_ext: APIRouter = APIRouter(prefix="/nostrlogin", tags=["nostrlogin"])
nostrlogin_ext.include_router(nostrlogin_ext_generic)
nostrlogin_ext.include_router(nostrlogin_ext_api)

nostrlogin_static_files = [
    {
        "path": "/nostrlogin/static",
        "name": "nostrlogin_static",
    }
]

scheduled_tasks: list[asyncio.Task] = []


def nostrlogin_stop():
    from .services.nip46 import shutdown_service

    shutdown_service()
    for task in scheduled_tasks:
        try:
            task.cancel()
        except Exception as ex:
            logger.warning(ex)


def nostrlogin_start():
    from lnbits.tasks import create_permanent_unique_task

    from .tasks import wait_for_cleanup

    task = create_permanent_unique_task("nostrlogin_cleanup", wait_for_cleanup)
    scheduled_tasks.append(task)


__all__ = [
    "db",
    "nostrlogin_ext",
    "nostrlogin_start",
    "nostrlogin_static_files",
    "nostrlogin_stop",
]
