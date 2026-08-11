"""Config loading tests (spec section 12)."""

from __future__ import annotations

from dataclasses import fields
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet

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


def test_platform_defaults_to_telegram() -> None:
    config = load_config(dict(REQUIRED))
    assert config.platform == "telegram"
    assert config.telegram_bot_token == REQUIRED["TELEGRAM_BOT_TOKEN"]
    assert config.discord_bot_token == ""


def test_discord_platform_requires_only_the_discord_token() -> None:
    env = dict(REQUIRED)
    del env["TELEGRAM_BOT_TOKEN"]  # a Discord deployment needs no Telegram token
    env |= {"PLATFORM": "discord", "DISCORD_BOT_TOKEN": "discord-secret"}
    config = load_config(env)
    assert config.platform == "discord"
    assert config.discord_bot_token == "discord-secret"  # noqa: S105 -- test fixture, not a secret
    assert config.telegram_bot_token == ""


def test_discord_platform_missing_token_is_reported() -> None:
    env = dict(REQUIRED)
    del env["TELEGRAM_BOT_TOKEN"]
    env["PLATFORM"] = "discord"
    with pytest.raises(ConfigurationError, match="DISCORD_BOT_TOKEN"):
        load_config(env)


def test_irc_platform_requires_only_the_server() -> None:
    env = dict(REQUIRED)
    del env["TELEGRAM_BOT_TOKEN"]  # an IRC deployment needs no Telegram token
    env |= {"PLATFORM": "irc", "IRC_SERVER": "irc.libera.chat"}
    config = load_config(env)
    assert config.platform == "irc"
    assert config.irc_server == "irc.libera.chat"
    assert config.irc_port == 6697  # TLS by default
    assert config.irc_tls is True
    assert config.irc_nick == "blybot"
    assert config.irc_channels == ()
    assert config.telegram_bot_token == ""


def test_irc_platform_missing_server_is_reported() -> None:
    env = dict(REQUIRED)
    del env["TELEGRAM_BOT_TOKEN"]
    env["PLATFORM"] = "irc"
    with pytest.raises(ConfigurationError, match="IRC_SERVER"):
        load_config(env)


def test_irc_channels_are_split_and_trimmed() -> None:
    env = dict(REQUIRED) | {"IRC_CHANNELS": "#wikipedia-fr, #wikimedia-tech ,"}
    assert load_config(env).irc_channels == ("#wikipedia-fr", "#wikimedia-tech")


def test_irc_tls_can_be_switched_off_explicitly() -> None:
    env = dict(REQUIRED) | {"IRC_TLS": "OFF", "IRC_PORT": "6667"}
    config = load_config(env)
    assert config.irc_tls is False
    assert config.irc_port == 6667


def test_a_typo_in_irc_tls_does_not_silently_downgrade_the_connection() -> None:
    env = dict(REQUIRED) | {"IRC_TLS": "false"}
    with pytest.raises(ConfigurationError, match="IRC_TLS must be one of"):
        load_config(env)


def test_irc_send_pacing_is_configurable() -> None:
    """The token bucket is a client-side guard against a limit we cannot
    query, not a protocol constant — a voiced bot has real headroom."""
    default = load_config(dict(REQUIRED))
    assert (default.irc_send_burst, default.irc_send_interval) == (4, 2.0)

    tuned = load_config(dict(REQUIRED) | {"IRC_SEND_BURST": "10", "IRC_SEND_INTERVAL": "0.5"})
    assert (tuned.irc_send_burst, tuned.irc_send_interval) == (10, 0.5)


@pytest.mark.parametrize("bad", ["0", "-1", "fast"])
def test_a_nonsensical_send_interval_is_rejected(bad: str) -> None:
    """Zero or negative would disable pacing silently and get us killed."""
    with pytest.raises(ConfigurationError, match="IRC_SEND_INTERVAL must be a positive number"):
        load_config(dict(REQUIRED) | {"IRC_SEND_INTERVAL": bad})


def test_unknown_platform_is_rejected() -> None:
    env = dict(REQUIRED) | {"PLATFORM": "slack"}
    with pytest.raises(ConfigurationError, match="PLATFORM must be one of"):
        load_config(env)


def test_group_allowlist_is_parsed() -> None:
    env = dict(REQUIRED) | {"ALLOWED_GROUP_IDS": "-100123, -100456"}
    config = load_config(env)
    assert config.allowed_group_ids == frozenset({"-100123", "-100456"})


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


def test_self_service_defaults_are_off_and_toolsdb_conventional() -> None:
    config = load_config(dict(REQUIRED))
    assert config.wiki_page_suffix == ""
    assert config.profile_encryption_key == ""
    assert config.toolsdb_host == "tools.db.svc.wikimedia.cloud"
    assert config.toolsdb_name == ""
    assert config.toolsdb_cnf.endswith("replica.my.cnf")
    assert config.events_poll_minutes == 5


def test_github_settings_default_to_public_repo_and_no_token() -> None:
    config = load_config(dict(REQUIRED))
    assert config.github_repo == "schiste/blybot"
    assert config.github_token == ""


def test_explicit_ttl_override_is_honored() -> None:
    config = load_config(dict(REQUIRED) | {"SESSION_TTL_MINUTES": "30"})
    assert config.session_ttl == timedelta(minutes=30)


def test_reannounce_days_accepts_zero_and_rejects_negatives() -> None:
    config = load_config({**REQUIRED, "CAPTURE_REANNOUNCE_DAYS": "0"})
    assert config.capture_reannounce_days == 0
    config = load_config({**REQUIRED, "CAPTURE_REANNOUNCE_DAYS": "30"})
    assert config.capture_reannounce_days == 30
    with pytest.raises(ConfigurationError, match="negative"):
        load_config({**REQUIRED, "CAPTURE_REANNOUNCE_DAYS": "-1"})
    with pytest.raises(ConfigurationError, match="integer"):
        load_config({**REQUIRED, "CAPTURE_REANNOUNCE_DAYS": "monthly"})


def test_no_credential_survives_into_the_config_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #24: a dataclass renders every field, so one stray print(config)
    or a framework dumping locals would spill all six secrets at once."""
    secrets = {
        "TELEGRAM_BOT_TOKEN": "tg-canary-value",
        "DISCORD_BOT_TOKEN": "dc-canary-value",
        "WIKI_BOTPASSWORD": "wiki-canary-value",
        "GITHUB_TOKEN": "gh-canary-value",
        "PROFILE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ARCHIVE_PSEUDONYM_KEY": "hmac-canary-value",
    }
    for key, value in {**REQUIRED, **secrets}.items():
        monkeypatch.setenv(key, value)

    config = load_config()
    rendered = repr(config)
    for name, value in secrets.items():
        assert value not in rendered, f"{name} leaked into repr(Config)"
    # The values are still reachable the normal way — repr=False changes
    # visibility, not behavior.
    assert config.telegram_bot_token == secrets["TELEGRAM_BOT_TOKEN"]
    assert config.archive_pseudonym_key == secrets["ARCHIVE_PSEUDONYM_KEY"]
    # A non-secret field still renders, so this isn't vacuously passing.
    assert "Blybot" in rendered


def test_every_credential_field_is_marked_non_repr() -> None:
    """Guards the list itself: a new secret field must opt out explicitly."""
    non_repr = {field.name for field in fields(Config) if not field.repr}
    assert non_repr == {
        "telegram_bot_token",
        "discord_bot_token",
        "wiki_botpassword",
        "github_token",
        "profile_encryption_key",
        "archive_pseudonym_key",
        "irc_password",
    }
