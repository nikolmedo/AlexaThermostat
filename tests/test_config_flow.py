"""Tests for the config, reauth and options flows."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alexa_thermostat.api import (
    CHALLENGE_CAPTCHA,
    CHALLENGE_SECURITY_CODE,
    CHALLENGE_UNSUPPORTED,
    AlexaAuthError,
    AlexaConnectionError,
    AlexaInvalidOtpSecret,
    AlexaLoginChallenge,
)
from custom_components.alexa_thermostat.api.parser import parse_thermostats
from custom_components.alexa_thermostat.const import (
    CONF_AMAZON_DOMAIN,
    CONF_CAPTCHA,
    CONF_DEVICE_UUID,
    CONF_OTP_SECRET,
    CONF_SCAN_INTERVAL,
    CONF_SECURITY_CODE,
    CONF_TOKENS,
    DOMAIN,
)

from .conftest import EMAIL, TOKENS, load_fixture

USER_INPUT = {
    CONF_EMAIL: EMAIL,
    CONF_PASSWORD: "secret",
    CONF_AMAZON_DOMAIN: "amazon.com",
}
NEW_UUID = "fedcba9876543210fedcba9876543210"


@pytest.fixture
def flow_auth() -> Generator[MagicMock]:
    """Patch AlexaAuth inside the config flow."""
    with patch(
        "custom_components.alexa_thermostat.config_flow.AlexaAuth", autospec=True
    ) as auth_cls:
        auth = auth_cls.return_value
        auth.async_login = AsyncMock()
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


async def _start(hass: HomeAssistant, user_input: dict | None = None):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input or USER_INPUT
    )


async def test_user_flow_creates_entry(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock, no_setup: None
) -> None:
    """A successful login stores tokens and uuid, never the password."""
    result = await _start(hass, {**USER_INPUT, CONF_OTP_SECRET: "ABCD EFGH"})

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
    assert flow_auth.call_args.kwargs["otp_secret"] == "ABCDEFGH"
    flow_auth.return_value.async_login.assert_awaited_once_with({CONF_PASSWORD: "secret"})
    flow_auth.return_value.async_close.assert_awaited()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AlexaAuthError("nope"), "invalid_auth"),
        (AlexaConnectionError("down"), "cannot_connect"),
        (AlexaLoginChallenge(CHALLENGE_UNSUPPORTED), "unsupported_challenge"),
        (AlexaInvalidOtpSecret("bad"), "invalid_otp_secret"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant,
    flow_auth: MagicMock,
    flow_client: MagicMock,
    no_setup: None,
    error: Exception,
    expected: str,
) -> None:
    """Login errors are shown on the form and the user can retry."""
    flow_auth.return_value.async_login.side_effect = [error, None]
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_invalid_otp_secret_on_field(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock, no_setup: None
) -> None:
    """An invalid key detected while building the login points at the field."""
    flow_auth.side_effect = AlexaInvalidOtpSecret("bad base32")
    result = await _start(hass, {**USER_INPUT, CONF_OTP_SECRET: "not-base32!"})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_OTP_SECRET: "invalid_otp_secret"}


async def test_security_code_step(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock, no_setup: None
) -> None:
    """Amazon asking for an OTP shows a second step."""
    flow_auth.return_value.async_login.side_effect = [
        AlexaLoginChallenge(CHALLENGE_SECURITY_CODE),
        None,
    ]
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "security_code"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SECURITY_CODE: " 123456 "}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    flow_auth.return_value.async_login.assert_awaited_with({"securitycode": "123456"})


async def test_captcha_step(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock, no_setup: None
) -> None:
    """A captcha shows the image and sends the answer."""
    flow_auth.return_value.async_login.side_effect = [
        AlexaLoginChallenge(CHALLENGE_CAPTCHA, "https://images.example/captcha.jpg"),
        None,
    ]
    result = await _start(hass)
    assert result["step_id"] == "captcha"
    assert result["description_placeholders"] == {
        "captcha_url": "https://images.example/captcha.jpg"
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CAPTCHA: "AbC12"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    flow_auth.return_value.async_login.assert_awaited_with({"captcha": "AbC12"})


async def test_no_thermostats_aborts(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock
) -> None:
    """An account without thermostats is not added."""
    flow_client.async_get_thermostats.return_value = []
    result = await _start(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_thermostats"


async def test_duplicate_account_aborts(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, flow_auth: MagicMock
) -> None:
    """The same account cannot be added twice."""
    mock_config_entry.add_to_hass(hass)
    result = await _start(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    flow_auth.return_value.async_login.assert_not_awaited()


async def test_reauth_keeps_device_uuid(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    flow_auth: MagicMock,
    flow_client: MagicMock,
    no_setup: None,
) -> None:
    """Reauth reuses the stored uuid and replaces the tokens."""
    mock_config_entry.add_to_hass(hass)
    new_tokens = {"refresh_token": "Atnr|new"}
    flow_auth.return_value.tokens = new_tokens
    flow_auth.return_value.device_uuid = mock_config_entry.data[CONF_DEVICE_UUID]

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new-secret"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert flow_auth.call_args.kwargs["device_uuid"] == mock_config_entry.data[CONF_DEVICE_UUID]
    assert mock_config_entry.data[CONF_TOKENS] == new_tokens


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
