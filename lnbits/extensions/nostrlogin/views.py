from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from lnbits.core.models import User
from lnbits.decorators import check_user_exists
from lnbits.helpers import template_renderer

nostrlogin_ext_generic = APIRouter(tags=["nostrlogin_pages"])


@nostrlogin_ext_generic.get(
    "/", description="Nostr Login page", response_class=HTMLResponse
)
async def index(request: Request):
    return template_renderer(["nostrlogin/templates"]).TemplateResponse(
        request, "nostrlogin/index.html"
    )


@nostrlogin_ext_generic.get(
    "/account", description="Link / unlink nostr keys", response_class=HTMLResponse
)
async def account(
    request: Request, user: User = Depends(check_user_exists)
):
    return template_renderer(["nostrlogin/templates"]).TemplateResponse(
        request, "nostrlogin/account.html", {"user": user.json()}
    )
