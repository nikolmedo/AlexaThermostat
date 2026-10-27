"""Tests for the climate entity."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock

from homeassistant.components.climate import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    DOMAIN as CLIMATE_DOMAIN,
    PRESET_ECO,
    PRESET_NONE,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    HVACMode,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.alexa_thermostat.api import (
    AlexaCommandError,
    AlexaConnectionError,
    Temperature,
    TemperatureScale,
    ThermostatMode,
)
from custom_components.alexa_thermostat.api.parser import parse_thermostat_state

from .conftest import load_fixture

ENTITY_ID = "climate.living_room_thermostat"
F = TemperatureScale.FAHRENHEIT


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
) -> MagicMock:
    """Set up the integration with a Fahrenheit thermostat in HEAT mode."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_client


async def _call(hass: HomeAssistant, service: str, **data) -> None:
    await hass.services.async_call(
        CLIMATE_DOMAIN, service, {ATTR_ENTITY_ID: ENTITY_ID, **data}, blocking=True
    )


async def test_heat_state(hass: HomeAssistant, setup_integration: MagicMock) -> None:
    """The entity mirrors the Alexa state."""
    state = hass.states.get(ENTITY_ID)

    assert state.state == HVACMode.HEAT
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 71.5
    assert state.attributes[ATTR_TEMPERATURE] == 73
    assert state.attributes[ATTR_CURRENT_HUMIDITY] == 42
    assert state.attributes[ATTR_PRESET_MODE] == PRESET_NONE
    assert set(state.attributes["hvac_modes"]) == {
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.HEAT_COOL,
    }


async def test_set_temperature_sends_number(
    hass: HomeAssistant, setup_integration: MagicMock
) -> None:
    """Setting 74.4°F sends the rounded number 74, never a string."""
    await _call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 74.4})

    setup_integration.async_set_target_temperature.assert_awaited_once()
    sent: Temperature = setup_integration.async_set_target_temperature.call_args.args[1]
    assert sent == Temperature(74.0, F)
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 74


async def test_rejected_command_raises_and_keeps_state(
    hass: HomeAssistant, setup_integration: MagicMock
) -> None:
    """A BAD_REQUEST is reported to the user and nothing changes."""
    setup_integration.async_set_target_temperature.side_effect = AlexaCommandError(
        "rejected", ["BAD_REQUEST"]
    )
    with pytest.raises(HomeAssistantError) as err:
        await _call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 75})

    assert err.value.translation_key == "command_rejected"
    assert err.value.translation_placeholders == {"codes": "BAD_REQUEST"}
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 73


async def test_optimistic_value_survives_stale_poll(
    hass: HomeAssistant, setup_integration: MagicMock
) -> None:
    """A poll that still returns the old setpoint does not revert the change."""
    await _call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 75})

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    assert setup_integration.async_get_state.await_count >= 2
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 75


async def test_set_range_in_heat_cool(hass: HomeAssistant, setup_integration: MagicMock) -> None:
    """Low and high are sent together in one command."""
    await _call(
        hass,
        SERVICE_SET_TEMPERATURE,
        **{ATTR_TARGET_TEMP_LOW: 67.6, ATTR_TARGET_TEMP_HIGH: 77.2},
    )

    lower, upper = setup_integration.async_set_temperature_range.call_args.args[1:]
    assert lower == Temperature(68.0, F)
    assert upper == Temperature(77.0, F)


async def test_single_setpoint_rejected_in_auto(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
) -> None:
    """In AUTO mode a single target is a user error."""
    mock_client.async_get_state.return_value = parse_thermostat_state(
        load_fixture("state_auto_celsius.json")
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(ENTITY_ID)
    assert state.state == HVACMode.HEAT_COOL
    assert state.attributes[ATTR_TARGET_TEMP_LOW] == 20
    assert state.attributes[ATTR_TARGET_TEMP_HIGH] == 24.5
    assert state.attributes[ATTR_TEMPERATURE] is None

    with pytest.raises(ServiceValidationError):
        await _call(hass, SERVICE_SET_TEMPERATURE, **{ATTR_TEMPERATURE: 22})
    mock_client.async_set_target_temperature.assert_not_awaited()


async def test_set_hvac_mode(hass: HomeAssistant, setup_integration: MagicMock) -> None:
    """HEAT_COOL maps to the Alexa AUTO mode."""
    await _call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.HEAT_COOL})

    setup_integration.async_set_mode.assert_awaited_once()
    assert setup_integration.async_set_mode.call_args.args[1] is ThermostatMode.AUTO
    assert hass.states.get(ENTITY_ID).state == HVACMode.HEAT_COOL


async def test_turn_off_uses_mode_off(hass: HomeAssistant, setup_integration: MagicMock) -> None:
    """Turning off sends setThermostatMode OFF first."""
    await _call(hass, SERVICE_TURN_OFF)

    assert setup_integration.async_set_mode.call_args.args[1] is ThermostatMode.OFF
    setup_integration.async_turn_off.assert_not_awaited()
    assert hass.states.get(ENTITY_ID).state == HVACMode.OFF


async def test_turn_off_falls_back_to_power(
    hass: HomeAssistant, setup_integration: MagicMock
) -> None:
    """If Alexa rejects mode OFF, the power feature is used instead."""
    setup_integration.async_set_mode.side_effect = AlexaCommandError("no", ["INVALID_VALUE"])
    await _call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.OFF})

    setup_integration.async_turn_off.assert_awaited_once()
    assert hass.states.get(ENTITY_ID).state == HVACMode.OFF


