"""HTTP views that carry the Amazon sign-in through the user's browser.

The config flow opens the proxy view in a new browser tab. Every request is
forwarded to Amazon, so the user sees Amazon's real pages and solves whatever
Amazon asks for (robot check, captcha, 2FA). Once Amazon confirms the sign-in
the browser is sent to the callback view, which resumes the config flow.

The views need no Home Assistant login because they open in a plain browser
tab. They only answer for flows of this integration that are in progress and
registered here, and the flow id is a random 128-bit value.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
import logging

from aiohttp import web
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.httpx_client import create_async_httpx_client
import httpx

from .api import AlexaAuth, AlexaError, AlexaSignInProxy
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PROXY_PATH = f"/auth/{DOMAIN}/proxy"
CALLBACK_PATH = f"/auth/{DOMAIN}/callback"
DATA_SIGN_INS = f"{DOMAIN}_sign_ins"

PROXY_TIMEOUT = httpx.Timeout(30)

CALLBACK_PAGE = (
    "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Alexa Thermostat</title></head>"
    "<body><p>Signed in. You can close this window.</p>"
    "<script>window.close()</script></body></html>"
)


@dataclass
class PendingSignIn:
    """A sign-in waiting for the browser to finish it."""

    auth: AlexaAuth
    proxy: AlexaSignInProxy | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def start_url(flow_id: str) -> str:
    """Return the relative URL the browser opens to start signing in."""
    return f"{PROXY_PATH}/{flow_id}"


@callback
def async_start_sign_in(hass: HomeAssistant, flow_id: str, auth: AlexaAuth) -> str:
    """Accept browser requests for this flow and return the URL to open."""
    if DATA_SIGN_INS not in hass.data:
        # Views cannot be removed from the router, so register them once.
        hass.data[DATA_SIGN_INS] = {}
        hass.http.register_view(AlexaSignInProxyView())
        hass.http.register_view(AlexaSignInCallbackView())
    hass.data[DATA_SIGN_INS][flow_id] = PendingSignIn(auth)
    return start_url(flow_id)


async def async_stop_sign_in(hass: HomeAssistant, flow_id: str) -> None:
    """Stop answering browser requests for this flow and close its proxy."""
    pending: PendingSignIn | None = hass.data.get(DATA_SIGN_INS, {}).pop(flow_id, None)
    if pending is not None and pending.proxy is not None:
        await pending.proxy.async_close()


def _pending_sign_in(hass: HomeAssistant, flow_id: str | None) -> PendingSignIn:
    """Return the sign-in for an in-progress flow of this integration, or 404."""
    pending = hass.data.get(DATA_SIGN_INS, {}).get(flow_id) if flow_id else None
    if pending is None:
        raise web.HTTPNotFound
    try:
        flow = hass.config_entries.flow.async_get(flow_id)
    except UnknownFlow:
        flow = None
    if flow is None or flow["handler"] != DOMAIN:
        raise web.HTTPNotFound
    return pending


class AlexaSignInProxyView(HomeAssistantView):
    """Forward the browser's requests to Amazon's sign-in pages."""

    url = PROXY_PATH + "/{flow_id}"
    extra_urls = [PROXY_PATH + "/{flow_id}/{tail:.*}"]
    name = f"api:{DOMAIN}:proxy"
    requires_auth = False

    async def _handle(
        self, request: web.Request, flow_id: str, tail: str = ""
    ) -> web.StreamResponse:
        hass: HomeAssistant = request.app[KEY_HASS]
        pending = _pending_sign_in(hass, flow_id)
        try:
            async with pending.lock:
                if pending.proxy is None:
                    # Build the proxy on the first request: links in Amazon's pages
                    # are rewritten to the address this browser reached us on.
                    pending.proxy = await pending.auth.async_create_proxy(
                        f"{request.url.origin()}{start_url(flow_id)}",
                        start_url=start_url(flow_id),
                        return_url=f"{CALLBACK_PATH}?flow_id={flow_id}",
                        session_factory=partial(
                            create_async_httpx_client,
                            hass,
                            auto_cleanup=False,
                            follow_redirects=False,
                            timeout=PROXY_TIMEOUT,
                        ),
                    )
            return await pending.proxy.async_handle(request)
        except web.HTTPException:
            # Includes the redirect to the callback once Amazon confirms the sign-in.
            raise
        except (httpx.HTTPError, AlexaError) as err:
            _LOGGER.debug("Could not reach Amazon through the sign-in proxy: %s", err)
            return web.Response(
                status=502, text="Could not reach Amazon. Refresh the page to try again."
            )

    get = post = put = patch = delete = head = options = _handle


class AlexaSignInCallbackView(HomeAssistantView):
    """Resume the config flow once Amazon confirmed the sign-in."""

    url = CALLBACK_PATH
    name = f"api:{DOMAIN}:callback"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Close the browser tab and continue the flow in Home Assistant."""
        hass: HomeAssistant = request.app[KEY_HASS]
        flow_id = request.query.get("flow_id")
        pending = _pending_sign_in(hass, flow_id)
        if pending.proxy is None or not pending.proxy.signed_in:
            raise web.HTTPNotFound
        await hass.config_entries.flow.async_configure(flow_id=flow_id, user_input=None)
        return web.Response(text=CALLBACK_PAGE, content_type="text/html")
