"""The Alexa Thermostat integration."""

from __future__ import annotations

from homeassistant.const import CONF_EMAIL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import AlexaAuth, AlexaAuthError, AlexaError, AlexaThermostatClient
from .const import CONF_AMAZON_DOMAIN, CONF_DEVICE_UUID, CONF_TOKENS
from .coordinator import (
    AlexaThermostatConfigEntry,
    AlexaThermostatCoordinator,
    AlexaThermostatData,
)

PLATFORMS: list[Platform] = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: AlexaThermostatConfigEntry) -> bool:
    """Set up Alexa Thermostat from a config entry."""
    auth = AlexaAuth(
        entry.data[CONF_AMAZON_DOMAIN],
        entry.data[CONF_EMAIL],
        hass.config.path,
        tokens=entry.data[CONF_TOKENS],
        device_uuid=entry.data[CONF_DEVICE_UUID],
    )
    client = AlexaThermostatClient(auth)
    try:
        await auth.async_resume()
        devices = await client.async_get_thermostats()
    except AlexaAuthError as err:
        await auth.async_close()
        raise ConfigEntryAuthFailed(str(err)) from err
    except AlexaError as err:
        await auth.async_close()
        raise ConfigEntryNotReady(str(err)) from err

    if auth.tokens != entry.data[CONF_TOKENS]:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKENS: auth.tokens})

    coordinator = AlexaThermostatCoordinator(hass, entry, client, devices)
    entry.runtime_data = AlexaThermostatData(auth, client, coordinator)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await auth.async_close()
        raise

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AlexaThermostatConfigEntry) -> bool:
    """Unload a config entry."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.auth.async_close()
    return unloaded
