"""Tests for the alexapy login wrapper."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

from alexapy.errors import AlexapyConnectionError, AlexapyLoginError, AlexapyPyotpInvalidKey
import pytest

from custom_components.alexa_thermostat.api import (
    CHALLENGE_CAPTCHA,
    CHALLENGE_SECURITY_CODE,
    CHALLENGE_UNSUPPORTED,
    AlexaAuth,
    AlexaAuthError,
    AlexaConnectionError,
    AlexaInvalidOtpSecret,
    AlexaLoginChallenge,
)
from custom_components.alexa_thermostat.api.auth import isolate_storage


@pytest.fixture
def alexa_login() -> Generator[MagicMock]:
    """Patch alexapy.AlexaLogin."""
    with patch("custom_components.alexa_thermostat.api.auth.AlexaLogin") as login_cls:
        login = login_cls.return_value
        login.login = AsyncMock()
        login.close = AsyncMock()
        login.status = {}
        login.uuid = "uuid-1"
        login.refresh_token = "Atnr|refresh"
        login.access_token = "Atna|access"
        login.mac_dms = {"adp_token": "x"}
        login.expires_in = 1234.0
        yield login_cls


def _auth(**kwargs) -> AlexaAuth:
    return AlexaAuth("amazon.com", "user@example.com", lambda path: f"/config/{path}", **kwargs)


def test_isolate_storage_rewrites_alexa_media_prefix() -> None:
    """Cookie files never collide with Alexa Media Player."""
    path = isolate_storage(lambda rel: f"/config/{rel}", "alexa_thermostat")

    assert path(".storage/alexa_media.user@example.com.cookies") == (
        "/config/.storage/alexa_thermostat.user@example.com.cookies"
    )
    assert path("other.file") == "/config/other.file"


def test_constructor_passes_tokens_and_uuid(alexa_login: MagicMock) -> None:
    """Stored tokens and uuid are handed to alexapy with OAuth login on."""
    _auth(tokens={"refresh_token": "Atnr|refresh"}, device_uuid="uuid-1")

    kwargs = alexa_login.call_args.kwargs
    assert kwargs["oauth"] == {"refresh_token": "Atnr|refresh"}
    assert kwargs["uuid"] == "uuid-1"
    assert kwargs["oauth_login"] is True
    assert kwargs["outputpath"](".storage/alexa_media.x.cookies") == (
        "/config/.storage/alexa_thermostat.x.cookies"
    )


def test_tokens_exclude_missing_values(alexa_login: MagicMock) -> None:
    """Only tokens alexapy actually has are persisted."""
    alexa_login.return_value.mac_dms = None
    assert _auth().tokens == {
        "access_token": "Atna|access",
        "refresh_token": "Atnr|refresh",
        "expires_in": 1234.0,
    }


def test_invalid_otp_secret(alexa_login: MagicMock) -> None:
    """A bad base32 key is reported as such."""
    alexa_login.side_effect = AlexapyPyotpInvalidKey("bad")
    with pytest.raises(AlexaInvalidOtpSecret):
        _auth(otp_secret="not base32")


@pytest.mark.parametrize(
    ("status", "challenge"),
    [
        ({"securitycode_required": True}, CHALLENGE_SECURITY_CODE),
        ({"captcha_required": True, "captcha_image_url": "https://x/c.jpg"}, CHALLENGE_CAPTCHA),
        ({"claimspicker_required": True}, CHALLENGE_UNSUPPORTED),
        ({"action_required": True}, CHALLENGE_UNSUPPORTED),
    ],
)
async def test_login_challenges(alexa_login: MagicMock, status: dict, challenge: str) -> None:
    """Amazon login steps become typed challenges."""
    alexa_login.return_value.status = status
    with pytest.raises(AlexaLoginChallenge) as err:
        await _auth(password="pw").async_login({"password": "pw"})
    assert err.value.challenge == challenge
    if challenge == CHALLENGE_CAPTCHA:
        assert err.value.captcha_url == "https://x/c.jpg"


async def test_login_success_and_failure(alexa_login: MagicMock) -> None:
    """login_successful passes, anything else is an auth error."""
    auth = _auth(password="pw")
    alexa_login.return_value.status = {"login_successful": True}
    await auth.async_login({"password": "pw"})

    alexa_login.return_value.status = {"login_failed": "login_failed"}
    with pytest.raises(AlexaAuthError):
        await auth.async_login({"password": "pw"})


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AlexapyConnectionError("down"), AlexaConnectionError),
        (AlexapyLoginError("nope"), AlexaAuthError),
    ],
)
async def test_login_maps_alexapy_errors(
    alexa_login: MagicMock, error: Exception, expected: type[Exception]
) -> None:
    """alexapy errors never leak out of the auth layer."""
    alexa_login.return_value.login.side_effect = error
    with pytest.raises(expected):
        await _auth(password="pw").async_login()
    with pytest.raises(expected):
        await _auth().async_resume()


async def test_resume_requires_successful_refresh(alexa_login: MagicMock) -> None:
    """Resume fails when there is no token or the refresh is rejected."""
    alexa_login.return_value.status = {"login_successful": True}
    await _auth().async_resume()

    alexa_login.return_value.status = {}
    with pytest.raises(AlexaAuthError):
        await _auth().async_resume()

    alexa_login.return_value.refresh_token = None
    with pytest.raises(AlexaAuthError, match="No refresh token"):
        await _auth().async_resume()
