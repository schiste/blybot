"""Per-scope LLM settings: grammar, clamps, storage round-trip."""

from __future__ import annotations

import json

import pytest

from blybot.domain.models import LlmSettings
from blybot.services.llmconf import (
    LlmParseError,
    describe_llm,
    dumps_llm,
    loads_llm,
    parse_llm_args,
)

DEFAULTS = LlmSettings()
CEILING = 4096


def test_overrides_apply_on_top_of_the_base() -> None:
    settings = parse_llm_args("lang:FR model:large temp:0.4 max_tokens:800", DEFAULTS, CEILING)
    assert settings.lang == "fr"  # normalized
    assert settings.model == "large"
    assert settings.temperature == 0.4
    assert settings.max_tokens == 800
    assert settings.platform == "liftwing"  # untouched fields keep the base


def test_platform_key_accepts_known_platforms_only() -> None:
    assert parse_llm_args("platform:liftwing", DEFAULTS, CEILING).platform == "liftwing"
    with pytest.raises(LlmParseError, match="Unknown platform"):
        parse_llm_args("platform:skynet", DEFAULTS, CEILING)


@pytest.mark.parametrize(
    "text",
    [
        "",  # nothing to set
        "model",  # not key:value
        "model:qwen99",  # not an alias
        "lang:english!",  # not a language code
        "lang:abcdefghij",  # too long
        "temp:hot",
        "temp:1.5",
        "max_tokens:0",
        "max_tokens:99999",  # above the ceiling
        "max_tokens:many",
        "typo:x",
    ],
)
def test_bad_arguments_are_rejected_with_guidance(text: str) -> None:
    with pytest.raises(LlmParseError):
        parse_llm_args(text, DEFAULTS, CEILING)


def test_describe_reads_back_as_settable_tokens() -> None:
    line = describe_llm(LlmSettings(model="large", lang="fr", temperature=0.4, max_tokens=800))
    assert line == "platform:liftwing model:large lang:fr temp:0.4 max_tokens:800"


def test_settings_round_trip_through_json() -> None:
    settings = LlmSettings(model="large", lang="pt-br", temperature=0.7, max_tokens=2048)
    assert loads_llm(dumps_llm(settings)) == settings
    assert loads_llm(None) is None
    assert loads_llm("") is None
    # Older rows with missing keys default per-field.
    assert loads_llm("{}") == LlmSettings()


def test_loads_llm_coerces_a_tampered_row_to_safe_values() -> None:
    """A directly-edited DB row can't smuggle a crafted lang/model into use."""
    tampered = json.dumps(
        {
            "platform": "evil",
            "model": "sneaky",
            "lang": "'; DROP TABLE",
            "temperature": 9.0,
            "max_tokens": -5,
        }
    )
    assert loads_llm(tampered) == LlmSettings(
        platform="liftwing", model="default", lang="en", temperature=1.0, max_tokens=1
    )


def test_settings_model_validates_ranges() -> None:
    with pytest.raises(ValueError, match="temperature"):
        LlmSettings(temperature=1.5)
    with pytest.raises(ValueError, match="max_tokens"):
        LlmSettings(max_tokens=0)
    with pytest.raises(ValueError, match="non-empty"):
        LlmSettings(platform="")
