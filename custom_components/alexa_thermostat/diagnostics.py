"""Diagnostics for Alexa Thermostat (attach these to bug reports)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_UUID, CONF_TOKENS
from .coordinator import AlexaThermostatConfigEntry

TO_REDACT = {CONF_EMAIL, CONF_TOKENS, CONF_DEVICE_UUID, "serial_number", "endpoint_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AlexaThermostatConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    states = coordinator.data or {}
    thermostats = [
        {
            "device": {**asdict(device), "operations": sorted(device.operations)},
            "state": asdict(states[endpoint_id]) if endpoint_id in states else None,
        }
        for endpoint_id, device in coordinator.devices.items()
    ]
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "thermostats": async_redact_data(thermostats, TO_REDACT),
    }
