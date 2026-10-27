"""Alexa smart home thermostat API.

This package has no Home Assistant imports on purpose: it can be extracted
into a standalone PyPI library, which Home Assistant core requires before an
integration can be accepted there.
"""

from .auth import (
    CHALLENGE_CAPTCHA,
    CHALLENGE_SECURITY_CODE,
    CHALLENGE_UNSUPPORTED,
    AlexaAuth,
    is_one_time_code,
    normalize_otp_input,
)
from .client import AlexaThermostatClient, setpoint_payload
from .exceptions import (
    AlexaApiError,
    AlexaAuthError,
    AlexaCommandError,
    AlexaConnectionError,
    AlexaError,
    AlexaInvalidOtpSecret,
    AlexaLoginChallenge,
)
from .models import (
    Operation,
    Temperature,
    TemperatureScale,
    ThermostatDevice,
    ThermostatMode,
    ThermostatState,
)

__all__ = [
    "CHALLENGE_CAPTCHA",
    "CHALLENGE_SECURITY_CODE",
    "CHALLENGE_UNSUPPORTED",
    "AlexaApiError",
    "AlexaAuth",
    "AlexaAuthError",
    "AlexaCommandError",
    "AlexaConnectionError",
    "AlexaError",
    "AlexaInvalidOtpSecret",
    "AlexaLoginChallenge",
    "AlexaThermostatClient",
    "Operation",
    "Temperature",
    "TemperatureScale",
    "ThermostatDevice",
    "ThermostatMode",
    "ThermostatState",
    "is_one_time_code",
    "normalize_otp_input",
    "setpoint_payload",
]
