"""Pure functions that turn Alexa GraphQL responses into models.

Nothing in here performs I/O, so every function can be tested with JSON
fixtures (see tests/fixtures).
"""

from __future__ import annotations

from typing import Any

from .exceptions import AlexaApiError, AlexaCommandError
from .models import (
    Operation,
    Temperature,
    TemperatureScale,
    ThermostatDevice,
    ThermostatMode,
    ThermostatState,
)

THERMOSTAT_CATEGORY = "THERMOSTAT"
HUMIDITY_RANGE_NAME = "indoor humidity"


def raise_for_graphql_errors(response: dict[str, Any]) -> None:
    """Raise AlexaApiError when a GraphQL response carries top-level errors."""
    errors = response.get("errors") or []
    if not errors:
        return
    codes = [_error_code(error) for error in errors]
    raise AlexaApiError(f"Alexa GraphQL error: {', '.join(codes)}", codes)


def parse_thermostats(response: dict[str, Any]) -> list[ThermostatDevice]:
    """Return the thermostats found in an Endpoints query response."""
    raise_for_graphql_errors(response)
    items = _dig(response, "data", "endpoints", "items")
    if not isinstance(items, list):
        raise AlexaApiError("Endpoints response has no items")

    devices: list[ThermostatDevice] = []
    for item in items:
        if _dig(item, "displayCategories", "primary", "value") != THERMOSTAT_CATEGORY:
            continue
        if item.get("enablement") not in (None, "ENABLED"):
            continue
        operations = frozenset(
            operation["name"]
            for feature in item.get("features") or []
            for operation in feature.get("operations") or []
            if operation.get("name")
        )
        if Operation.SET_TARGET_SETPOINT.value not in operations:
            continue
        devices.append(
            ThermostatDevice(
                endpoint_id=item["id"],
                name=item.get("friendlyName") or "Thermostat",
                operations=operations,
                manufacturer=_dig(item, "manufacturer", "value", "text"),
                model=_dig(item, "model", "value", "text"),
                serial_number=_dig(item, "serialNumber", "value", "text"),
            )
        )
    return devices


def parse_thermostat_state(response: dict[str, Any]) -> ThermostatState:
    """Return the thermostat state found in a getThermostatStates response."""
    raise_for_graphql_errors(response)
    features = _dig(response, "data", "endpoint", "features")
    if not isinstance(features, list):
        raise AlexaApiError("State not available")

    values: dict[str, Any] = {}
    for feature in features:
        feature_name = feature.get("name")
        for prop in feature.get("properties") or []:
            _read_property(feature, feature_name, prop, values)

    if not values:
        raise AlexaApiError("State not available")
    return ThermostatState(**values)


def raise_for_command_errors(response: dict[str, Any]) -> None:
    """Raise AlexaCommandError when setEndpointFeatures reports errors.

    Alexa answers HTTP 200 even when it rejects a command, so the errors
    array is the only reliable signal (this is how issue #284 went unnoticed
    in the Homebridge plugin).
    """
    raise_for_graphql_errors(response)
    result = _dig(response, "data", "setEndpointFeatures")
    if not isinstance(result, dict):
        raise AlexaCommandError("Command response is missing setEndpointFeatures")
    errors = result.get("errors") or []
    if errors:
        codes = [_error_code(error) for error in errors]
        raise AlexaCommandError(f"Alexa rejected the command: {', '.join(codes)}", codes)


def _read_property(
    feature: dict[str, Any],
    feature_name: str | None,
    prop: dict[str, Any],
    values: dict[str, Any],
) -> None:
    name = prop.get("name")
    if feature_name == "temperatureSensor" and (temp := _temperature(prop)):
        values["current_temperature"] = temp
    elif name == "targetSetpoint" and (temp := _temperature(prop)):
        values["target_setpoint"] = temp
    elif name == "lowerSetpoint" and (temp := _temperature(prop)):
        values["lower_setpoint"] = temp
    elif name == "upperSetpoint" and (temp := _temperature(prop)):
        values["upper_setpoint"] = temp
    elif name == "thermostatMode" and (raw := prop.get("thermostatModeValue")):
        values["raw_mode"] = raw
        values["mode"] = _mode(raw)
    elif feature_name == "power" and (power := prop.get("powerStateValue")):
        values["power_on"] = power == "ON"
    elif feature_name == "range" and _is_humidity(feature):
        humidity = _number(_dig(prop, "rangeValue", "value"))
        if humidity is not None:
            values["humidity"] = humidity


def _temperature(prop: dict[str, Any]) -> Temperature | None:
    reading = prop.get("value")
    if not isinstance(reading, dict):
        return None
    value = _number(reading.get("value"))
    try:
        scale = TemperatureScale(str(reading.get("scale", "")).upper())
    except ValueError:
        return None
    return None if value is None else Temperature(value, scale)


def _mode(raw: str) -> ThermostatMode | None:
    try:
        return ThermostatMode(raw.upper())
    except ValueError:
        return None


def _is_humidity(feature: dict[str, Any]) -> bool:
    name = _dig(feature, "configuration", "friendlyName", "value", "text")
    return isinstance(name, str) and name.strip().lower() == HUMIDITY_RANGE_NAME


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _error_code(error: Any) -> str:
    if not isinstance(error, dict):
        return str(error)
    return str(
        error.get("code") or _dig(error, "extensions", "code") or error.get("message") or "UNKNOWN"
    )


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data
