"""Tests for the alexapy login wrapper."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientError
from alexapy.errors import AlexapyConnectionError, AlexapyLoginError, AlexapyPyotpInvalidKey
import httpx
import pytest
from yarl import URL

from custom_components.alexa_thermostat.api import (
    CHALLENGE_CAPTCHA,
    CHALLENGE_SECURITY_CODE,
    CHALLENGE_UNSUPPORTED,
    AlexaAuth,
    AlexaAuthError,
    AlexaConnectionError,
    AlexaInvalidOtpSecret,
    AlexaLoginChallenge,
    AlexaSignInProxy,
)
from custom_components.alexa_thermostat.api.auth import (
    is_one_time_code,
    isolate_storage,
    normalize_otp_input,
)


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abcd efgh ijkl mnop", "ABCDEFGHIJKLMNOP"),
        ("ABCD\u00a0EFGH\u00a0IJKL", "ABCDEFGHIJKL"),
        (" ABCD\u200bEFGH\nIJKL\t", "ABCDEFGHIJKL"),
        ("abcd-efgh-ijkl", "ABCDEFGHIJKL"),
        # Unexpected characters are kept so validation can reject the key.
        ("abcdéfgh", "ABCDÉFGH"),
        ("", ""),
    ],
)
def test_normalize_otp_input(raw: str, expected: str) -> None:
    """Keys pasted from Amazon (often on a phone) are cleaned before use."""
    assert normalize_otp_input(raw) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("123456", True), ("12345678", True), ("12345", False), ("ABCDEFGH", False)],
)
def test_is_one_time_code(value: str, expected: bool) -> None:
    """A 6-8 digit value is a one-time code, not an authenticator key."""
    assert is_one_time_code(value) is expected


async def test_login_forwards_security_code(alexa_login: MagicMock) -> None:
    """The one-time code reaches alexapy, which fills Amazon's OTP field with it."""
    alexa_login.return_value.status = {"login_successful": True}
    await _auth(password="pw").async_login({"password": "pw", "securitycode": "123456"})

    alexa_login.return_value.login.assert_awaited_once_with(
        data={"password": "pw", "securitycode": "123456"}
    )


def test_key_with_unexpected_characters_is_rejected() -> None:
    """A look-alike character reaches pyotp and fails instead of being dropped."""
    with pytest.raises(AlexaInvalidOtpSecret):
        AlexaAuth(
            "amazon.com",
            "user@example.com",
            lambda path: path,
            otp_secret=normalize_otp_input("ABCD EFGH ÉJKL MNOP"),
        )


def test_full_width_digits_are_not_a_one_time_code() -> None:
    """Digits typed with a phone IME in full width are not treated as a code."""
    full_width_123456 = "".join(chr(0xFF11 + offset) for offset in range(6))
    assert is_one_time_code(full_width_123456) is False


@pytest.mark.parametrize("key", ["ABCDEFGH", "234567234"])
def test_too_short_key_is_rejected(key: str) -> None:
    """A partial paste is reported instead of generating wrong codes."""
    with pytest.raises(AlexaInvalidOtpSecret):
        AlexaAuth("amazon.com", "user@example.com", lambda path: path, otp_secret=key)


PROXY_BASE = "http://ha.local:8123/auth/alexa_thermostat/proxy/abc"
PROXY_START = "/auth/alexa_thermostat/proxy/abc?config_flow_id=abc"
PROXY_RETURN = "/auth/alexa_thermostat/callback?flow_id=abc"


@pytest.fixture
def proxy_login(alexa_login: MagicMock) -> MagicMock:
    """Give the patched login what alexapy's proxy reads from it."""
    login = alexa_login.return_value
    login.start_url = URL("https://www.amazon.com/ap/signin")
    login.email = "user@example.com"
    login.password = "pw"
    login.get_totp_token = MagicMock(return_value="123456")
    login.session = MagicMock()
    login._debug = False
    return login


async def _proxy(clients: list[httpx.AsyncClient]) -> AlexaSignInProxy:
    def factory() -> httpx.AsyncClient:
        clients.append(httpx.AsyncClient())
        return clients[-1]

    return await _auth(password="pw").async_create_proxy(
        PROXY_BASE, start_url=PROXY_START, return_url=PROXY_RETURN, session_factory=factory
    )


def _response(url: str) -> MagicMock:
    return MagicMock(url=url)


async def test_proxy_success_redirects_to_our_callback(proxy_login: MagicMock) -> None:
    """Amazon's landing page ends the proxy with a redirect to our own callback only."""
    clients: list[httpx.AsyncClient] = []
    proxy = await _proxy(clients)
    try:
        assert proxy_login.proxy_url == URL(PROXY_START)
        # A callback_url smuggled into the start query must never be followed.
        proxy._proxy.init_query = {"callback_url": "https://evil.example/steal"}

        result = await proxy.async_check_response(
            _response("https://www.amazon.com/ap/maplanding?openid.oa2.authorization_code=CODE"),
            {},
            {},
        )

        assert result == URL(PROXY_RETURN)
        assert proxy_login.authorization_code == "CODE"
        assert proxy.signed_in
    finally:
        for client in clients:
            await client.aclose()


async def test_proxy_other_pages_keep_going(proxy_login: MagicMock) -> None:
    """Sign-in pages pass through; a bare "/" goes back to the start of the proxy."""
    clients: list[httpx.AsyncClient] = []
    proxy = await _proxy(clients)
    try:
        signin = await proxy.async_check_response(
            _response("https://www.amazon.com/ap/signin"), {}, {}
        )
        home = await proxy.async_check_response(_response("https://www.amazon.com/"), {}, {})

        assert signin is None
        assert home == URL(PROXY_START)
        assert not proxy.signed_in
    finally:
        for client in clients:
            await client.aclose()


async def test_proxy_handle_refreshes_the_2fa_code(proxy_login: MagicMock) -> None:
    """Each browser request gets a fresh code, since codes expire every 30 seconds."""
    clients: list[httpx.AsyncClient] = []
    proxy = await _proxy(clients)
    try:
        proxy._proxy.all_handler = AsyncMock(return_value="page")
        proxy_login.get_totp_token.reset_mock()
        request = MagicMock()

        assert await proxy.async_handle(request) == "page"

        proxy._proxy.all_handler.assert_awaited_once_with(request)
        proxy_login.get_totp_token.assert_called_once()
        await proxy.async_close()
        assert clients[-1].is_closed
    finally:
        for client in clients:
            await client.aclose()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (False, AlexaAuthError),
        (AlexapyLoginError("nope"), AlexaAuthError),
        (AlexapyConnectionError("down"), AlexaConnectionError),
        (ClientError("down"), AlexaConnectionError),
    ],
)
async def test_finish_proxy_login_errors(
    alexa_login: MagicMock, outcome: object, expected: type[Exception]
) -> None:
    """Amazon not confirming the sign-in never passes silently."""
    login = alexa_login.return_value
    if isinstance(outcome, Exception):
        login.test_loggedin = AsyncMock(side_effect=outcome)
    else:
        login.test_loggedin = AsyncMock(return_value=outcome)
    with pytest.raises(expected):
        await _auth().async_finish_proxy_login()


async def test_finish_proxy_login_success(alexa_login: MagicMock) -> None:
    """A confirmed sign-in exchanges the captured code for tokens."""
    login = alexa_login.return_value
    login.test_loggedin = AsyncMock(return_value=True)
    await _auth().async_finish_proxy_login()
    login.test_loggedin.assert_awaited_once_with()
