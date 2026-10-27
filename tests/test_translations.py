"""Guard the translation file against shapes hassfest rejects."""

from __future__ import annotations

import json
from pathlib import Path
import re

COMPONENT = Path(__file__).parent.parent / "custom_components" / "alexa_thermostat"
EN = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))


def test_exceptions_are_message_objects() -> None:
    """hassfest requires {"message": ...} objects under exceptions, not strings."""
    for key, value in EN["exceptions"].items():
        assert isinstance(value, dict), key
        assert set(value) == {"message"}, key
        assert isinstance(value["message"], str), key


def test_every_translation_key_used_in_code_exists() -> None:
    """Each translation_key raised in the code has an English message."""
    used = {
        match
        for path in COMPONENT.glob("*.py")
        for match in re.findall(r'translation_key="([a-z_]+)"', path.read_text(encoding="utf-8"))
    }
    assert used
    assert used <= set(EN["exceptions"])
