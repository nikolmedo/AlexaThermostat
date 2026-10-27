"""Tests for the GraphQL client, including the #284 setpoint regression."""

from __future__ import annotations

from collections.abc import Iterator
import json
from typing import Any

from aiohttp import ClientError
import pytest

from custom_components.alexa_thermostat.api import (
    AlexaAuthError,
    AlexaCommandError,
    AlexaConnectionError,
    AlexaThermostatClient,
    Temperature,
    TemperatureScale,
    ThermostatDevice,
    ThermostatMode,
)
from custom_components.alexa_thermostat.api.parser import parse_thermostats

from .conftest import ENDPOINT_ID, load_fixture

GRAPHQL_URL = "https://alexa.amazon.com/nexus/v1/graphql"


class FakeResponse:
    """Minimal aiohttp response."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSession:
    """Records POSTs and replays queued responses (or exceptions)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[FakeResponse | BaseException] = []

    def queue(self, payload: Any = None, *, status: int = 200, body: str | None = None) -> None:
        self._queue.append(FakeResponse(status, body if body is not None else json.dumps(payload)))

    def queue_error(self, error: BaseException) -> None:
        self._queue.append(error)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        item = self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]
        if isinstance(item, BaseException):
            raise item
        return item


class FakeAuth:
    """Stand-in for AlexaAuth."""

    def __init__(self) -> None:
        self.domain = "amazon.com"
        self.session = FakeSession()
        self.csrf = "csrf-token"
        self.user_agent = "TestAgent/1.0"
        self.resume_calls = 0

    async def async_resume(self) -> None:
        self.resume_calls += 1


@pytest.fixture
def auth() -> Iterator[FakeAuth]:
    """Return a fake auth."""
    return FakeAuth()


@pytest.fixture
def client(auth: FakeAuth) -> AlexaThermostatClient:
    """Return a client wired to the fake auth."""
    return AlexaThermostatClient(auth)


@pytest.fixture
def device() -> ThermostatDevice:
    """Return the thermostat from the endpoints fixture."""
    return parse_thermostats(load_fixture("endpoints.json"))[0]


def _last_request(auth: FakeAuth) -> dict[str, Any]:
    return auth.session.calls[-1]["json"]["variables"]["featureControlRequests"][0]


async def test_set_target_sends_numeric_value(
    auth: FakeAuth, client: AlexaThermostatClient, device: ThermostatDevice
) -> None:
    """Regression for homebridge-alexa-smarthome #284: the value is a number."""
    auth.session.queue(load_fixture("set_success.json"))
    await client.async_set_target_temperature(
        device, Temperature(73.0, TemperatureScale.FAHRENHEIT)
    )

    request = _last_request(auth)
    assert request == {
        "endpointId": ENDPOINT_ID,
        "featureName": "thermostat",
        "featureOperationName": "setTargetSetpoint",
        "payload": {"targetSetpoint": {"value": 73, "scale": "FAHRENHEIT"}},
    }
    value = request["payload"]["targetSetpoint"]["value"]
    assert isinstance(value, int | float)
    assert '"value": 73,' in json.dumps(request)


async def test_set_range_sends_both_numeric_setpoints(
    auth: FakeAuth, client: AlexaThermostatClient, device: ThermostatDevice
) -> None:
    """AUTO mode sends lower and upper setpoints together, as numbers."""
    auth.session.queue(load_fixture("set_success.json"))
    await client.async_set_temperature_range(
        device,
        Temperature(20.5, TemperatureScale.CELSIUS),
        Temperature(24.0, TemperatureScale.CELSIUS),
    )

    payload = _last_request(auth)["payload"]
    assert payload == {
        "lowerSetpoint": {"value": 20.5, "scale": "CELSIUS"},
        "upperSetpoint": {"value": 24, "scale": "CELSIUS"},
    }
    assert all(not isinstance(item["value"], str) for item in payload.values())


