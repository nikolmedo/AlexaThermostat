"""Constants for the Alexa Thermostat integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "alexa_thermostat"

CONF_AMAZON_DOMAIN: Final = "amazon_domain"
CONF_OTP_SECRET: Final = "otp_secret"
CONF_TOKENS: Final = "tokens"
CONF_DEVICE_UUID: Final = "device_uuid"
CONF_SECURITY_CODE: Final = "security_code"
CONF_CAPTCHA: Final = "captcha"
CONF_SCAN_INTERVAL: Final = "scan_interval"

DEFAULT_AMAZON_DOMAIN: Final = "amazon.com"
AMAZON_DOMAINS: Final = [
    "amazon.com",
    "amazon.ca",
    "amazon.com.mx",
    "amazon.com.br",
    "amazon.co.uk",
    "amazon.de",
    "amazon.fr",
    "amazon.it",
    "amazon.es",
    "amazon.co.jp",
    "amazon.com.au",
    "amazon.in",
]

DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 3600

# Alexa needs a moment before a command shows up in its state API.
COMMAND_SETTLE_DELAY: Final = timedelta(seconds=15)
