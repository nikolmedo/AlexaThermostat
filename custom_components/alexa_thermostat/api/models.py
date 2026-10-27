"""Data models for Alexa thermostat endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class TemperatureScale(StrEnum):
    """Temperature scales reported by Alexa."""

    CELSIUS = "CELSIUS"
    FAHRENHEIT = "FAHRENHEIT"
    KELVIN = "KELVIN"


class ThermostatMode(StrEnum):
    """Thermostat modes used by the Alexa ThermostatController."""

    HEAT = "HEAT"
    COOL = "COOL"
    AUTO = "AUTO"
    ECO = "ECO"
    OFF = "OFF"


class Operation(StrEnum):
    """Feature operations the client knows how to send."""

    SET_TARGET_SETPOINT = "setTargetSetpoint"
    SET_THERMOSTAT_MODE = "setThermostatMode"
    TURN_ON = "turnOn"
    TURN_OFF = "turnOff"


@dataclass(frozen=True, slots=True)
class Temperature:
    """A temperature value with the scale Alexa reported it in."""

    value: float
    scale: TemperatureScale

    def to(self, scale: TemperatureScale) -> Temperature:
        """Return this temperature converted to another scale."""
        if scale == self.scale:
            return self
        celsius = _to_celsius(self.value, self.scale)
        return Temperature(_from_celsius(celsius, scale), scale)


def _to_celsius(value: float, scale: TemperatureScale) -> float:
    if scale == TemperatureScale.FAHRENHEIT:
        return (value - 32) / 1.8
    if scale == TemperatureScale.KELVIN:
        return value - 273.15
    return value


def _from_celsius(value: float, scale: TemperatureScale) -> float:
    if scale == TemperatureScale.FAHRENHEIT:
        return value * 1.8 + 32
    if scale == TemperatureScale.KELVIN:
        return value + 273.15
    return value


@dataclass(frozen=True, slots=True)
class ThermostatDevice:
    """A thermostat endpoint discovered on the Alexa account."""

    endpoint_id: str
    name: str
    operations: frozenset[str]
    manufacturer: str | None = None
    model: str | None = None
    serial_number: str | None = None

    def supports(self, operation: Operation) -> bool:
        """Return True when the endpoint advertises an operation."""
        return operation.value in self.operations


@dataclass(frozen=True, slots=True)
class ThermostatState:
    """The state of one thermostat as read from Alexa."""

    current_temperature: Temperature | None = None
    target_setpoint: Temperature | None = None
    lower_setpoint: Temperature | None = None
    upper_setpoint: Temperature | None = None
    mode: ThermostatMode | None = None
    power_on: bool | None = None
    humidity: float | None = None
    raw_mode: str | None = field(default=None, compare=False)

    @property
    def scale(self) -> TemperatureScale | None:
        """Return the scale the thermostat reports its readings in."""
        for reading in (
            self.current_temperature,
            self.target_setpoint,
            self.lower_setpoint,
            self.upper_setpoint,
        ):
            if reading is not None:
                return reading.scale
        return None

    def evolve(self, **changes: object) -> ThermostatState:
        """Return a copy with some fields replaced."""
        return replace(self, **changes)
