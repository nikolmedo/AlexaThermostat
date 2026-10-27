"""Tests for the pure GraphQL response parser."""

from __future__ import annotations

import pytest

from custom_components.alexa_thermostat.api import (
    AlexaApiError,
    AlexaCommandError,
    Temperature,
    TemperatureScale,
    ThermostatMode,
)
from custom_components.alexa_thermostat.api.parser import (
    parse_thermostat_state,
    parse_thermostats,
    raise_for_command_errors,
)

from .conftest import ENDPOINT_ID, load_fixture


def test_parse_thermostats_keeps_only_enabled_controllable_thermostats() -> None:
    """Lights, disabled thermostats and sensor-only endpoints are skipped."""
    devices = parse_thermostats(load_fixture("endpoints.json"))

    assert [device.endpoint_id for device in devices] == [ENDPOINT_ID]
    device = devices[0]
    assert device.name == "Living Room Thermostat"
    assert device.manufacturer == "Amazon"
    assert device.model == "Amazon Smart Thermostat"
    assert device.serial_number == "G0123456789"
    assert {"setTargetSetpoint", "setThermostatMode", "turnOff"} <= device.operations


def test_parse_thermostats_raises_on_graphql_errors() -> None:
    """Top-level GraphQL errors are surfaced, not swallowed."""
    with pytest.raises(AlexaApiError) as err:
        parse_thermostats({"errors": [{"message": "boom", "extensions": {"code": "UNAUTH"}}]})
    assert err.value.codes == ["UNAUTH"]


def test_parse_heat_state_in_fahrenheit() -> None:
    """Every property of a HEAT state is read, string setpoints included."""
    state = parse_thermostat_state(load_fixture("state_heat.json"))

    assert state.current_temperature == Temperature(71.5, TemperatureScale.FAHRENHEIT)
    assert state.target_setpoint == Temperature(73.0, TemperatureScale.FAHRENHEIT)
    assert state.lower_setpoint == Temperature(68.0, TemperatureScale.FAHRENHEIT)
    assert state.upper_setpoint == Temperature(76.0, TemperatureScale.FAHRENHEIT)
    assert state.mode is ThermostatMode.HEAT
    assert state.power_on is True
    assert state.humidity == 42
    assert state.scale is TemperatureScale.FAHRENHEIT


def test_parse_auto_state_in_celsius() -> None:
    """AUTO mode reports a lower and an upper setpoint."""
    state = parse_thermostat_state(load_fixture("state_auto_celsius.json"))

    assert state.mode is ThermostatMode.AUTO
    assert state.lower_setpoint == Temperature(20.0, TemperatureScale.CELSIUS)
    assert state.upper_setpoint == Temperature(24.5, TemperatureScale.CELSIUS)
    assert state.target_setpoint is None
    assert state.power_on is None


def test_parse_unknown_mode_keeps_raw_value() -> None:
    """An unknown mode does not crash the parser."""
    response = {
        "data": {
            "endpoint": {
                "features": [
                    {
                        "name": "thermostat",
                        "properties": [{"name": "thermostatMode", "thermostatModeValue": "CUSTOM"}],
                    }
                ]
            }
        }
    }
    state = parse_thermostat_state(response)
    assert state.mode is None
    assert state.raw_mode == "CUSTOM"


@pytest.mark.parametrize(
    "response",
    [
        {"data": None},
        {"data": {"endpoint": None}},
        {"data": {"endpoint": {"features": []}}},
    ],
)
def test_parse_state_not_available(response: dict) -> None:
    """An empty state is an error, like the Homebridge plugin."""
    with pytest.raises(AlexaApiError, match="State not available"):
        parse_thermostat_state(response)


def test_command_success_passes() -> None:
    """A response without errors is accepted."""
    raise_for_command_errors(load_fixture("set_success.json"))


def test_command_errors_are_raised() -> None:
    """Alexa answers HTTP 200 with an errors array when it rejects a command."""
    with pytest.raises(AlexaCommandError) as err:
        raise_for_command_errors(load_fixture("set_bad_request.json"))
    assert err.value.codes == ["BAD_REQUEST"]


def test_temperature_conversion() -> None:
    """Temperatures convert between scales."""
    assert Temperature(212, TemperatureScale.FAHRENHEIT).to(TemperatureScale.CELSIUS).value == (
        pytest.approx(100)
    )
    assert Temperature(20, TemperatureScale.CELSIUS).to(TemperatureScale.FAHRENHEIT).value == (
        pytest.approx(68)
    )


@pytest.mark.parametrize("name", ["Luftfeuchtigkeit", "Humidité intérieure", "室内湿度"])
def test_localized_humidity_name(name: str) -> None:
    """Humidity is found when Amazon localizes the range name."""
    response = {
        "data": {
            "endpoint": {
                "features": [
                    {
                        "name": "range",
                        "properties": [{"name": "rangeValue", "rangeValue": {"value": 55}}],
                        "configuration": {"friendlyName": {"value": {"text": name}}},
                    },
                    {
                        "name": "range",
                        "properties": [{"name": "rangeValue", "rangeValue": {"value": 3}}],
                        "configuration": {"friendlyName": {"value": {"text": "Fan speed"}}},
                    },
                ]
            }
        }
    }
    assert parse_thermostat_state(response).humidity == 55
