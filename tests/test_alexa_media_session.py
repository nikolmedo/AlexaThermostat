"""Tests for reusing an Alexa Media Player session."""

from __future__ import annotations

from collections.abc import Generator
import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alexa_thermostat.alexa_media_session import (
    async_alexa_media_uuid,
    find_alexa_media_sessions,
)
from custom_components.alexa_thermostat.api import AlexaAuthError, AlexaConnectionError
from custom_components.alexa_thermostat.api.parser import parse_thermostats
from custom_components.alexa_thermostat.const import (
    CONF_AMAZON_DOMAIN,
    CONF_DEVICE_UUID,
    CONF_TOKENS,
    DOMAIN,
)

from .conftest import EMAIL, load_fixture

INSTANCE_ID = "0123456789abcdef0123456789abcdef"
AMP_OAUTH = {
    "access_token": "Atna|amp",
    "refresh_token": "Atnr|amp",
    "expires_in": 1234.0,
    "mac_dms": {"adp_token": "x"},
    "code_verifier": "not-copied",
    "authorization_code": "not-copied",
}


def _expected_uuid(email: str, url: str, index: int = 0) -> str:
    digest = hashlib.sha256((email.lower() + url.lower()).encode()).hexdigest()
    return hex(int(INSTANCE_ID, 16) + index + int(digest, 16))[-32:]


async def _alexa_media_calculate_uuid(hass, email: str, url: str) -> dict:
    """Verbatim logic of alexa_media.helpers.calculate_uuid (alexa_media 5.18.0).

    Source: https://github.com/alandtse/alexa_media_player (Apache-2.0),
    custom_components/alexa_media/helpers.py. Only the logger call is removed.
    """
    result = {}
    return_index = 0
    if hass.config_entries.async_entries("alexa_media"):
        for index, entry in enumerate(hass.config_entries.async_entries("alexa_media")):
            if entry.data.get(CONF_EMAIL) == email and entry.data.get("url") == url:
                return_index = index
                break
    uuid = INSTANCE_ID
    result["uuid"] = hex(
        int(uuid, 16)
        # increment uuid for second accounts
        + return_index
        # hash email/url in case HA uuid duplicated
        + int(
            hashlib.sha256((email.lower() + url.lower()).encode()).hexdigest(),
            16,  # nosec
        )
    )[-32:]
    result["index"] = return_index
    return result


def _amp_entry(email: str = EMAIL, url: str = "amazon.com", oauth: dict | None = None):
    return MockConfigEntry(
        domain="alexa_media",
        title=email,
        data={CONF_EMAIL: email, "url": url, "password": "x", "oauth": oauth or AMP_OAUTH},
    )


@pytest.fixture(autouse=True)
def instance() -> Generator[None]:
    """Pin the Home Assistant instance id."""
    with patch(
        "custom_components.alexa_thermostat.alexa_media_session.instance_id.async_get",
        AsyncMock(return_value=INSTANCE_ID),
    ):
        yield


@pytest.fixture
def flow_auth() -> Generator[MagicMock]:
    """Patch AlexaAuth inside the config flow."""
    with patch(
        "custom_components.alexa_thermostat.config_flow.AlexaAuth", autospec=True
    ) as auth_cls:
        auth = auth_cls.return_value
        auth.async_resume = AsyncMock()
        auth.async_close = AsyncMock()
        auth.tokens = {"refresh_token": "Atnr|amp", "access_token": "Atna|fresh"}
        auth.device_uuid = _expected_uuid(EMAIL, "amazon.com")
        yield auth_cls


@pytest.fixture
def flow_client() -> Generator[MagicMock]:
    """Patch the client inside the config flow."""
    with patch(
        "custom_components.alexa_thermostat.config_flow.AlexaThermostatClient", autospec=True
    ) as client_cls:
        client_cls.return_value.async_get_thermostats = AsyncMock(
            return_value=parse_thermostats(load_fixture("endpoints.json"))
        )
        yield client_cls.return_value


@pytest.fixture
def no_setup() -> Generator[None]:
    """Skip the real setup after the entry is created."""
    with patch("custom_components.alexa_thermostat.async_setup_entry", return_value=True):
        yield


