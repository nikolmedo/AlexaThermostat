"""Reuse the Amazon session of an Alexa Media Player account.

Amazon's password sign-in pages change often and block automated logins.
Alexa Media Player (domain alexa_media) signs in through a browser proxy and
keeps an OAuth refresh token in its config entry. That token, together with
the device uuid it was registered with, is all this integration needs, so a
user who already runs Alexa Media Player can skip the password sign-in.

Only read access to Alexa Media Player's config entry is used; nothing in it
is changed. The format below matches alexa_media 5.x (config_flow.py and
helpers.calculate_uuid).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from homeassistant.const import CONF_EMAIL, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import instance_id

from .api.auth import TOKEN_KEYS

ALEXA_MEDIA_DOMAIN = "alexa_media"
ALEXA_MEDIA_OAUTH = "oauth"


@dataclass(frozen=True, slots=True)
class AlexaMediaSession:
    """An Alexa Media Player account that can be reused."""

    entry_id: str
    email: str
    url: str
    tokens: dict[str, Any]

    @property
    def amazon_domain(self) -> str:
        """Return the Amazon domain, normalized for this integration."""
        return self.url.lower()


def find_alexa_media_sessions(hass: HomeAssistant) -> list[AlexaMediaSession]:
    """Return Alexa Media Player accounts that hold a refresh token."""
    sessions = []
    for entry in hass.config_entries.async_entries(ALEXA_MEDIA_DOMAIN):
        oauth = entry.data.get(ALEXA_MEDIA_OAUTH)
        email, url = entry.data.get(CONF_EMAIL), entry.data.get(CONF_URL)
        if not (isinstance(oauth, dict) and oauth.get("refresh_token")):
            continue
        if not (isinstance(email, str) and isinstance(url, str) and email and url):
            continue
        tokens = {key: oauth[key] for key in TOKEN_KEYS if oauth.get(key) is not None}
        sessions.append(AlexaMediaSession(entry.entry_id, email, url, tokens))
    return sessions


async def async_alexa_media_uuid(hass: HomeAssistant, session: AlexaMediaSession) -> str:
    """Return the device uuid Alexa Media Player registered this account with.

    Mirrors alexa_media.helpers.calculate_uuid exactly (same comparisons on the
    raw stored email and url): the Home Assistant instance id, plus the
    account's index among alexa_media entries (0 when none matches), plus a
    hash of the lowercased email and url, keeping the last 32 hex characters.
    tests/test_alexa_media_session.py checks it against a verbatim copy.
    """
    index = 0
    for position, entry in enumerate(hass.config_entries.async_entries(ALEXA_MEDIA_DOMAIN)):
        if entry.data.get(CONF_EMAIL) == session.email and entry.data.get(CONF_URL) == session.url:
            index = position
            break
    instance = await instance_id.async_get(hass)
    digest = hashlib.sha256((session.email.lower() + session.url.lower()).encode()).hexdigest()
    return hex(int(instance, 16) + index + int(digest, 16))[-32:]
