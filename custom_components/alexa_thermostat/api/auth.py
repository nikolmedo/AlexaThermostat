"""Amazon login, isolated behind a small interface.

Amazon changes its login pages often. Everything that touches alexapy lives
in this module so a broken login can be fixed (or the library swapped) without
touching the GraphQL client or Home Assistant code.

Sign-in goes through a browser proxy: the user signs in on Amazon's own pages,
served through Home Assistant, so Amazon's robot checks, captchas and 2FA
prompts are solved by a person. Only the OAuth refresh token and the device
uuid are persisted. The password is used once to fill in the form and never
stored.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
import logging
import re
from typing import Any
import unicodedata

from aiohttp import ClientError, ClientSession, web
from alexapy import AlexaLogin
from alexapy.alexaproxy import AlexaProxy
from alexapy.errors import (
    AlexapyConnectionError,
    AlexapyLoginError,
    AlexapyPyotpInvalidKey,
)
import httpx
from yarl import URL

from .exceptions import (
    AlexaAuthError,
    AlexaConnectionError,
    AlexaInvalidOtpSecret,
)

_LOGGER = logging.getLogger(__name__)

ALEXAPY_FILE_PREFIX = "alexa_media."
TOKEN_KEYS = ("access_token", "refresh_token", "mac_dms", "expires_in")

# Amazon lands on one of these once the browser sign-in is complete.
SIGNED_IN_PATHS = ("/ap/maplanding", "/spa/index.html")

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

    async def async_create_proxy(
        self,
        base_url: str,
        *,
        start_url: str,
        return_url: str,
        session_factory: Callable[[], httpx.AsyncClient],
    ) -> AlexaSignInProxy:
        """Create the proxy that serves Amazon's sign-in pages under base_url.

        start_url is where the browser enters the proxy and return_url is where
        it is sent once Amazon confirms the sign-in.
        """
        # The proxy builds a default httpx client, which loads SSL certificates:
        # keep that blocking work off the event loop.
        proxy = await asyncio.get_running_loop().run_in_executor(
            None, AlexaProxy, self._login, base_url
        )
        default_session = proxy.session
        proxy.session_factory = session_factory
        proxy.session = session_factory()
        await default_session.aclose()
        # alexapy sends the browser back here when Amazon drops it on its home page.
        self._login.proxy_url = URL(start_url)
        return AlexaSignInProxy(proxy, self._login, URL(return_url))

    async def async_finish_proxy_login(self) -> None:
        """Exchange what the proxy captured for tokens and confirm the account."""
        try:
            signed_in = await self._login.test_loggedin()
        except AlexapyConnectionError as err:
            raise AlexaConnectionError(str(err)) from err
        except AlexapyLoginError as err:
            raise AlexaAuthError(str(err)) from err
        except ClientError as err:
            raise AlexaConnectionError(str(err)) from err
        if not signed_in:
            raise AlexaAuthError("Amazon did not confirm the sign-in")

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


class AlexaSignInProxy:
    """Serve Amazon's sign-in pages to the browser and notice when it is done."""

    def __init__(self, proxy: AlexaProxy, login: AlexaLogin, return_url: URL) -> None:
        """Wrap alexapy's proxy so success always returns to our own callback."""
        self._proxy = proxy
        self._login = login
        self._return_url = return_url
        self.signed_in = False
        self._amazon_test = proxy.test_amazon_url
        proxy.tests = {"test_amazon_url": self.async_check_response}

    async def async_check_response(
        self, resp: Any, data: dict[str, Any], query: dict[str, Any]
    ) -> URL | str | None:
        """Run after every Amazon response; a URL result redirects the browser."""
        result = await self._amazon_test(resp, data, query)
        if URL(str(resp.url)).path in SIGNED_IN_PATHS:
            # alexapy captured the authorization code. Ignore its own result:
            # it would redirect to a callback_url taken from the query string.
            self.signed_in = True
            return self._return_url
        return result

    async def async_handle(self, request: web.Request) -> web.StreamResponse:
        """Proxy one browser request to Amazon."""
        # 2FA codes expire every 30 seconds, so fill in a fresh one each time.
        self._proxy.modifiers = {
            "autofill": partial(
                self._proxy.autofill,
                {
                    "email": self._login.email,
                    "password": self._login.password,
                    "otpCode": self._login.get_totp_token(),
                },
            )
        }
        return await self._proxy.all_handler(request)

    async def async_close(self) -> None:
        """Close the proxy's connection to Amazon."""
        await self._proxy.session.aclose()