async def test_set_mode_and_power_payloads(
    auth: FakeAuth, client: AlexaThermostatClient, device: ThermostatDevice
) -> None:
    """Mode goes through the thermostat feature, on/off through power."""
    auth.session.queue(load_fixture("set_success.json"))
    await client.async_set_mode(device, ThermostatMode.COOL)
    mode_request = _last_request(auth)
    await client.async_turn_off(device)
    off_request = _last_request(auth)

    assert mode_request["featureName"] == "thermostat"
    assert mode_request["featureOperationName"] == "setThermostatMode"
    assert mode_request["payload"] == {"thermostatMode": "COOL"}
    assert off_request == {
        "endpointId": ENDPOINT_ID,
        "featureName": "power",
        "featureOperationName": "turnOff",
    }


async def test_rejected_command_raises(
    auth: FakeAuth, client: AlexaThermostatClient, device: ThermostatDevice
) -> None:
    """A BAD_REQUEST in the errors array is not treated as success."""
    auth.session.queue(load_fixture("set_bad_request.json"))
    with pytest.raises(AlexaCommandError) as err:
        await client.async_set_target_temperature(
            device, Temperature(73, TemperatureScale.FAHRENHEIT)
        )
    assert err.value.codes == ["BAD_REQUEST"]


async def test_request_shape(auth: FakeAuth, client: AlexaThermostatClient) -> None:
    """Discovery posts the Endpoints query with csrf and user agent headers."""
    auth.session.queue(load_fixture("endpoints.json"))
    devices = await client.async_get_thermostats()

    call = auth.session.calls[0]
    assert len(devices) == 1
    assert call["url"] == GRAPHQL_URL
    assert call["headers"]["csrf"] == "csrf-token"
    assert call["headers"]["User-Agent"] == "TestAgent/1.0"
    assert call["headers"]["Referer"] == "https://alexa.amazon.com/spa/index.html"
    assert "query Endpoints" in call["json"]["query"]


async def test_get_state_sends_endpoint_variable(
    auth: FakeAuth, client: AlexaThermostatClient
) -> None:
    """The state query is scoped to one endpoint."""
    auth.session.queue(load_fixture("state_heat.json"))
    state = await client.async_get_state(ENDPOINT_ID)

    assert auth.session.calls[0]["json"]["variables"] == {"endpointId": ENDPOINT_ID}
    assert state.mode is ThermostatMode.HEAT


async def test_401_refreshes_session_once(auth: FakeAuth, client: AlexaThermostatClient) -> None:
    """A 401 triggers one session refresh and a retry."""
    auth.session.queue(status=401, body="")
    auth.session.queue(load_fixture("endpoints.json"))
    devices = await client.async_get_thermostats()

    assert auth.resume_calls == 1
    assert len(auth.session.calls) == 2
    assert len(devices) == 1


async def test_401_after_refresh_raises_auth_error(
    auth: FakeAuth, client: AlexaThermostatClient
) -> None:
    """A second 401 means the refresh token is no longer valid."""
    auth.session.queue(status=401, body="")
    with pytest.raises(AlexaAuthError):
        await client.async_get_thermostats()
    assert auth.resume_calls == 1


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_http_errors_raise_connection_error(
    auth: FakeAuth, client: AlexaThermostatClient, status: int
) -> None:
    """HTTP errors become connection errors without a session refresh."""
    auth.session.queue(status=status, body="")
    with pytest.raises(AlexaConnectionError):
        await client.async_get_thermostats()
    assert auth.resume_calls == 0


async def test_invalid_json_raises_connection_error(
    auth: FakeAuth, client: AlexaThermostatClient
) -> None:
    """A non JSON body (for example a login page) is an error."""
    auth.session.queue(body="<html>Sign in</html>")
    with pytest.raises(AlexaConnectionError):
        await client.async_get_thermostats()


@pytest.mark.parametrize("error", [TimeoutError(), ClientError("reset")])
async def test_network_errors_raise_connection_error(
    auth: FakeAuth, client: AlexaThermostatClient, error: BaseException
) -> None:
    """Network failures become connection errors."""
    auth.session.queue_error(error)
    with pytest.raises(AlexaConnectionError):
        await client.async_get_thermostats()
