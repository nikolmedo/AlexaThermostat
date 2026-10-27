"""Exceptions raised by the Alexa API client."""

from __future__ import annotations


class AlexaError(Exception):
    """Base error for the Alexa API client."""


class AlexaAuthError(AlexaError):
    """Authentication failed or the session can no longer be refreshed."""


class AlexaInvalidOtpSecret(AlexaAuthError):
    """The 2FA app key (TOTP secret) is not a valid base32 secret."""


class AlexaConnectionError(AlexaError):
    """The Alexa API could not be reached or returned an unusable response."""


class AlexaApiError(AlexaError):
    """The Alexa API answered with an error payload."""

    def __init__(self, message: str, codes: list[str] | None = None) -> None:
        """Store the error codes reported by Alexa."""
        super().__init__(message)
        self.codes = codes or []


class AlexaCommandError(AlexaApiError):
    """A control command was rejected by Alexa (for example BAD_REQUEST)."""


class AlexaLoginChallenge(AlexaError):
    """Amazon asked for an extra login step (OTP code or captcha)."""

    def __init__(self, challenge: str, captcha_url: str | None = None) -> None:
        """Store which challenge Amazon requested."""
        super().__init__(challenge)
        self.challenge = challenge
        self.captcha_url = captcha_url