async def test_uuid_matches_alexa_media_formula(hass: HomeAssistant) -> None:
    """The uuid is computed exactly like alexa_media.helpers.calculate_uuid."""
    _amp_entry("other@example.com").add_to_hass(hass)
    _amp_entry().add_to_hass(hass)

    sessions = find_alexa_media_sessions(hass)
    mine = next(session for session in sessions if session.email == EMAIL)

    assert await async_alexa_media_uuid(hass, mine) == _expected_uuid(EMAIL, "amazon.com", 1)


async def test_only_signed_in_accounts_are_offered(hass: HomeAssistant) -> None:
    """Accounts without a refresh token are ignored; extra oauth keys are not copied."""
    _amp_entry("old@example.com", oauth={"access_token": "only"}).add_to_hass(hass)
    _amp_entry().add_to_hass(hass)

    sessions = find_alexa_media_sessions(hass)

    assert [session.email for session in sessions] == [EMAIL]
    assert sessions[0].tokens == {
        "access_token": "Atna|amp",
        "refresh_token": "Atnr|amp",
        "mac_dms": {"adp_token": "x"},
        "expires_in": 1234.0,
    }


async def test_no_menu_without_alexa_media(hass: HomeAssistant) -> None:
    """Without Alexa Media Player the credentials form is shown directly."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_import_alexa_media_session(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock, no_setup: None
) -> None:
    """The Alexa Media Player session creates the entry without a password."""
    _amp_entry().add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["alexa_media", "credentials"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "alexa_media"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    kwargs = flow_auth.call_args.kwargs
    assert kwargs["tokens"]["refresh_token"] == "Atnr|amp"
    assert kwargs["device_uuid"] == _expected_uuid(EMAIL, "amazon.com")
    flow_auth.return_value.async_resume.assert_awaited_once()
    assert result["data"] == {
        CONF_EMAIL: EMAIL,
        CONF_AMAZON_DOMAIN: "amazon.com",
        CONF_TOKENS: flow_auth.return_value.tokens,
        CONF_DEVICE_UUID: _expected_uuid(EMAIL, "amazon.com"),
    }
    assert result["result"].unique_id == f"amazon.com:{EMAIL}"


async def test_choose_between_alexa_media_accounts(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock, no_setup: None
) -> None:
    """With several accounts the user picks one."""
    other = _amp_entry("other@example.com")
    other.add_to_hass(hass)
    mine = _amp_entry()
    mine.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "alexa_media"}
    )
    assert result["step_id"] == "alexa_media"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"account": mine.entry_id}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_EMAIL] == EMAIL
    assert flow_auth.call_args.kwargs["device_uuid"] == _expected_uuid(EMAIL, "amazon.com", 1)


async def test_expired_alexa_media_session_aborts(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock
) -> None:
    """A refresh token Amazon rejects is reported and the session closed."""
    _amp_entry().add_to_hass(hass)
    flow_auth.return_value.async_resume.side_effect = AlexaAuthError("expired")

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "alexa_media"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "alexa_media_session_invalid"
    flow_auth.return_value.async_close.assert_awaited()


async def test_credentials_option_still_works(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock, no_setup: None
) -> None:
    """Choosing email and password shows the usual form."""
    _amp_entry().add_to_hass(hass)
    flow_auth.return_value.async_login = AsyncMock()

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )
    assert result["step_id"] == "credentials"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_EMAIL: EMAIL, "password": "secret", CONF_AMAZON_DOMAIN: "amazon.com"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_reauth_with_alexa_media_session(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    flow_auth: MagicMock,
    flow_client: MagicMock,
    no_setup: None,
) -> None:
    """Reauth can reuse Alexa Media Player's session instead of the password."""
    _amp_entry().add_to_hass(hass)
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["alexa_media", "reauth_confirm"]
    assert result["description_placeholders"]["email"] == EMAIL

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "alexa_media"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_TOKENS] == flow_auth.return_value.tokens


@pytest.mark.parametrize(
    "accounts",
    [
        [(EMAIL, "amazon.com")],
        [("other@example.com", "amazon.com"), (EMAIL, "amazon.com")],
        [(EMAIL, "amazon.de"), ("Mixed.Case@Example.com", "Amazon.COM")],
    ],
)
async def test_uuid_parity_with_alexa_media_code(
    hass: HomeAssistant, accounts: list[tuple[str, str]]
) -> None:
    """Every account gets the uuid Alexa Media Player's own code computes."""
    for email, url in accounts:
        _amp_entry(email, url).add_to_hass(hass)

    for session in find_alexa_media_sessions(hass):
        expected = await _alexa_media_calculate_uuid(hass, session.email, session.url)
        assert await async_alexa_media_uuid(hass, session) == expected["uuid"]


