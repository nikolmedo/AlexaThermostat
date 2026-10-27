"""Tests for setting up and unloading the integration."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alexa_thermostat.api import (
    AlexaApiError,
    AlexaAuthError,
    AlexaConnectionError,
)
from custom_components.alexa_thermostat.const import CONF_TOKENS, DOMAIN


async def test_setup_and_unload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
) -> None:
    """The entry loads, creates one climate entity and unloads cleanly."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert len(hass.states.async_entity_ids("climate")) == 1
    mock_auth.async_resume.assert_awaited_once()

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    mock_auth.async_close.assert_awaited()


async def test_setup_persists_refreshed_tokens(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
) -> None:
    """Tokens refreshed during setup are saved to the entry."""
    mock_auth.tokens = {"refresh_token": "Atnr|refresh", "access_token": "Atna|fresh"}
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    assert mock_config_entry.data[CONF_TOKENS] == mock_auth.tokens


async def test_auth_error_starts_reauth(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
) -> None:
    """A rejected refresh token asks the user to sign in again."""
    mock_auth.async_resume.side_effect = AlexaAuthError("expired")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    mock_auth.async_close.assert_awaited()


@pytest.mark.parametrize("error", [AlexaConnectionError("down"), AlexaApiError("bad")])
async def test_api_error_retries_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
    error: Exception,
) -> None:
    """Temporary failures retry later."""
    mock_client.async_get_thermostats.side_effect = error
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    mock_auth.async_close.assert_awaited()


async def test_first_refresh_failure_retries_setup(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
) -> None:
    """A failing first state read retries the setup."""
    mock_client.async_get_state.side_effect = AlexaConnectionError("down")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY
    mock_auth.async_close.assert_awaited()
