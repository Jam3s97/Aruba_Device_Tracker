"""Catch translation problems that otherwise only hassfest (CI) reports."""

import json
from pathlib import Path

COMPONENT = Path(__file__).parent.parent / "custom_components" / "aruba_device_tracker"


def load(name: str) -> dict:
    """Load one of the integration's translation files."""
    return json.loads((COMPONENT / name).read_text())


def test_strings_and_english_translation_match():
    """translations/en.json is a copy of strings.json for custom integrations."""
    assert load("strings.json") == load("translations/en.json")


def test_config_subentry_types_have_the_keys_hassfest_requires():
    """
    Hassfest requires entry_type, initiate_flow and step for every subentry type.

    The cluster subentry is created in code with no subentry flow, so
    initiate_flow and step are never shown; entry_type is the group's second
    line on the integration page.
    """
    for subentry_type, strings in load("strings.json")["config_subentries"].items():
        missing = {"entry_type", "initiate_flow", "step"} - set(strings)
        assert not missing, f"{subentry_type} is missing {sorted(missing)}"
        assert "user" in strings["initiate_flow"], subentry_type
