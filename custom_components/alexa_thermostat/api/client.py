"""GraphQL client for Alexa smart home thermostats."""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from aiohttp import ClientError, ClientSession, ClientTimeout

from .exceptions import AlexaAuthError, AlexaConnectionError
from .models import Operation, Temperature, ThermostatDevice, ThermostatMode, ThermostatState
from .parser import parse_thermostat_state, parse_thermostats, raise_for_command_errors
from .queries import (
    ENDPOINTS_QUERY,
    GRAPHQL_PATH,
    SET_ENDPOINT_FEATURES_MUTATION,
    THERMOSTAT_STATE_QUERY,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = ClientTimeout(total=20)
THERMOSTAT_FEATURE = "thermostat"
POWER_FEATURE = "power"


class AuthProvider(Protocol):
    """What the client needs from the login layer (see auth.AlexaAuth)."""

    @property
    def domain(self) -> str: ...

    @property
    def session(self) -> ClientSession: ...

    @property
    def csrf(self) -> str | None: ...

    @property
    def user_agent(self) -> str | None: ...

    async def async_resume(self) -> None: ...


def setpoint_payload(temperature: Temperature) -> dict[str, Any]:
    """Build a setpoint payload.

    The value MUST be a JSON number. Amazon answers BAD_REQUEST when it is a
    string (homebridge-alexa-smarthome issue #284, fixed by PR #285).
    """
    value = round(float(temperature.value), 1)
    return {
        "value": int(value) if value.is_integer() else value,
        "scale": temperature.scale.value,
    }


class AlexaThermostatClient:
    """Discover, read and control thermostats linked to an Alexa account."""

    def __init__(self, auth: AuthProvider) -> None:
        """Wrap an authenticated login."""
        self._auth = auth

    async def async_get_thermostats(self) -> list[ThermostatDevice]:
        """Return every enabled thermostat on the account."""
        return parse_thermostats(await self._graphql(ENDPOINTS_QUERY))

    async def async_get_state(self, endpoint_id: str) -> ThermostatState:
        """Return the current state of one thermostat."""
        response = await self._graphql(THERMOSTAT_STATE_QUERY, {"endpointId": endpoint_id})
        return parse_thermostat_state(response)

    async def async_set_target_temperature(
        self, device: ThermostatDevice, temperature: Temperature
    ) -> None:
        """Set the single target setpoint (HEAT or COOL mode)."""
        await self._command(
            device,
            THERMOSTAT_FEATURE,
            Operation.SET_TARGET_SETPOINT,
            {"targetSetpoint": setpoint_payload(temperature)},
        )

    async def async_set_temperature_range(
        self, device: ThermostatDevice, lower: Temperature, upper: Temperature
    ) -> None:
        """Set both setpoints at once (AUTO mode)."""
        await self._command(
            device,
            THERMOSTAT_FEATURE,
            Operation.SET_TARGET_SETPOINT,
            {
                "lowerSetpoint": setpoint_payload(lower),
                "upperSetpoint": setpoint_payload(upper),
            },
        )

    async def async_set_mode(self, device: ThermostatDevice, mode: ThermostatMode) -> None:
        """Change the thermostat mode."""
        await self._command(
            device,
            THERMOSTAT_FEATURE,
            Operation.SET_THERMOSTAT_MODE,
            {"thermostatMode": mode.value},
        )

    async def async_turn_on(self, device: ThermostatDevice) -> None:
        """Turn the thermostat on through the power feature."""
        await self._command(device, POWER_FEATURE, Operation.TURN_ON)

    async def async_turn_off(self, device: ThermostatDevice) -> None:
        """Turn the thermostat off through the power feature."""
        await self._command(device, POWER_FEATURE, Operation.TURN_OFF)

    async def _command(
        self,
        device: ThermostatDevice,
        feature: str,
        operation: Operation,
        payload: dict[str, Any] | None = None,
    ) -> None:
        request: dict[str, Any] = {
            "endpointId": device.endpoint_id,
            "featureName": feature,
            "featureOperationName": operation.value,
        }
        if payload:
            request["payload"] = payload
        response = await self._graphql(
            SET_ENDPOINT_FEATURES_MUTATION, {"featureControlRequests": [request]}
        )
        raise_for_command_errors(response)

    async def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {"query": query, "variables": variables or {}}
        status, data = await self._post(body)
        if status == 401:
            _LOGGER.debug("Alexa answered 401, refreshing the session once")
            await self._auth.async_resume()
            status, data = await self._post(body)
            if status == 401:
                raise AlexaAuthError("Alexa rejected the refreshed session")
        if status == 429:
            raise AlexaConnectionError("Alexa rate limit reached (HTTP 429)")
        if status >= 400:
            raise AlexaConnectionError(f"Alexa answered HTTP {status}")
        if not isinstance(data, dict):
            raise AlexaConnectionError("Alexa answered with an unexpected body")
        return data

    async def _post(self, body: dict[str, Any]) -> tuple[int, Any]:
        url = f"https://alexa.{self._auth.domain}{GRAPHQL_PATH}"
        try:
            async with self._auth.session.post(
                url, json=body, headers=self._headers(), timeout=REQUEST_TIMEOUT
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    return response.status, None
        except (ClientError, TimeoutError) as err:
            raise AlexaConnectionError(f"Error talking to Alexa: {err}") from err
        try:
            return response.status, json.loads(text)
        except ValueError as err:
            raise AlexaConnectionError("Alexa answered with invalid JSON") from err

    def _headers(self) -> dict[str, str]:
        domain = self._auth.domain
        headers = {
            "Accept": "application/json; charset=utf-8",
            "Content-Type": "application/json; charset=utf-8",
            "Origin": f"https://alexa.{domain}",
            "Referer": f"https://alexa.{domain}/spa/index.html",
        }
        if csrf := self._auth.csrf:
            headers["csrf"] = csrf
        if user_agent := self._auth.user_agent:
            headers["User-Agent"] = user_agent
        return headers
