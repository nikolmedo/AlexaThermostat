"""Tests for the config, reauth and options flows."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.network import NoURLAvailableError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alexa_thermostat.api import (
    AlexaAuth,
    AlexaAuthError,
    AlexaConnectionError,
    AlexaInvalidOtpSecret,
)
from custom_components.alexa_thermostat.api.parser import parse_thermostats
from custom_components.alexa_thermostat.const import (
    CONF_AMAZON_DOMAIN,
    CONF_DEVICE_UUID,
    CONF_HASS_URL,
    CONF_OTP_SECRET,
    CONF_SCAN_INTERVAL,
    CONF_TOKENS,
    DOMAIN,
)
from custom_components.alexa_thermostat.proxy_view import DATA_SIGN_INS

from .conftest import EMAIL, TOKENS, load_fixture

HASS_URL = "http://ha.local:8123"
USER_INPUT = {
    CONF_EMAIL: EMAIL,
    CONF_PASSWORD: "secret",
    CONF_AMAZON_DOMAIN: "amazon.com",
    CONF_HASS_URL: HASS_URL,
}
NEW_UUID = "fedcba9876543210fedcba9876543210"


@pytest.fixture
def flow_auth() -> Generator[MagicMock]:
    """Patch AlexaAuth inside the config flow."""
    with patch(
        "custom_components.alexa_thermostat.config_flow.AlexaAuth", autospec=True
    ) as auth_cls:
        auth = auth_cls.return_value
        auth.async_finish_proxy_login = AsyncMock()
        auth.async_close = AsyncMock()
        auth.tokens = TOKENS
        auth.device_uuid = NEW_UUID
        yield auth_cls


@pytest.fixture
def flow_client() -> Generator[MagicMock]:
    """Patch the client inside the config flow."""
    with patch(
        "custom_components.alexa_thermostat.config_flow.AlexaThermostatClient", autospec=True
    ) as client_cls:
        client_cls.return_value.async_get_thermostats = AsyncMock(
            return_value=parse_thermostats(load_fixture("endpoints.json"))
        )
        yield client_cls.return_value


@pytest.fixture
def no_setup() -> Generator[None]:
    """Skip the real setup after the entry is created."""
    with patch("custom_components.alexa_thermostat.async_setup_entry", return_value=True):
        yield


async def _submit(hass: HomeAssistant, user_input: dict | None = None):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input or USER_INPUT
    )


async def _browser_signs_in(hass: HomeAssistant, result):
    """Do what the callback view does, then what the frontend does next."""
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    flow_id = result["flow_id"]
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.EXTERNAL_STEP_DONE
    return await hass.config_entries.flow.async_configure(flow_id)


async def _start(hass: HomeAssistant, user_input: dict | None = None):
    return await _browser_signs_in(hass, await _submit(hass, user_input))


async def test_user_flow_opens_amazon_through_the_proxy(
    hass: HomeAssistant, flow_auth: MagicMock
) -> None:
    """Submitting the form opens the proxy for this flow in the browser."""
    result = await _submit(hass, {**USER_INPUT, CONF_OTP_SECRET: "ABCD EFGH"})

    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["step_id"] == "sign_in"
    # The frontend only opens absolute http(s) URLs.
    url = f"{HASS_URL}/auth/{DOMAIN}/proxy/{result['flow_id']}"
    assert result["url"] == url
    assert result["description_placeholders"] == {"url": url}
    assert hass.data[DATA_SIGN_INS][result["flow_id"]].auth is flow_auth.return_value
    assert flow_auth.call_args.kwargs["password"] == "secret"
    assert flow_auth.call_args.kwargs["otp_secret"] == "ABCDEFGH"
    flow_auth.return_value.async_finish_proxy_login.assert_not_awaited()


async def test_user_flow_creates_entry(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock, no_setup: None
) -> None:
    """A confirmed browser sign-in stores tokens and uuid, never the password."""
    result = await _start(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"] == {
        CONF_EMAIL: EMAIL,
        CONF_AMAZON_DOMAIN: "amazon.com",
        CONF_TOKENS: TOKENS,
        CONF_DEVICE_UUID: NEW_UUID,
    }
    assert CONF_PASSWORD not in result["data"]
    assert result["result"].unique_id == f"amazon.com:{EMAIL}"
    flow_auth.return_value.async_finish_proxy_login.assert_awaited_once()
    flow_auth.return_value.async_close.assert_awaited()
    assert hass.data[DATA_SIGN_INS] == {}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AlexaAuthError("nope"), "sign_in_failed"),
        (AlexaConnectionError("down"), "cannot_connect"),
    ],
)
async def test_sign_in_errors_then_recovers(
    hass: HomeAssistant,
    flow_auth: MagicMock,
    flow_client: MagicMock,
    no_setup: None,
    error: Exception,
    expected: str,
) -> None:
    """A failed sign-in shows the form again and the next submit starts over."""
    flow_auth.return_value.async_finish_proxy_login.side_effect = [error, None]
    result = await _start(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": expected}
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }
    assert suggested == {
        CONF_EMAIL: EMAIL,
        CONF_AMAZON_DOMAIN: "amazon.com",
        CONF_HASS_URL: HASS_URL,
    }
    flow_auth.return_value.async_close.assert_awaited_once()
    assert hass.data[DATA_SIGN_INS] == {}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    result = await _browser_signs_in(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert flow_auth.call_count == 2


async def test_invalid_otp_secret_on_field(hass: HomeAssistant, flow_auth: MagicMock) -> None:
    """A key that is not base32 is reported on its field, before any sign-in."""
    flow_auth.side_effect = AlexaInvalidOtpSecret("bad")
    result = await _submit(hass, {**USER_INPUT, CONF_OTP_SECRET: "nope"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_OTP_SECRET: "invalid_otp_secret"}
    assert hass.data.get(DATA_SIGN_INS, {}) == {}


@pytest.mark.parametrize("pasted", ["ABCD EFGH ÉJKL MNOP", "ABCD EFGH"])
async def test_invalid_key_through_real_validation(
    hass: HomeAssistant, flow_auth: MagicMock, pasted: str
) -> None:
    """A bad key goes through the real normalize + validation and is reported."""
    flow_auth.side_effect = AlexaAuth
    result = await _submit(hass, {**USER_INPUT, CONF_OTP_SECRET: pasted})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_OTP_SECRET: "invalid_otp_secret"}


async def test_otp_key_with_invisible_characters(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock, no_setup: None
) -> None:
    """A key copied from Amazon on a phone (non-breaking spaces) is cleaned."""
    pasted_key = "abcd\u00a0efgh\u00a0ijkl\u200bmnop\n"
    result = await _start(hass, {**USER_INPUT, CONF_OTP_SECRET: pasted_key})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert flow_auth.call_args.kwargs["otp_secret"] == "ABCDEFGHIJKLMNOP"


async def test_one_time_code_in_key_field(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock, no_setup: None
) -> None:
    """A 6-digit code is not a key: Amazon's page asks for the code instead."""
    result = await _start(hass, {**USER_INPUT, CONF_OTP_SECRET: "123 456"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert flow_auth.call_args.kwargs["otp_secret"] == ""
    assert "123456" not in str(result["data"])
    assert result["result"].options == {}


async def test_no_thermostats_aborts(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock
) -> None:
    """An account without thermostats is not added."""
    flow_client.async_get_thermostats.return_value = []
    result = await _start(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_thermostats"


async def test_discovery_failure_aborts(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock
) -> None:
    """Signed in but no device list: abort and close the session."""
    flow_client.async_get_thermostats.side_effect = AlexaConnectionError("down")
    result = await _start(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"
    flow_auth.return_value.async_close.assert_awaited()


async def test_duplicate_account_aborts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, flow_auth: MagicMock
) -> None:
    """The same account cannot be added twice, and no sign-in starts."""
    mock_config_entry.add_to_hass(hass)
    result = await _submit(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    flow_auth.assert_not_called()


@pytest.mark.parametrize("domain", ["evil.example.com", "amazon.com.attacker.io", "amaz0n.com"])
async def test_invalid_domain_is_rejected(
    hass: HomeAssistant, flow_auth: MagicMock, domain: str
) -> None:
    """Only Amazon domains are used as the login host."""
    result = await _submit(hass, {**USER_INPUT, CONF_AMAZON_DOMAIN: domain})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_AMAZON_DOMAIN: "invalid_domain"}
    flow_auth.assert_not_called()


@pytest.mark.parametrize("domain", ["amazon.co.uk", "amazon.com.mx", "amazon.de"])
async def test_regional_domains_are_accepted(
    hass: HomeAssistant,
    flow_auth: MagicMock,
    flow_client: MagicMock,
    no_setup: None,
    domain: str,
) -> None:
    """Regional Amazon domains pass validation."""
    result = await _start(hass, {**USER_INPUT, CONF_AMAZON_DOMAIN: domain})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_abandoned_sign_in_cleans_up(hass: HomeAssistant, flow_auth: MagicMock) -> None:
    """Closing the dialog while Amazon's tab is open stops the proxy and the session."""
    result = await _submit(hass)
    assert result["flow_id"] in hass.data[DATA_SIGN_INS]

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()

    assert hass.data[DATA_SIGN_INS] == {}
    flow_auth.return_value.async_close.assert_awaited_once()


async def test_reauth_keeps_device_uuid(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    flow_auth: MagicMock,
    flow_client: MagicMock,
    no_setup: None,
) -> None:
    """Reauth signs in through the browser with the stored uuid and replaces the tokens."""
    mock_config_entry.add_to_hass(hass)
    new_tokens = {"refresh_token": "Atnr|new"}
    flow_auth.return_value.tokens = new_tokens
    flow_auth.return_value.device_uuid = mock_config_entry.data[CONF_DEVICE_UUID]

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-secret", CONF_HASS_URL: HASS_URL}
    )
    result = await _browser_signs_in(hass, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert flow_auth.call_args.kwargs["device_uuid"] == mock_config_entry.data[CONF_DEVICE_UUID]
    assert flow_auth.call_args.kwargs["password"] == "new-secret"
    assert mock_config_entry.data[CONF_TOKENS] == new_tokens


async def test_reauth_failure_shows_reauth_form(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, flow_auth: MagicMock
) -> None:
    """A failed reauth sign-in asks for the password again."""
    mock_config_entry.add_to_hass(hass)
    flow_auth.return_value.async_finish_proxy_login.side_effect = AlexaAuthError("nope")

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-secret", CONF_HASS_URL: HASS_URL}
    )
    result = await _browser_signs_in(hass, result)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "sign_in_failed"}
    assert "email" in result["description_placeholders"]


async def test_options_flow(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, no_setup: None
) -> None:
    """The scan interval can be changed."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {CONF_SCAN_INTERVAL: 120}


async def test_alexa_media_player_entries_are_never_read(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Another integration's signed-in entry never changes the sign-in or reauth steps."""
    MockConfigEntry(
        domain="alexa_media",
        data={CONF_EMAIL: EMAIL, "url": "amazon.com", "oauth": {"refresh_token": "Atnr|amp"}},
    ).add_to_hass(hass)
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"


@pytest.mark.parametrize("hass_url", ["homeassistant.local:8123", "ftp://ha.local", "/auth"])
async def test_invalid_hass_url_is_rejected(
    hass: HomeAssistant, flow_auth: MagicMock, hass_url: str
) -> None:
    """The browser needs a full http(s) address to open Amazon's page."""
    result = await _submit(hass, {**USER_INPUT, CONF_HASS_URL: hass_url})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HASS_URL: "invalid_hass_url"}
    flow_auth.assert_not_called()


