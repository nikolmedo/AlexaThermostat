"""Tests for the sign-in proxy and callback views."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.setup import async_setup_component
import httpx
import pytest

from custom_components.alexa_thermostat.api import AlexaAuth
from custom_components.alexa_thermostat.const import DOMAIN
from custom_components.alexa_thermostat.proxy_view import (
    CALLBACK_PATH,
    DATA_SIGN_INS,
    async_start_sign_in,
    async_stop_sign_in,
)

FLOW_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def proxy() -> MagicMock:
    """A sign-in proxy that answers every request with a fixed page."""
    proxy = MagicMock()
    proxy.signed_in = False
    proxy.async_handle = AsyncMock(side_effect=lambda request: web.Response(text="amazon page"))
    proxy.async_close = AsyncMock()
    return proxy


@pytest.fixture
def auth(proxy: MagicMock) -> MagicMock:
    """An AlexaAuth whose proxy is the fixture above."""
    auth = MagicMock()
    auth.async_create_proxy = AsyncMock(return_value=proxy)
    return auth


@pytest.fixture
def flows(hass: HomeAssistant) -> Generator[SimpleNamespace]:
    """Pretend flow ids in `handlers` are in progress; `configure` records resumes."""
    in_progress = {FLOW_ID: DOMAIN}

    def _get(flow_id: str) -> dict[str, str]:
        if flow_id not in in_progress:
            raise UnknownFlow
        return {"flow_id": flow_id, "handler": in_progress[flow_id]}

    with (
        patch.object(hass.config_entries.flow, "async_get", side_effect=_get),
        patch.object(hass.config_entries.flow, "async_configure", AsyncMock()) as configure,
    ):
        yield SimpleNamespace(handlers=in_progress, configure=configure)


@pytest.fixture
async def client(hass: HomeAssistant, hass_client_no_auth):
    """An HTTP client with no Home Assistant login, like a fresh browser tab."""
    assert await async_setup_component(hass, "http", {})
    return await hass_client_no_auth()


async def test_start_returns_relative_url_and_registers_once(
    hass: HomeAssistant, auth: MagicMock, client
) -> None:
    """Starting twice registers the views once and returns a relative URL."""
    with patch.object(hass.http, "register_view", wraps=hass.http.register_view) as register:
        url = async_start_sign_in(hass, FLOW_ID, auth)
        async_start_sign_in(hass, "other", auth)

    assert url == f"/auth/{DOMAIN}/proxy/{FLOW_ID}"
    assert register.call_count == 2


async def test_proxy_builds_once_from_the_browser_origin(
    hass: HomeAssistant, auth: MagicMock, proxy: MagicMock, flows: SimpleNamespace, client
) -> None:
    """The first request builds the proxy for the address the browser used."""
    url = async_start_sign_in(hass, FLOW_ID, auth)

    first = await client.get(url)
    assert first.status == 200
    assert await first.text() == "amazon page"
    second = await client.post(f"{url}/ap/signin", data={"email": "x"})
    assert second.status == 200
    auth.async_create_proxy.assert_awaited_once()
    base_url = auth.async_create_proxy.call_args.args[0]
    kwargs = auth.async_create_proxy.call_args.kwargs
    assert base_url == f"http://{client.host}:{client.port}{url}"
    assert kwargs["start_url"] == url
    assert kwargs["return_url"] == f"{CALLBACK_PATH}?flow_id={FLOW_ID}"
    assert proxy.async_handle.await_count == 2


@pytest.mark.parametrize("handler", [None, "alexa_media"])
async def test_proxy_rejects_unknown_and_foreign_flows(
    hass: HomeAssistant, auth: MagicMock, flows: SimpleNamespace, client, handler: str | None
) -> None:
    """Only registered, in-progress flows of this integration are served."""
    if handler is None:
        # Registered here, but the flow is gone.
        flows.handlers.pop(FLOW_ID)
    else:
        flows.handlers[FLOW_ID] = handler
    url = async_start_sign_in(hass, FLOW_ID, auth)

    assert (await client.get(url)).status == 404
    assert (await client.get(f"{CALLBACK_PATH}?flow_id={FLOW_ID}")).status == 404
    # A flow id that was never registered.
    assert (await client.get(f"/auth/{DOMAIN}/proxy/{'f' * 32}")).status == 404
    assert (await client.get(CALLBACK_PATH)).status == 404
    auth.async_create_proxy.assert_not_awaited()


async def test_callback_needs_a_confirmed_sign_in(
    hass: HomeAssistant, auth: MagicMock, proxy: MagicMock, flows: SimpleNamespace, client
) -> None:
    """The callback resumes the flow only after Amazon confirmed the sign-in."""
    url = async_start_sign_in(hass, FLOW_ID, auth)
    callback_url = f"{CALLBACK_PATH}?flow_id={FLOW_ID}"

    # Before the proxy even started.
    assert (await client.get(callback_url)).status == 404
    await client.get(url)
    assert (await client.get(callback_url)).status == 404
    flows.configure.assert_not_awaited()

    proxy.signed_in = True
    response = await client.get(callback_url)

    assert response.status == 200
    assert "window.close()" in await response.text()
    flows.configure.assert_awaited_once_with(flow_id=FLOW_ID, user_input=None)


async def test_stop_closes_the_proxy_and_stops_serving(
    hass: HomeAssistant, auth: MagicMock, proxy: MagicMock, flows: SimpleNamespace, client
) -> None:
    """A stopped sign-in closes its proxy and answers 404 afterwards."""
    url = async_start_sign_in(hass, FLOW_ID, auth)
    await client.get(url)

    await async_stop_sign_in(hass, FLOW_ID)
    await async_stop_sign_in(hass, FLOW_ID)

    proxy.async_close.assert_awaited_once()
    assert FLOW_ID not in hass.data[DATA_SIGN_INS]
    assert (await client.get(url)).status == 404


async def test_amazon_unreachable_shows_an_error_page(
    hass: HomeAssistant, auth: MagicMock, proxy: MagicMock, flows: SimpleNamespace, client
) -> None:
    """Network errors toward Amazon become a readable page, not a 500."""
    proxy.async_handle.side_effect = httpx.ConnectError("down")
    url = async_start_sign_in(hass, FLOW_ID, auth)

    response = await client.get(url)

    assert response.status == 502
    assert "Could not reach Amazon" in await response.text()


async def test_success_redirect_reaches_the_browser(
    hass: HomeAssistant, auth: MagicMock, proxy: MagicMock, flows: SimpleNamespace, client
) -> None:
    """The proxy's redirect to the callback is passed through untouched."""
    proxy.async_handle.side_effect = web.HTTPFound(location=f"{CALLBACK_PATH}?flow_id={FLOW_ID}")
    url = async_start_sign_in(hass, FLOW_ID, auth)

    response = await client.get(url, allow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"] == f"{CALLBACK_PATH}?flow_id={FLOW_ID}"


async def test_callback_resumes_the_flow_once(
    hass: HomeAssistant, auth: MagicMock, proxy: MagicMock, flows: SimpleNamespace, client
) -> None:
    """Reloading the callback tab does not run the next step again."""
    url = async_start_sign_in(hass, FLOW_ID, auth)
    await client.get(url)
    proxy.signed_in = True
    callback_url = f"{CALLBACK_PATH}?flow_id={FLOW_ID}"

    assert (await client.get(callback_url)).status == 200
    assert (await client.get(callback_url)).status == 404
    flows.configure.assert_awaited_once()


async def test_stop_while_the_proxy_is_built(
    hass: HomeAssistant, proxy: MagicMock, flows: SimpleNamespace, client
) -> None:
    """A sign-in stopped while its proxy is being built closes it and serves nothing."""
    gate = asyncio.Event()

    async def slow_create(*args, **kwargs) -> MagicMock:
        await gate.wait()
        return proxy

    auth = MagicMock()
    auth.async_create_proxy = slow_create
    url = async_start_sign_in(hass, FLOW_ID, auth)
    request = asyncio.ensure_future(client.get(url))
    await asyncio.sleep(0.05)
    await async_stop_sign_in(hass, FLOW_ID)
    gate.set()

    assert (await request).status == 404
    proxy.async_close.assert_awaited_once()
    proxy.async_handle.assert_not_awaited()


SIGN_IN_PAGE = (
    "<html><body><form name='signIn' method='post' action='/ap/signin'>"
    "<input name='email'><input name='password' type='password'></form></body></html>"
)


async def test_spoofed_host_never_leaves_amazon(
    hass: HomeAssistant, flows: SimpleNamespace, client
) -> None:
    """A Host header with another port must not make the real proxy fetch that server."""
    fetched: list[httpx.URL] = []

    def amazon(request: httpx.Request) -> httpx.Response:
        fetched.append(request.url)
        return httpx.Response(200, text=SIGN_IN_PAGE, headers={"content-type": "text/html"})

    def factory(hass: HomeAssistant, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(amazon))

    auth = AlexaAuth("amazon.com", "user@example.com", hass.config.path, password="pw")
    navigate = {"Sec-Fetch-Mode": "navigate"}
    with patch("custom_components.alexa_thermostat.proxy_view.create_async_httpx_client", factory):
        url = async_start_sign_in(hass, FLOW_ID, auth)
        assert (await client.get(url, headers=navigate)).status == 200
        spoofed = await client.get(f"{url}/x", headers={**navigate, "Host": "169.254.169.254:80"})
        await async_stop_sign_in(hass, FLOW_ID)
    await auth.async_close()

    assert spoofed.status == 404
    assert fetched
    assert all(str(url.host).endswith("amazon.com") for url in fetched), fetched


def test_debug_logging_never_enables_authcaptureproxy() -> None:
    """authcaptureproxy logs the posted password at debug level."""
    manifest = json.loads(
        (Path(__file__).parent.parent / "custom_components" / DOMAIN / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert "authcaptureproxy" not in manifest["loggers"]
