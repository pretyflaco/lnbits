from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from lnbits.core.crud import get_user
from lnbits.decorators import optional_user_id
from lnbits.helpers import template_renderer

nostrlogin_ext_generic = APIRouter(tags=["nostrlogin_pages"])


@nostrlogin_ext_generic.get(
    "/",
    description="Nostr Login management page (link/unlink keys, settings)",
    response_class=HTMLResponse,
)
async def index(
    request: Request,
    user_id: str | None = Depends(optional_user_id),
):
    user = await get_user(user_id) if user_id else None
    if not user:
        # Signed-out visitors belong on the login page, not the manager.
        return RedirectResponse("/nostrlogin/login")
    return template_renderer(["nostrlogin/templates"]).TemplateResponse(
        request, "nostrlogin/index.html", {"user": user.json()}
    )


@nostrlogin_ext_generic.get(
    "/login",
    description="Sign in with a remote Nostr signer (public)",
    response_class=HTMLResponse,
)
async def login(
    request: Request,
    user_id: str | None = Depends(optional_user_id),
):
    return template_renderer(["nostrlogin/templates"]).TemplateResponse(
        request, "nostrlogin/login.html", {"already_signed_in": bool(user_id)}
    )
