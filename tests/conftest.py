"""Shared fixtures for Alexa Thermostat tests."""

from __future__ import annotations

from collections.abc import Generator
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alexa_thermostat.api.parser import (
    parse_thermostat_state,
    parse_thermostats,
)
from custom_components.alexa_thermostat.const import (
    CONF_AMAZON_DOMAIN,
    CONF_DEVICE_UUID,
    CONF_TOKENS,
    DOMAIN,
)

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "user@example.com"
ENDPOINT_ID = "amzn1.alexa.endpoint.thermostat-1"
TOKENS = {"refresh_token": "Atnr|refresh", "access_token": "Atna|access", "mac_dms": {}}


def load_fixture(name: str) -> dict[str, Any]:
    """Load a JSON fixture."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    return


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a configured entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=f"amazon.com:{EMAIL}",
        data={
            "email": EMAIL,
            CONF_AMAZON_DOMAIN: "amazon.com",
            CONF_TOKENS: TOKENS,
            CONF_DEVICE_UUID: "0123456789abcdef0123456789abcdef",
        },
    )


@pytest.fixture
def mock_auth() -> Generator[MagicMock]:
    """Patch the login layer used by async_setup_entry."""
    with patch("custom_components.alexa_thermostat.AlexaAuth", autospec=True) as auth_cls:
        auth = auth_cls.return_value
        auth.tokens = TOKENS
        auth.async_resume = AsyncMock()
        auth.async_close = AsyncMock()
        yield auth


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Patch the GraphQL client used by async_setup_entry."""
    with patch(
        "custom_components.alexa_thermostat.AlexaThermostatClient", autospec=True
    ) as client_cls:
        client = client_cls.return_value
        client.async_get_thermostats = AsyncMock(
            return_value=parse_thermostats(load_fixture("endpoints.json"))
        )
        client.async_get_state = AsyncMock(
            return_value=parse_thermostat_state(load_fixture("state_heat.json"))
        )
        for name in (
            "async_set_target_temperature",
            "async_set_temperature_range",
            "async_set_mode",
            "async_turn_on",
            "async_turn_off",
        ):
            setattr(client, name, AsyncMock())
        yield client