async def test_malformed_alexa_media_entries_are_ignored(hass: HomeAssistant) -> None:
    """A broken alexa_media entry never blocks the password sign-in."""
    MockConfigEntry(
        domain="alexa_media", data={CONF_EMAIL: EMAIL, "url": "amazon.com", "oauth": "not-a-dict"}
    ).add_to_hass(hass)
    MockConfigEntry(domain="alexa_media", data={"oauth": AMP_OAUTH}).add_to_hass(hass)

    assert find_alexa_media_sessions(hass) == []
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    assert result["type"] is FlowResultType.FORM


async def _choose_alexa_media(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "alexa_media"}
    )


async def test_connection_error_has_its_own_reason(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock
) -> None:
    """A refresh that never reaches Amazon is not reported as a device listing error."""
    _amp_entry().add_to_hass(hass)
    flow_auth.return_value.async_resume.side_effect = AlexaConnectionError("throttled")

    result = await _choose_alexa_media(hass)

    assert result["reason"] == "alexa_media_cannot_connect"
    flow_auth.return_value.async_close.assert_awaited()


async def test_non_amazon_url_is_rejected(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock
) -> None:
    """A stored url that is not an Amazon domain is never used as a host."""
    _amp_entry(url="evil.example.com").add_to_hass(hass)

    result = await _choose_alexa_media(hass)

    assert result["reason"] == "alexa_media_session_invalid"
    flow_auth.assert_not_called()


async def test_already_configured_through_alexa_media(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    flow_auth: MagicMock,
    flow_client: MagicMock,
) -> None:
    """The same account cannot be added twice through the import either."""
    mock_config_entry.add_to_hass(hass)
    _amp_entry().add_to_hass(hass)

    result = await _choose_alexa_media(hass)

    assert result["reason"] == "already_configured"
    flow_auth.assert_not_called()


async def test_chosen_account_removed_before_submit(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock
) -> None:
    """An account that signs out while the form is open aborts cleanly."""
    _amp_entry("first@example.com").add_to_hass(hass)
    _amp_entry("second@example.com").add_to_hass(hass)
    mine = _amp_entry()
    mine.add_to_hass(hass)
    result = await _choose_alexa_media(hass)
    assert result["step_id"] == "alexa_media"

    hass.config_entries.async_update_entry(mine, data={**mine.data, "oauth": {}})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"account": mine.entry_id}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "alexa_media_not_found"
    flow_auth.assert_not_called()


async def test_chosen_account_gone_never_imports_another(
    hass: HomeAssistant, flow_auth: MagicMock, flow_client: MagicMock
) -> None:
    """With one other account left, the submitted choice still wins: abort, not import."""
    other = _amp_entry("other@example.com")
    other.add_to_hass(hass)
    mine = _amp_entry()
    mine.add_to_hass(hass)
    result = await _choose_alexa_media(hass)
    assert result["step_id"] == "alexa_media"

    hass.config_entries.async_update_entry(mine, data={**mine.data, "oauth": {}})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"account": mine.entry_id}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "alexa_media_not_found"
    flow_auth.assert_not_called()


async def test_reauth_falls_back_to_password_form(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    flow_auth: MagicMock,
    flow_client: MagicMock,
) -> None:
    """If Alexa Media Player's session fails during reauth, the password form is shown."""
    _amp_entry().add_to_hass(hass)
    mock_config_entry.add_to_hass(hass)
    flow_auth.return_value.async_resume.side_effect = AlexaAuthError("expired")

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "alexa_media"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "alexa_media_session_invalid"}


async def test_reauth_connection_error_falls_back_to_password_form(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    flow_auth: MagicMock,
    flow_client: MagicMock,
) -> None:
    """A throttled refresh during reauth also offers the password form."""
    _amp_entry().add_to_hass(hass)
    mock_config_entry.add_to_hass(hass)
    flow_auth.return_value.async_resume.side_effect = AlexaConnectionError("throttled")

    result = await mock_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "alexa_media"}
    )

    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "alexa_media_cannot_connect"}
    assert result["description_placeholders"]["email"] == EMAIL
