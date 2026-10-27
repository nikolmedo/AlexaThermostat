"""Tests for polling and the optimistic overlay in the coordinator."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alexa_thermostat.api import (
    AlexaApiError,
    Temperature,
    TemperatureScale,
    ThermostatDevice,
)
from custom_components.alexa_thermostat.api.parser import parse_thermostat_state
from custom_components.alexa_thermostat.const import COMMAND_SETTLE_DELAY
from custom_components.alexa_thermostat.coordinator import AlexaThermostatCoordinator

from .conftest import ENDPOINT_ID, load_fixture

SECOND_ID = "amzn1.alexa.endpoint.thermostat-2"
F = TemperatureScale.FAHRENHEIT


def _device(endpoint_id: str) -> ThermostatDevice:
    return ThermostatDevice(endpoint_id, endpoint_id, frozenset({"setTargetSetpoint"}))


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, *endpoint_ids: str
) -> tuple[AlexaThermostatCoordinator, MagicMock]:
    entry.add_to_hass(hass)
    client = MagicMock()
    client.async_get_state = AsyncMock(
        return_value=parse_thermostat_state(load_fixture("state_heat.json"))
    )
    devices = [_device(endpoint_id) for endpoint_id in endpoint_ids]
    return AlexaThermostatCoordinator(hass, entry, client, devices), client


async def test_one_failing_thermostat_keeps_the_others(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """An offline thermostat is left out instead of failing the whole update."""
    coordinator, client = _coordinator(hass, mock_config_entry, ENDPOINT_ID, SECOND_ID)
    healthy = parse_thermostat_state(load_fixture("state_heat.json"))
    client.async_get_state.side_effect = [AlexaApiError("State not available"), healthy]

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data == {SECOND_ID: healthy}


async def test_every_thermostat_failing_fails_the_update(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """When nothing could be read the update fails."""
    coordinator, client = _coordinator(hass, mock_config_entry, ENDPOINT_ID, SECOND_ID)
    client.async_get_state.side_effect = AlexaApiError("State not available")

    await coordinator.async_refresh()

    assert not coordinator.last_update_success


async def test_optimistic_value_expires(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    """A stale poll keeps the new value inside the settle window, then Alexa wins."""
    coordinator, _ = _coordinator(hass, mock_config_entry, ENDPOINT_ID)
    await coordinator.async_refresh()
    new_target = Temperature(75.0, F)

    coordinator.apply_optimistic(ENDPOINT_ID, target_setpoint=new_target)
    assert coordinator.data[ENDPOINT_ID].target_setpoint == new_target

    freezer.tick(COMMAND_SETTLE_DELAY - timedelta(seconds=1))
    await coordinator.async_refresh()
    assert coordinator.data[ENDPOINT_ID].target_setpoint == new_target

    freezer.tick(timedelta(seconds=2))
    await coordinator.async_refresh()
    assert coordinator.data[ENDPOINT_ID].target_setpoint == Temperature(73.0, F)


async def test_optimistic_value_clears_when_alexa_catches_up(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Once Alexa reports the new value, the overlay is dropped."""
    coordinator, client = _coordinator(hass, mock_config_entry, ENDPOINT_ID)
    await coordinator.async_refresh()
    heat = parse_thermostat_state(load_fixture("state_heat.json"))
    new_target = Temperature(75.0, F)

    coordinator.apply_optimistic(ENDPOINT_ID, target_setpoint=new_target)
    client.async_get_state.return_value = heat.evolve(target_setpoint=new_target)
    await coordinator.async_refresh()

    # Alexa later moves the setpoint again (for example from the Alexa app):
    # the overlay is gone, so the new reading shows immediately.
    later = Temperature(70.0, F)
    client.async_get_state.return_value = heat.evolve(target_setpoint=later)
    await coordinator.async_refresh()
    assert coordinator.data[ENDPOINT_ID].target_setpoint == later
