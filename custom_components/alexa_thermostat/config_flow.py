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

from .api import (
    CHALLENGE_CAPTCHA,
    CHALLENGE_SECURITY_CODE,
    AlexaAuth,
    AlexaAuthError,
    AlexaConnectionError,
    AlexaError,
    AlexaInvalidOtpSecret,
    AlexaLoginChallenge,
    AlexaThermostatClient,
    is_one_time_code,
    normalize_otp_input,
)
from .const import (
    AMAZON_DOMAINS,
    CONF_AMAZON_DOMAIN,
    CONF_CAPTCHA,
    CONF_DEVICE_UUID,
    CONF_OTP_SECRET,
    CONF_SCAN_INTERVAL,
    CONF_SECURITY_CODE,
    CONF_TOKENS,
    DEFAULT_AMAZON_DOMAIN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import AlexaThermostatConfigEntry

_LOGGER = logging.getLogger(__name__)

# The region is used as the login and API host, so only Amazon retail domains
# are accepted (amazon.com, amazon.de, amazon.co.uk, amazon.com.mx, ...).
AMAZON_DOMAIN_PATTERN = re.compile(r"amazon\.(?:[a-z]{2,3}|co\.[a-z]{2}|com\.[a-z]{2})")

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

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
    }
)

REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        vol.Optional(CONF_OTP_SECRET): PASSWORD_SELECTOR,
    }
)

SECURITY_CODE_SCHEMA = vol.Schema({vol.Required(CONF_SECURITY_CODE): str})
CAPTCHA_SCHEMA = vol.Schema({vol.Required(CONF_CAPTCHA): str})


class AlexaThermostatConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in to Amazon and create the config entry."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._auth: AlexaAuth | None = None
        self._email: str = ""
        self._amazon_domain: str = DEFAULT_AMAZON_DOMAIN
        self._captcha_url: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: AlexaThermostatConfigEntry) -> OptionsFlowHandler:
        """Return the options flow."""
        return OptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the Amazon credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._amazon_domain = user_input[CONF_AMAZON_DOMAIN].strip().lower()
            if not AMAZON_DOMAIN_PATTERN.fullmatch(self._amazon_domain):
                errors[CONF_AMAZON_DOMAIN] = "invalid_domain"
            else:
                await self.async_set_unique_id(f"{self._amazon_domain}:{self._email.lower()}")
                self._abort_if_unique_id_configured()
                result = await self._async_start_login(user_input, errors)
                if result is not None:
                    return result

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA,
                {
                    key: value
                    for key, value in (user_input or {}).items()
                    if key not in (CONF_PASSWORD, CONF_OTP_SECRET)
                },
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start a reauth when the refresh token stops working."""
        self._email = entry_data[CONF_EMAIL]
        self._amazon_domain = entry_data[CONF_AMAZON_DOMAIN]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_start_login(user_input, errors)
            if result is not None:
                return result

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email},
        )

    async def async_step_security_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the one-time code Amazon sent."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_continue_login(
                {"securitycode": user_input[CONF_SECURITY_CODE].strip()}, errors
            )
            if result is not None:
                return result

        return self.async_show_form(
            step_id="security_code", data_schema=SECURITY_CODE_SCHEMA, errors=errors
        )

    async def async_step_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to solve the captcha Amazon showed."""
        errors: dict[str, str] = {}
        if user_input is not None:
            result = await self._async_continue_login(
                {"captcha": user_input[CONF_CAPTCHA].strip()}, errors
            )
            if result is not None:
                return result

        return self.async_show_form(
            step_id="captcha",
            data_schema=CAPTCHA_SCHEMA,
            errors=errors,
            description_placeholders={"captcha_url": self._captcha_url},
        )

    async def _async_start_login(
        self, user_input: dict[str, Any], errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        await self._async_close_auth()
        reauth_uuid = None
        if self.source == SOURCE_REAUTH:
            reauth_uuid = self._get_reauth_entry().data.get(CONF_DEVICE_UUID)
        otp_input = normalize_otp_input(user_input.get(CONF_OTP_SECRET))
        login_data = {CONF_PASSWORD: user_input[CONF_PASSWORD]}
        if is_one_time_code(otp_input):
            # The user typed the current 6-digit code instead of the key:
            # use it for this login only.
            login_data["securitycode"] = otp_input
            otp_input = ""
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
            return None
        result = await self._async_continue_login(login_data, errors)
        if result is None:
            # The form is shown again and the next submit starts a new login.
            await self._async_close_auth()
        return result

    async def _async_continue_login(
        self, data: dict[str, str], errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        assert self._auth is not None
        try:
            await self._auth.async_login(data)
        except AlexaLoginChallenge as challenge:
            if challenge.challenge == CHALLENGE_SECURITY_CODE:
                return await self.async_step_security_code()
            if challenge.challenge == CHALLENGE_CAPTCHA:
                self._captcha_url = challenge.captcha_url or ""
                return await self.async_step_captcha()
            errors["base"] = "unsupported_challenge"
        except AlexaInvalidOtpSecret:
            errors["base"] = "invalid_otp_secret"
        except AlexaAuthError:
            errors["base"] = "invalid_auth"
        except AlexaConnectionError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error during Amazon login")
            errors["base"] = "unknown"
        else:
            return await self._async_finish_login(errors)
        return None

    async def _async_finish_login(self, errors: dict[str, str]) -> ConfigFlowResult | None:
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
        """Close the Amazon session when the flow is abandoned."""
        if self._auth is not None:
            self.hass.async_create_task(self._auth.async_close())
            self._auth = None

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