async def test_power_off_state_turns_on_before_mode(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
) -> None:
    """A powered off thermostat is turned on before changing its mode."""
    heat = parse_thermostat_state(load_fixture("state_heat.json"))
    mock_client.async_get_state.return_value = heat.evolve(power_on=False)
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).state == HVACMode.OFF

    await _call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.COOL})

    mock_client.async_turn_on.assert_awaited_once()
    assert mock_client.async_set_mode.call_args.args[1] is ThermostatMode.COOL
    assert hass.states.get(ENTITY_ID).state == HVACMode.COOL


async def test_eco_preset(hass: HomeAssistant, setup_integration: MagicMock) -> None:
    """The eco preset maps to Alexa ECO and back to the last active mode."""
    await _call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_ECO})
    assert setup_integration.async_set_mode.call_args.args[1] is ThermostatMode.ECO
    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_PRESET_MODE] == PRESET_ECO
    assert state.state == HVACMode.HEAT

    await _call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_NONE})
    assert setup_integration.async_set_mode.call_args.args[1] is ThermostatMode.HEAT


async def test_unavailable_when_polling_fails(
    hass: HomeAssistant, setup_integration: MagicMock
) -> None:
    """The entity becomes unavailable while Alexa cannot be reached."""
    setup_integration.async_get_state.side_effect = AlexaConnectionError("down")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=61))
    await hass.async_block_till_done()

    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE


async def test_set_temperature_validates_before_changing_mode(
    hass: HomeAssistant, setup_integration: MagicMock
) -> None:
    """hvac_mode=heat_cool with a single target fails without touching the mode."""
    with pytest.raises(ServiceValidationError):
        await _call(
            hass,
            SERVICE_SET_TEMPERATURE,
            **{ATTR_HVAC_MODE: HVACMode.HEAT_COOL, ATTR_TEMPERATURE: 72},
        )

    setup_integration.async_set_mode.assert_not_awaited()
    setup_integration.async_set_target_temperature.assert_not_awaited()
    assert hass.states.get(ENTITY_ID).state == HVACMode.HEAT


async def test_set_temperature_with_mode_change(
    hass: HomeAssistant, setup_integration: MagicMock
) -> None:
    """hvac_mode=cool plus a target sends the mode, then the setpoint."""
    await _call(
        hass, SERVICE_SET_TEMPERATURE, **{ATTR_HVAC_MODE: HVACMode.COOL, ATTR_TEMPERATURE: 76}
    )

    assert setup_integration.async_set_mode.call_args.args[1] is ThermostatMode.COOL
    sent = setup_integration.async_set_target_temperature.call_args.args[1]
    assert sent == Temperature(76.0, F)
    state = hass.states.get(ENTITY_ID)
    assert state.state == HVACMode.COOL
    assert state.attributes[ATTR_TEMPERATURE] == 76


async def _setup_with_state(
    hass: HomeAssistant, entry: MockConfigEntry, client: MagicMock, state
) -> None:
    client.async_get_state.return_value = state
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_turn_on_shown_even_if_mode_change_fails(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
) -> None:
    """turnOn succeeded, setThermostatMode failed: the entity is no longer off."""
    heat = parse_thermostat_state(load_fixture("state_heat.json"))
    await _setup_with_state(hass, mock_config_entry, mock_client, heat.evolve(power_on=False))
    mock_client.async_set_mode.side_effect = AlexaCommandError("no", ["BAD_REQUEST"])

    with pytest.raises(HomeAssistantError):
        await _call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.COOL})

    mock_client.async_turn_on.assert_awaited_once()
    assert hass.states.get(ENTITY_ID).state == HVACMode.HEAT


async def test_no_power_on_without_turn_on_support(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
) -> None:
    """Without turnOn, no power command is sent and power is not faked as on."""
    devices = mock_client.async_get_thermostats.return_value
    mock_client.async_get_thermostats.return_value = [
        replace(devices[0], operations=devices[0].operations - {"turnOn"})
    ]
    heat = parse_thermostat_state(load_fixture("state_heat.json"))
    await _setup_with_state(hass, mock_config_entry, mock_client, heat.evolve(power_on=False))

    await _call(hass, SERVICE_SET_HVAC_MODE, **{ATTR_HVAC_MODE: HVACMode.COOL})

    mock_client.async_turn_on.assert_not_awaited()
    assert hass.states.get(ENTITY_ID).state == HVACMode.OFF


async def test_eco_with_unknown_previous_mode(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_auth: MagicMock,
    mock_client: MagicMock,
) -> None:
    """Starting in ECO, leaving the preset needs an explicit mode; turn_on uses heat/cool."""
    heat = parse_thermostat_state(load_fixture("state_heat.json"))
    await _setup_with_state(
        hass, mock_config_entry, mock_client, heat.evolve(mode=ThermostatMode.ECO)
    )
    assert hass.states.get(ENTITY_ID).attributes[ATTR_PRESET_MODE] == PRESET_ECO

    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, SERVICE_SET_PRESET_MODE, **{ATTR_PRESET_MODE: PRESET_NONE})
    assert err.value.translation_key == "unknown_previous_mode"
    mock_client.async_set_mode.assert_not_awaited()

    await _call(hass, SERVICE_TURN_ON)
    assert mock_client.async_set_mode.call_args.args[1] is ThermostatMode.AUTO
