"""Config flow for Alexa Thermostat."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import re
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol
from yarl import URL

from .api import (
    AlexaAuth,
    AlexaAuthError,
    AlexaConnectionError,
    AlexaError,
    AlexaInvalidOtpSecret,
    AlexaThermostatClient,
    is_one_time_code,
    normalize_otp_input,
)
from .const import (
    AMAZON_DOMAINS,
    CONF_AMAZON_DOMAIN,
    CONF_DEVICE_UUID,
    CONF_HASS_URL,
    CONF_OTP_SECRET,
    CONF_SCAN_INTERVAL,
    CONF_TOKENS,
    DEFAULT_AMAZON_DOMAIN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import AlexaThermostatConfigEntry
from .proxy_view import async_start_sign_in, async_stop_sign_in

_LOGGER = logging.getLogger(__name__)

# The region is used as the login and API host, so only Amazon retail domains
# are accepted (amazon.com, amazon.de, amazon.co.uk, amazon.com.mx, ...).
AMAZON_DOMAIN_PATTERN = re.compile(r"amazon\.(?:[a-z]{2,3}|co\.[a-z]{2}|com\.[a-z]{2})")

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
HASS_URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
# Used when Home Assistant has no URL configured (Settings > System > Network).
FALLBACK_HASS_URL = "http://homeassistant.local:8123"

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL)),
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        vol.Required(CONF_AMAZON_DOMAIN, default=DEFAULT_AMAZON_DOMAIN): SelectSelector(
            SelectSelectorConfig(
                options=AMAZON_DOMAINS,
                custom_value=True,
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_OTP_SECRET): PASSWORD_SELECTOR,
        vol.Required(CONF_HASS_URL): HASS_URL_SELECTOR,
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        vol.Optional(CONF_OTP_SECRET): PASSWORD_SELECTOR,
        vol.Required(CONF_HASS_URL): HASS_URL_SELECTOR,
    }
)


def normalize_hass_url(value: str) -> str | None:
    """Return the origin of an http(s) URL, or None when it is not one."""
    url = URL(value.strip())
    if url.scheme not in ("http", "https") or not url.host:
        return None
    return str(url.origin())


class AlexaThermostatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in to Amazon in the browser and create the config entry."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._auth: AlexaAuth | None = None
        self._email: str = ""
        self._amazon_domain: str = DEFAULT_AMAZON_DOMAIN
        self._hass_url: str | None = None
        self._sign_in_started = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: AlexaThermostatConfigEntry) -> OptionsFlowHandler:
        """Return the options flow."""
        return OptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the Amazon account, then sign in through the browser."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._amazon_domain = user_input[CONF_AMAZON_DOMAIN].strip().lower()
            if not AMAZON_DOMAIN_PATTERN.fullmatch(self._amazon_domain):
                errors[CONF_AMAZON_DOMAIN] = "invalid_domain"
            else:
                await self.async_set_unique_id(f"{self._amazon_domain}:{self._email.lower()}")
                self._abort_if_unique_id_configured()
                if self._prepare_auth(user_input, errors):
                    return await self.async_step_sign_in()

        return self._show_user_form(user_input, errors)

    def _show_user_form(
        self, user_input: Mapping[str, Any] | None, errors: dict[str, str]
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA,
                {
                    CONF_HASS_URL: self._suggested_hass_url(),
                    **{
                        key: value
                        for key, value in (user_input or {}).items()
                        if key not in (CONF_PASSWORD, CONF_OTP_SECRET)
                    },
                },
            ),
            errors=errors,
        )

    def _suggested_hass_url(self) -> str:
        """Prefill the address the browser opens Home Assistant with."""
        if self._hass_url:
            return self._hass_url
        try:
            return get_url(self.hass)
        except NoURLAvailableError:
            return FALLBACK_HASS_URL

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start a reauth when the refresh token stops working."""
        self._email = entry_data[CONF_EMAIL]
        self._amazon_domain = entry_data[CONF_AMAZON_DOMAIN]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again, then sign in through the browser."""
        errors: dict[str, str] = {}
        if user_input is not None and self._prepare_auth(user_input, errors):
            return await self.async_step_sign_in()
        return self._show_reauth_form(errors)

    def _show_reauth_form(self, errors: dict[str, str]) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                REAUTH_SCHEMA, {CONF_HASS_URL: self._suggested_hass_url()}
            ),
            errors=errors,
            description_placeholders={"email": self._email},
        )

    def _prepare_auth(self, user_input: Mapping[str, Any], errors: dict[str, str]) -> bool:
        """Create the alexapy login for this attempt; False when the input is invalid."""
        if (hass_url := normalize_hass_url(user_input[CONF_HASS_URL])) is None:
            errors[CONF_HASS_URL] = "invalid_hass_url"
            return False
        self._hass_url = hass_url
        otp_input = normalize_otp_input(user_input.get(CONF_OTP_SECRET))
        if is_one_time_code(otp_input):
            # A current 6-digit code cannot generate the next ones. Amazon's page
            # asks for the code instead.
            otp_input = ""
        reauth_uuid = None
        if self.source == SOURCE_REAUTH:
            reauth_uuid = self._get_reauth_entry().data.get(CONF_DEVICE_UUID)
        try:
            self._auth = AlexaAuth(
                self._amazon_domain,
                self._email,
                self.hass.config.path,
                password=user_input[CONF_PASSWORD],
                otp_secret=otp_input,
                device_uuid=reauth_uuid,
            )
        except AlexaInvalidOtpSecret:
            errors[CONF_OTP_SECRET] = "invalid_otp_secret"
            return False
        return True

    async def async_step_sign_in(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Open Amazon's sign-in page in the browser, through Home Assistant."""
        assert self._auth is not None
        if not self._sign_in_started:
            self._sign_in_started = True
            # The frontend only opens absolute http(s) URLs.
            url = f"{self._hass_url}{async_start_sign_in(self.hass, self.flow_id, self._auth)}"
            return self.async_external_step(
                step_id="sign_in", url=url, description_placeholders={"url": url}
            )
        # The callback view resumes the flow once Amazon confirmed the sign-in.
        return self.async_external_step_done(next_step_id="finish")

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Exchange the browser sign-in for tokens and look for thermostats."""
        assert self._auth is not None
        await async_stop_sign_in(self.hass, self.flow_id)
        self._sign_in_started = False
        try:
            await self._auth.async_finish_proxy_login()
        except AlexaConnectionError:
            return await self._async_sign_in_failed("cannot_connect")
        except AlexaAuthError:
            return await self._async_sign_in_failed("sign_in_failed")
        return await self._async_finish_login()

    async def _async_sign_in_failed(self, error: str) -> ConfigFlowResult:
        """Show the form again; the next submit starts a new sign-in."""
        await self._async_close_auth()
        if self.source == SOURCE_REAUTH:
            return self._show_reauth_form({"base": error})
        return self._show_user_form(
            {CONF_EMAIL: self._email, CONF_AMAZON_DOMAIN: self._amazon_domain},
            {"base": error},
        )

    async def _async_finish_login(self) -> ConfigFlowResult:
        assert self._auth is not None
        try:
            devices = await AlexaThermostatClient(self._auth).async_get_thermostats()
        except AlexaError:
            _LOGGER.debug("Signed in but could not list thermostats", exc_info=True)
            await self._async_close_auth()
            return self.async_abort(reason="cannot_connect")

        data = {
            CONF_EMAIL: self._email,
            CONF_AMAZON_DOMAIN: self._amazon_domain,
            CONF_TOKENS: self._auth.tokens,
            CONF_DEVICE_UUID: self._auth.device_uuid,
        }
        await self._async_close_auth()

        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        if not devices:
            return self.async_abort(reason="no_thermostats")
        return self.async_create_entry(title=self._email, data=data)

    @callback
    def async_remove(self) -> None:
        """Stop the sign-in and close the Amazon session when the flow ends."""
        self.hass.async_create_task(self._async_cleanup())

    async def _async_cleanup(self) -> None:
        await async_stop_sign_in(self.hass, self.flow_id)
        await self._async_close_auth()

    async def _async_close_auth(self) -> None:
        if self._auth is not None:
            await self._auth.async_close()
            self._auth = None


class OptionsFlowHandler(OptionsFlowWithReload):
    """Tune how often Alexa is polled."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options form."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=self.config_entry.options.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
