"""Config loading tests (spec section 12)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from blybot.config import Config, ConfigurationError, load_config
from blybot.domain.models import TimestampGranularity

REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "WIKI_USERNAME": "Blybot@blybot",
    "WIKI_BOTPASSWORD": "hunter2",
    "LOG_TARGET_PAGE": "Meta:Community/Log",
    "DM_TARGET_BASE": "Meta:Community/Discussions",
    "USER_AGENT": "Blybot/0.1 (https://example.org; ops@example.org)",
}


def test_loads_with_defaults() -> None:
    config = load_config(dict(REQUIRED))
    assert isinstance(config, Config)
    assert config.bot_name == "Blybot"
    assert config.wiki_api_url == "https://meta.wikimedia.org/w/api.php"
    assert config.session_ttl == timedelta(minutes=45)
    assert config.burst_debounce == timedelta(seconds=8)
    assert config.timestamp_granularity is TimestampGranularity.DATE
    assert config.allowed_group_ids == frozenset()


def test_missing_keys_are_all_named_but_values_never_echoed() -> None:
    env = dict(REQUIRED)
    del env["TELEGRAM_BOT_TOKEN"]
    del env["USER_AGENT"]
    with pytest.raises(ConfigurationError) as excinfo:
        load_config(env)
    message = str(excinfo.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert "USER_AGENT" in message
    assert "hunter2" not in message


def test_blank_required_value_counts_as_missing() -> None:
    env = dict(REQUIRED) | {"WIKI_BOTPASSWORD": ""}
    with pytest.raises(ConfigurationError, match="WIKI_BOTPASSWORD"):
        load_config(env)


def test_group_allowlist_is_parsed() -> None:
    env = dict(REQUIRED) | {"ALLOWED_GROUP_IDS": "-100123, -100456"}
    config = load_config(env)
    assert config.allowed_group_ids == frozenset({-100123, -100456})


def test_invalid_group_allowlist_is_rejected() -> None:
    env = dict(REQUIRED) | {"ALLOWED_GROUP_IDS": "not-a-number"}
    with pytest.raises(ConfigurationError, match="ALLOWED_GROUP_IDS"):
        load_config(env)


@pytest.mark.parametrize("bad", ["0", "-5", "soon"])
def test_invalid_ttl_is_rejected(bad: str) -> None:
    env = dict(REQUIRED) | {"SESSION_TTL_MINUTES": bad}
    with pytest.raises(ConfigurationError, match="SESSION_TTL_MINUTES"):
        load_config(env)


def test_invalid_timestamp_granularity_is_rejected() -> None:
    env = dict(REQUIRED) | {"TIMESTAMP_GRANULARITY": "precise"}
    with pytest.raises(ConfigurationError, match="TIMESTAMP_GRANULARITY"):
        load_config(env)


def test_edit_summary_is_generic_and_follows_bot_name() -> None:
    config = load_config(dict(REQUIRED) | {"BOT_NAME": "Renamed"})
    assert config.edit_summary == "Log entry via Renamed"
