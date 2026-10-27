"""Polling coordinator for Alexa thermostats."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AlexaAuth,
    AlexaAuthError,
    AlexaError,
    AlexaThermostatClient,
    ThermostatDevice,
    ThermostatState,
)
from .const import COMMAND_SETTLE_DELAY, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type AlexaThermostatConfigEntry = ConfigEntry[AlexaThermostatData]


@dataclass
class AlexaThermostatData:
    """Runtime data stored on the config entry."""

    auth: AlexaAuth
    client: AlexaThermostatClient
    coordinator: AlexaThermostatCoordinator


@dataclass
class _PendingChange:
    changes: dict[str, Any]
    expires: datetime = field(default_factory=lambda: dt_util.utcnow() + COMMAND_SETTLE_DELAY)


class AlexaThermostatCoordinator(DataUpdateCoordinator[dict[str, ThermostatState]]):
    """Fetch the state of every thermostat on the account."""

    config_entry: AlexaThermostatConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AlexaThermostatConfigEntry,
        client: AlexaThermostatClient,
        devices: list[ThermostatDevice],
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
            always_update=False,
        )
        self.client = client
        self.devices = {device.endpoint_id: device for device in devices}
        self._pending: dict[str, _PendingChange] = {}

    async def _async_update_data(self) -> dict[str, ThermostatState]:
        states: dict[str, ThermostatState] = {}
        errors: list[AlexaError] = []
        for endpoint_id in self.devices:
            try:
                fetched = await self.client.async_get_state(endpoint_id)
            except AlexaAuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except AlexaError as err:
                # Leave this thermostat out (its entity becomes unavailable)
                # instead of failing the update for every thermostat.
                _LOGGER.debug("Could not read %s: %s", endpoint_id, err)
                errors.append(err)
                continue
            states[endpoint_id] = self._merge_pending(endpoint_id, fetched)
        if errors and not states:
            raise UpdateFailed(str(errors[0])) from errors[0]
        return states

    def apply_optimistic(self, endpoint_id: str, **changes: Any) -> None:
        """Show a confirmed command right away.

        Alexa accepted the command, but its state API can lag for a few
        seconds. Without this the next poll would briefly revert the value.
        """
        pending = self._pending.get(endpoint_id)
        merged = {**(pending.changes if pending else {}), **changes}
        self._pending[endpoint_id] = _PendingChange(merged)
        if self.data is None or endpoint_id not in self.data:
            return
        self.async_set_updated_data(
            {**self.data, endpoint_id: self.data[endpoint_id].evolve(**changes)}
        )

    def _merge_pending(self, endpoint_id: str, fetched: ThermostatState) -> ThermostatState:
        pending = self._pending.get(endpoint_id)
        if pending is None:
            return fetched
        caught_up = all(getattr(fetched, key) == value for key, value in pending.changes.items())
        if caught_up or dt_util.utcnow() >= pending.expires:
            del self._pending[endpoint_id]
            return fetched
        return fetched.evolve(**pending.changes)
