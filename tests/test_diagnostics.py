"""Tests for diagnostics."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alexa_thermostat.diagnostics import async_get_config_entry_diagnostics

from .conftest import EMAIL, ENDPOINT_ID


async def test_diagnostics_redacts_secrets(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
) -> None:
    """Tokens, email and identifiers never show up in diagnostics."""
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    dumped = json.dumps(result)

    for secret in (EMAIL, "Atnr|refresh", "Atna|access", ENDPOINT_ID, "G0123456789"):
        assert secret not in dumped
    thermostat = result["thermostats"][0]
    assert thermostat["device"]["name"] == "Living Room Thermostat"
    assert "setTargetSetpoint" in thermostat["device"]["operations"]
    assert thermostat["state"]["mode"] == "HEAT"