async def test_hass_url_keeps_only_the_origin(hass: HomeAssistant, flow_auth: MagicMock) -> None:
    """A pasted dashboard address still opens the proxy at the root of Home Assistant."""
    result = await _submit(
        hass, {**USER_INPUT, CONF_HASS_URL: " https://ha.example.com/lovelace/0 "}
    )

    assert result["url"] == f"https://ha.example.com/auth/{DOMAIN}/proxy/{result['flow_id']}"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("http://192.168.1.5:8123", "http://192.168.1.5:8123"),
        (None, "http://homeassistant.local:8123"),
    ],
)
async def test_hass_url_is_prefilled(
    hass: HomeAssistant, configured: str | None, expected: str
) -> None:
    """The field starts with Home Assistant's own URL, or the usual local address."""
    side_effect = NoURLAvailableError if configured is None else None
    with patch(
        "custom_components.alexa_thermostat.config_flow.get_url",
        return_value=configured,
        side_effect=side_effect,
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }
    assert suggested == {CONF_HASS_URL: expected}


async def test_open_sign_in_does_not_block_a_new_attempt(
    hass: HomeAssistant, flow_auth: MagicMock
) -> None:
    """A sign-in left open in another dialog does not abort a second one."""
    first = await _submit(hass)
    second = await _submit(hass)

    assert first["type"] is FlowResultType.EXTERNAL_STEP
    assert second["type"] is FlowResultType.EXTERNAL_STEP
    assert first["flow_id"] != second["flow_id"]
