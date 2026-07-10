"""Config loading tests (spec section 12)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from blybot.config import Config, ConfigurationError, load_config
from blybot.domain.models import ConsentMode, TimestampGranularity

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
    assert config.consent_mode is ConsentMode.IMMEDIATE
    assert config.log_throttle_per_minute == 6
    assert "Blybot" in config.group_greeting_text
    assert config.welcome_text


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


def test_minute_timestamp_granularity_is_accepted() -> None:
    config = load_config(dict(REQUIRED) | {"TIMESTAMP_GRANULARITY": "minute"})
    assert config.timestamp_granularity is TimestampGranularity.MINUTE


def test_invalid_timestamp_granularity_is_rejected() -> None:
    env = dict(REQUIRED) | {"TIMESTAMP_GRANULARITY": "precise"}
    with pytest.raises(ConfigurationError, match="TIMESTAMP_GRANULARITY"):
        load_config(env)


def test_edit_summary_is_generic_and_follows_bot_name() -> None:
    config = load_config(dict(REQUIRED) | {"BOT_NAME": "Renamed"})
    assert config.edit_summary == "Log entry via Renamed"


def test_default_copy_follows_bot_name_but_custom_copy_is_verbatim() -> None:
    renamed = load_config(dict(REQUIRED) | {"BOT_NAME": "Renamed"})
    assert "Renamed" in renamed.group_greeting_text

    custom = load_config(dict(REQUIRED) | {"GROUP_GREETING_TEXT": "Hi {bot_name}!"})
    assert custom.group_greeting_text == "Hi {bot_name}!"


def test_author_only_consent_mode_is_accepted() -> None:
    config = load_config(dict(REQUIRED) | {"CONSENT_MODE": "author_only"})
    assert config.consent_mode is ConsentMode.AUTHOR_ONLY


def test_confirm_consent_mode_is_rejected_as_unimplemented() -> None:
    """N1 hook: 'confirm' is reserved; failing loudly beats silent degradation."""
    with pytest.raises(ConfigurationError, match="not implemented"):
        load_config(dict(REQUIRED) | {"CONSENT_MODE": "confirm"})


def test_unknown_consent_mode_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="CONSENT_MODE"):
        load_config(dict(REQUIRED) | {"CONSENT_MODE": "ask-nicely"})


def test_maintainer_defaults_to_empty_and_page_url_builds_wmf_urls() -> None:
    config = load_config(dict(REQUIRED))
    assert config.maintainer == ""
    assert (
        config.page_url("Talk:Next 25/Telegram logs")
        == "https://meta.wikimedia.org/wiki/Talk:Next_25/Telegram_logs"
    )

    named = load_config(dict(REQUIRED) | {"MAINTAINER": "User:Schiste"})
    assert named.maintainer == "User:Schiste"


def test_newcomer_welcome_defaults_on_and_can_be_switched_off() -> None:
    assert load_config(dict(REQUIRED)).newcomer_welcome_enabled is True
    off = load_config(dict(REQUIRED) | {"NEWCOMER_WELCOME": "off"})
    assert off.newcomer_welcome_enabled is False
    with pytest.raises(ConfigurationError, match="NEWCOMER_WELCOME"):
        load_config(dict(REQUIRED) | {"NEWCOMER_WELCOME": "sometimes"})


def test_cleanup_and_throttle_defaults() -> None:
    config = load_config(dict(REQUIRED))
    assert config.log_cleanup_seconds == 5.0
    assert config.reply_cleanup_seconds == 15.0
    assert config.bug_throttle_per_hour == 3
    assert config.wiki_max_retries == 5


def test_cleanup_zero_means_disabled_not_immediate() -> None:
    config = load_config(dict(REQUIRED) | {"LOG_CLEANUP_SECONDS": "0"})
    assert config.log_cleanup_seconds == -1.0  # sentinel: never delete


def test_cleanup_rejects_negatives_and_junk() -> None:
    with pytest.raises(ConfigurationError, match="REPLY_CLEANUP_SECONDS"):
        load_config(dict(REQUIRED) | {"REPLY_CLEANUP_SECONDS": "-3"})
    with pytest.raises(ConfigurationError, match="LOG_CLEANUP_SECONDS"):
        load_config(dict(REQUIRED) | {"LOG_CLEANUP_SECONDS": "soon"})


def test_github_settings_default_to_public_repo_and_no_token() -> None:
    config = load_config(dict(REQUIRED))
    assert config.github_repo == "schiste/blybot"
    assert config.github_token == ""


def test_explicit_ttl_override_is_honored() -> None:
    config = load_config(dict(REQUIRED) | {"SESSION_TTL_MINUTES": "30"})
    assert config.session_ttl == timedelta(minutes=30)
