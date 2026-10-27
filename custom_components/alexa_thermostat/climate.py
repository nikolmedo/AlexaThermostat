"""Climate entity for Alexa thermostats."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, NoReturn

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    PRESET_ECO,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    AlexaAuthError,
    AlexaCommandError,
    AlexaError,
    Operation,
    Temperature,
    TemperatureScale,
    ThermostatDevice,
    ThermostatMode,
    ThermostatState,
)
from .const import DOMAIN
from .coordinator import AlexaThermostatConfigEntry, AlexaThermostatCoordinator

PARALLEL_UPDATES = 1

ALEXA_TO_HVAC: dict[ThermostatMode, HVACMode] = {
    ThermostatMode.HEAT: HVACMode.HEAT,
    ThermostatMode.COOL: HVACMode.COOL,
    ThermostatMode.AUTO: HVACMode.HEAT_COOL,
    ThermostatMode.OFF: HVACMode.OFF,
}
HVAC_TO_ALEXA: dict[HVACMode, ThermostatMode] = {
    hvac: alexa for alexa, hvac in ALEXA_TO_HVAC.items()
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlexaThermostatConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one climate entity per thermostat."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        AlexaThermostatClimate(coordinator, device) for device in coordinator.devices.values()
    )


class AlexaThermostatClimate(CoordinatorEntity[AlexaThermostatCoordinator], ClimateEntity):
    """An Alexa thermostat exposed as a Home Assistant climate entity."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_precision = PRECISION_TENTHS
    _attr_preset_modes = [PRESET_NONE, PRESET_ECO]

    def __init__(self, coordinator: AlexaThermostatCoordinator, device: ThermostatDevice) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._device = device
        self._last_active_mode = ThermostatMode.AUTO
        self._remember_active_mode()
        self._attr_unique_id = device.endpoint_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.endpoint_id)},
            name=device.name,
            manufacturer=device.manufacturer or "Amazon",
            model=device.model,
            serial_number=device.serial_number,
        )

        can_change_mode = device.supports(Operation.SET_THERMOSTAT_MODE)
        self._attr_hvac_modes = list(HVAC_TO_ALEXA) if can_change_mode else [HVACMode.HEAT_COOL]
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        )
        if can_change_mode:
            features |= (
                ClimateEntityFeature.TURN_ON
                | ClimateEntityFeature.TURN_OFF
                | ClimateEntityFeature.PRESET_MODE
            )
        self._attr_supported_features = features

    @property
    def _state(self) -> ThermostatState | None:
        return (self.coordinator.data or {}).get(self._device.endpoint_id)

    @property
    def _scale(self) -> TemperatureScale:
        state = self._state
        return (state.scale if state else None) or TemperatureScale.CELSIUS

    @property
    def available(self) -> bool:
        """Return True when the last poll returned this thermostat."""
        return super().available and self._state is not None

    @property
    def temperature_unit(self) -> str:
        """Return the unit Alexa reports in, Home Assistant converts the rest."""
        if self._scale == TemperatureScale.FAHRENHEIT:
            return UnitOfTemperature.FAHRENHEIT
        return UnitOfTemperature.CELSIUS

    @property
    def target_temperature_step(self) -> float:
        """Return the setpoint step."""
        return 1.0 if self._scale == TemperatureScale.FAHRENHEIT else 0.5

    @property
    def current_temperature(self) -> float | None:
        """Return the measured temperature."""
        return self._value((self._state or ThermostatState()).current_temperature)

    @property
    def current_humidity(self) -> float | None:
        """Return the indoor humidity when the thermostat reports it."""
        return (self._state or ThermostatState()).humidity

    @property
    def target_temperature(self) -> float | None:
        """Return the single setpoint (not used in heat/cool mode)."""
        state = self._state
        if state is None or self._alexa_mode == ThermostatMode.AUTO:
            return None
        return self._value(state.target_setpoint)

    @property
    def target_temperature_low(self) -> float | None:
        """Return the lower setpoint in heat/cool mode."""
        state = self._state
        if state is None or self._alexa_mode != ThermostatMode.AUTO:
            return None
        return self._value(state.lower_setpoint)

    @property
    def target_temperature_high(self) -> float | None:
        """Return the upper setpoint in heat/cool mode."""
        state = self._state
        if state is None or self._alexa_mode != ThermostatMode.AUTO:
            return None
        return self._value(state.upper_setpoint)

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return the current HVAC mode."""
        state = self._state
        if state is None:
            return None
        if state.power_on is False:
            return HVACMode.OFF
        mode = self._alexa_mode
        return ALEXA_TO_HVAC.get(mode) if mode else None

    @property
    def preset_mode(self) -> str | None:
        """Return eco when the thermostat is in ECO mode."""
        state = self._state
        if state is None:
            return None
        return PRESET_ECO if state.mode == ThermostatMode.ECO else PRESET_NONE

    @property
    def _alexa_mode(self) -> ThermostatMode | None:
        """Return the active mode, with ECO reported as the last real mode."""
        state = self._state
        if state is None or state.mode is None:
            return None
        if state.mode == ThermostatMode.ECO:
            return self._last_active_mode
        return state.mode

    def _handle_coordinator_update(self) -> None:
        self._remember_active_mode()
        super()._handle_coordinator_update()

    def _remember_active_mode(self) -> None:
        """Track the last real mode so ECO and turn_on can return to it."""
        state = self._state
        if state and state.mode in (ThermostatMode.HEAT, ThermostatMode.COOL, ThermostatMode.AUTO):
            self._last_active_mode = state.mode

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Change the HVAC mode."""
        target = HVAC_TO_ALEXA[hvac_mode]
        client = self.coordinator.client

        if target == ThermostatMode.OFF:
            try:
                await client.async_set_mode(self._device, ThermostatMode.OFF)
            except AlexaCommandError as err:
                # Some thermostats only turn off through the power feature
                # (homebridge-alexa-smarthome issue #260).
                if not self._device.supports(Operation.TURN_OFF):
                    self._raise_ha_error(err)
                await self._run(client.async_turn_off(self._device))
                self.coordinator.apply_optimistic(self._device.endpoint_id, power_on=False)
                return
            except AlexaError as err:
                self._raise_ha_error(err)
            self.coordinator.apply_optimistic(self._device.endpoint_id, mode=ThermostatMode.OFF)
            return

        state = self._state
        if state and state.power_on is False and self._device.supports(Operation.TURN_ON):
            await self._run(client.async_turn_on(self._device))
        await self._run(client.async_set_mode(self._device, target))
        self._last_active_mode = target
        self.coordinator.apply_optimistic(
            self._device.endpoint_id, mode=target, **self._power_on_change()
        )

    async def async_turn_on(self) -> None:
        """Turn the thermostat on in its last active mode."""
        await self.async_set_hvac_mode(ALEXA_TO_HVAC[self._last_active_mode])

    async def async_turn_off(self) -> None:
        """Turn the thermostat off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Enter or leave ECO mode."""
        target = ThermostatMode.ECO if preset_mode == PRESET_ECO else self._last_active_mode
        await self._run(self.coordinator.client.async_set_mode(self._device, target))
        self.coordinator.apply_optimistic(self._device.endpoint_id, mode=target)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set one setpoint, or both in heat/cool mode."""
        if (hvac_mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(hvac_mode)

        client = self.coordinator.client
        low, high = kwargs.get(ATTR_TARGET_TEMP_LOW), kwargs.get(ATTR_TARGET_TEMP_HIGH)
        if low is not None and high is not None:
            lower, upper = self._temperature(low), self._temperature(high)
            await self._run(client.async_set_temperature_range(self._device, lower, upper))
            self.coordinator.apply_optimistic(
                self._device.endpoint_id, lower_setpoint=lower, upper_setpoint=upper
            )
            return

        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        if self._alexa_mode == ThermostatMode.AUTO:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="single_setpoint_in_auto"
            )
        target = self._temperature(temperature)
        await self._run(client.async_set_target_temperature(self._device, target))
        self.coordinator.apply_optimistic(self._device.endpoint_id, target_setpoint=target)

    def _power_on_change(self) -> dict[str, bool]:
        state = self._state
        return {"power_on": True} if state and state.power_on is False else {}

    def _temperature(self, value: float) -> Temperature:
        """Round to the thermostat step; Alexa wants a number, never a string."""
        scale = self._scale
        if scale == TemperatureScale.FAHRENHEIT:
            return Temperature(float(round(value)), scale)
        return Temperature(round(value * 2) / 2, scale)

    def _value(self, reading: Temperature | None) -> float | None:
        if reading is None:
            return None
        return round(reading.to(self._scale).value, 1)

    async def _run(self, command: Awaitable[None]) -> None:
        """Run a command and turn API errors into Home Assistant errors."""
        try:
            await command
        except AlexaError as err:
            self._raise_ha_error(err)

    def _raise_ha_error(self, err: AlexaError) -> NoReturn:
        if isinstance(err, AlexaCommandError):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_rejected",
                translation_placeholders={"codes": ", ".join(err.codes) or "unknown"},
            ) from err
        if isinstance(err, AlexaAuthError):
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err
