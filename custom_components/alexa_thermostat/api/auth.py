"""Amazon login, isolated behind a small interface.

Amazon changes its login pages often. Everything that touches alexapy lives
in this module so a broken login can be fixed (or the library swapped) without
touching the GraphQL client or Home Assistant code.

Only the OAuth refresh token and the device uuid are persisted. The password
is used once during the config flow and never stored.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
import re
from typing import Any
import unicodedata

from aiohttp import ClientSession
from alexapy import AlexaLogin
from alexapy.errors import (
    AlexapyConnectionError,
    AlexapyLoginError,
    AlexapyPyotpInvalidKey,
)
from yarl import URL

from .exceptions import (
    AlexaAuthError,
    AlexaConnectionError,
    AlexaInvalidOtpSecret,
    AlexaLoginChallenge,
)

_LOGGER = logging.getLogger(__name__)

ALEXAPY_FILE_PREFIX = "alexa_media."
TOKEN_KEYS = ("access_token", "refresh_token", "mac_dms", "expires_in")

CHALLENGE_SECURITY_CODE = "securitycode"
CHALLENGE_CAPTCHA = "captcha"
CHALLENGE_UNSUPPORTED = "unsupported"

ONE_TIME_CODE = re.compile(r"\d{6,8}", re.ASCII)
# Amazon's authenticator keys are 52 characters; anything under 16 is a partial paste.
BASE32_KEY = re.compile(r"[A-Z2-7]{16,}=*")


def normalize_otp_input(value: str | None) -> str:
    """Clean an authenticator key or code pasted by the user.

    Amazon shows the key in groups of four. Copying it (especially on a phone)
    can bring non-breaking spaces, zero-width characters, newlines or dashes,
    which pyotp rejects as invalid base32. Only those separators are removed:
    any other unexpected character is kept so the key fails validation with a
    clear error instead of silently producing wrong codes.
    """
    if not value:
        return ""
    return "".join(
        char
        for char in value
        if not char.isspace() and char not in "-_" and unicodedata.category(char) != "Cf"
    ).upper()


def is_one_time_code(value: str) -> bool:
    """Return True for a 6-8 digit code (as opposed to an authenticator key)."""
    return bool(ONE_TIME_CODE.fullmatch(value))


def isolate_storage(outputpath: Callable[[str], str], prefix: str) -> Callable[[str], str]:
    """Return an outputpath that keeps alexapy files apart from Alexa Media Player.

    alexapy hardcodes the "alexa_media." file prefix for its cookie files, so
    two integrations logged in with the same email would overwrite each
    other's cookies without this rewrite.
    """

    def _path(relative: str) -> str:
        return outputpath(relative.replace(ALEXAPY_FILE_PREFIX, f"{prefix}.", 1))

    return _path


class AlexaAuth:
    """Owns the alexapy login and exposes the authenticated session."""

    def __init__(
        self,
        domain: str,
        email: str,
        outputpath: Callable[[str], str],
        *,
        password: str = "",
        otp_secret: str = "",
        tokens: dict[str, Any] | None = None,
        device_uuid: str | None = None,
        storage_prefix: str = "alexa_thermostat",
    ) -> None:
        """Create the alexapy login object without doing any I/O."""
        self._domain = domain
        # pyotp raises ValueError (which alexapy does not catch) for non-ASCII
        # keys, so check the base32 alphabet before handing the key over.
        if otp_secret and not BASE32_KEY.fullmatch(otp_secret.upper()):
            raise AlexaInvalidOtpSecret("The 2FA app key is not valid base32")
        try:
            self._login = AlexaLogin(
                url=domain,
                email=email,
                password=password,
                outputpath=isolate_storage(outputpath, storage_prefix),
                otp_secret=otp_secret,
                oauth=dict(tokens or {}),
                uuid=device_uuid,
                oauth_login=True,
            )
        except AlexapyPyotpInvalidKey as err:
            raise AlexaInvalidOtpSecret(str(err)) from err

    @property
    def domain(self) -> str:
        """Return the Amazon domain, for example amazon.com."""
        return self._domain

    @property
    def device_uuid(self) -> str:
        """Return the device uuid the refresh token is bound to."""
        return self._login.uuid

    @property
    def tokens(self) -> dict[str, Any]:
        """Return the OAuth tokens that must be persisted between restarts."""
        return {
            key: getattr(self._login, key)
            for key in TOKEN_KEYS
            if getattr(self._login, key, None) is not None
        }

    @property
    def session(self) -> ClientSession:
        """Return the authenticated aiohttp session (cookies included)."""
        session = self._login.session
        if session is None:
            raise AlexaAuthError("Not logged in")
        return session

    @property
    def csrf(self) -> str | None:
        """Return the csrf cookie value Alexa expects as a header."""
        cookies = self.session.cookie_jar.filter_cookies(URL(f"https://alexa.{self._domain}"))
        morsel = cookies.get("csrf")
        return morsel.value if morsel else None

    @property
    def user_agent(self) -> str | None:
        """Return the user agent alexapy logged in with."""
        headers: dict[str, str] = getattr(self._login, "_headers", None) or {}
        return headers.get("User-Agent")

    async def async_login(self, data: dict[str, str] | None = None) -> None:
        """Log in with the password (config flow) or answer a challenge.

        Raises AlexaLoginChallenge when Amazon asks for a code or captcha;
        call again with {"securitycode": ...} or {"captcha": ...}.
        """
        try:
            await self._login.login(data=dict(data or {}))
        except AlexapyPyotpInvalidKey as err:
            raise AlexaInvalidOtpSecret(str(err)) from err
        except AlexapyConnectionError as err:
            raise AlexaConnectionError(str(err)) from err
        except AlexapyLoginError as err:
            raise AlexaAuthError(str(err)) from err
        self._raise_for_status()

    async def async_resume(self) -> None:
        """Restore the session from the stored refresh token."""
        if not self._login.refresh_token:
            raise AlexaAuthError("No refresh token stored")
        try:
            await self._login.login()
        except AlexapyConnectionError as err:
            raise AlexaConnectionError(str(err)) from err
        except AlexapyLoginError as err:
            raise AlexaAuthError(str(err)) from err
        if not self._login.status.get("login_successful"):
            raise AlexaAuthError("Refresh token was rejected")

    async def async_close(self) -> None:
        """Close the alexapy session."""
        await self._login.close()

    def _raise_for_status(self) -> None:
        status: dict[str, Any] = self._login.status or {}
        if status.get("login_successful"):
            return
        # The status keys tell which Amazon page alexapy ended on; they hold no
        # credentials. Enable debug logging to see them when sign-in fails.
        _LOGGER.debug(
            "Amazon sign-in did not complete, alexapy status: %s",
            {key: value for key, value in status.items() if key != "captcha_image_url"},
        )
        if status.get("securitycode_required"):
            raise AlexaLoginChallenge(CHALLENGE_SECURITY_CODE)
        if status.get("captcha_required"):
            raise AlexaLoginChallenge(CHALLENGE_CAPTCHA, status.get("captcha_image_url"))
        if any(
            status.get(key)
            for key in (
                "claimspicker_required",
                "authselect_required",
                "verificationcode_required",
                "action_required",
                "force_get",
            )
        ):
            _LOGGER.debug("Unsupported Amazon login step: %s", sorted(status))
            raise AlexaLoginChallenge(CHALLENGE_UNSUPPORTED)
        raise AlexaAuthError(str(status.get("login_failed") or "login_failed"))
