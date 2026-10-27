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
    amazon_domain: str
    tokens: dict[str, Any]


def find_alexa_media_sessions(hass: HomeAssistant) -> list[AlexaMediaSession]:
    """Return Alexa Media Player accounts that hold a refresh token."""
    sessions = []
    for entry in hass.config_entries.async_entries(ALEXA_MEDIA_DOMAIN):
        oauth = entry.data.get(ALEXA_MEDIA_OAUTH) or {}
        email, url = entry.data.get(CONF_EMAIL), entry.data.get(CONF_URL)
        if not (oauth.get("refresh_token") and email and url):
            continue
        tokens = {key: oauth[key] for key in TOKEN_KEYS if oauth.get(key) is not None}
        sessions.append(AlexaMediaSession(entry.entry_id, email, str(url).lower(), tokens))
    return sessions


async def async_alexa_media_uuid(hass: HomeAssistant, session: AlexaMediaSession) -> str:
    """Return the device uuid Alexa Media Player registered this account with.

    Mirrors alexa_media.helpers.calculate_uuid: the Home Assistant instance id,
    plus the account's index among alexa_media entries, plus a hash of the
    email and Amazon domain, keeping the last 32 hex characters.
    """
    entries = hass.config_entries.async_entries(ALEXA_MEDIA_DOMAIN)
    index = next(
        (
            position
            for position, entry in enumerate(entries)
            if entry.data.get(CONF_EMAIL) == session.email
            and str(entry.data.get(CONF_URL, "")).lower() == session.amazon_domain
        ),
        0,
    )
    instance = await instance_id.async_get(hass)
    digest = hashlib.sha256(
        (session.email.lower() + session.amazon_domain.lower()).encode()
    ).hexdigest()
    return hex(int(instance, 16) + index + int(digest, 16))[-32:]
