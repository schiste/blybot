"""Configuration loading (spec section 12).

Configuration comes from the process environment (populated on Toolforge
from a ``0600`` file in the tool home directory). Secrets never live in
the repository, and this module never logs values — only key names.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from blybot.domain.models import ConsentMode, TimestampGranularity

_REQUIRED_KEYS: Final = (
    "TELEGRAM_BOT_TOKEN",
    "WIKI_USERNAME",
    "WIKI_BOTPASSWORD",
    "LOG_TARGET_PAGE",
    "DM_TARGET_BASE",
    "USER_AGENT",
)

DEFAULT_BOT_NAME: Final = "Blybot"
DEFAULT_WIKI_API_URL: Final = "https://meta.wikimedia.org/w/api.php"

# Default message copy (spec section 12: "provided"). The {bot_name}
# placeholder is substituted only in these defaults; operator-supplied
# text is used verbatim.
DEFAULT_GROUP_GREETING: Final = (
    "Hello! I'm {bot_name}. Reply to any message with /log to publish it "
    "anonymously to our Meta-wiki log page. I only ever see messages "
    "explicitly marked that way — never ordinary chatter."
)
DEFAULT_WELCOME: Final = (
    "Welcome! Anything you write to me here is transcribed to a public "
    "Meta-wiki page under a random per-session pseudonym. Your Telegram "
    "name and ID are never recorded anywhere. Send /flush at any time to "
    "get a fresh identity, and /help for all commands."
)


class ConfigurationError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Config:
    """Validated runtime configuration."""

    bot_name: str
    telegram_bot_token: str
    wiki_api_url: str
    wiki_username: str
    wiki_botpassword: str
    log_target_page: str
    dm_target_base: str
    allowed_group_ids: frozenset[int]
    session_ttl: timedelta
    burst_debounce: timedelta
    timestamp_granularity: TimestampGranularity
    consent_mode: ConsentMode
    log_throttle_per_minute: int
    group_greeting_text: str
    welcome_text: str
    user_agent: str

    @property
    def edit_summary(self) -> str:
        """Generic, non-identifying edit summary (spec R8)."""
        return f"Log entry via {self.bot_name}"


def load_config(env: dict[str, str] | None = None) -> Config:
    """Build a :class:`Config` from ``env`` (defaults to ``os.environ``).

    Raises :class:`ConfigurationError` naming the missing keys — but
    never echoing any values.
    """
    source = os.environ if env is None else env

    missing = [key for key in _REQUIRED_KEYS if not source.get(key)]
    if missing:
        msg = f"missing required configuration keys: {', '.join(sorted(missing))}"
        raise ConfigurationError(msg)

    try:
        granularity = TimestampGranularity(source.get("TIMESTAMP_GRANULARITY", "date"))
    except ValueError as exc:
        msg = "TIMESTAMP_GRANULARITY must be one of: none, date"
        raise ConfigurationError(msg) from exc

    bot_name = source.get("BOT_NAME", DEFAULT_BOT_NAME)

    return Config(
        bot_name=bot_name,
        telegram_bot_token=source["TELEGRAM_BOT_TOKEN"],
        wiki_api_url=source.get("WIKI_API_URL", DEFAULT_WIKI_API_URL),
        wiki_username=source["WIKI_USERNAME"],
        wiki_botpassword=source["WIKI_BOTPASSWORD"],
        log_target_page=source["LOG_TARGET_PAGE"],
        dm_target_base=source["DM_TARGET_BASE"],
        allowed_group_ids=_parse_group_ids(source.get("ALLOWED_GROUP_IDS", "")),
        session_ttl=timedelta(minutes=_parse_positive_int(source, "SESSION_TTL_MINUTES", 45)),
        burst_debounce=timedelta(seconds=_parse_positive_int(source, "BURST_DEBOUNCE_SECONDS", 8)),
        timestamp_granularity=granularity,
        consent_mode=_parse_consent_mode(source.get("CONSENT_MODE", "immediate")),
        log_throttle_per_minute=_parse_positive_int(source, "LOG_THROTTLE_PER_MINUTE", 6),
        group_greeting_text=source.get(
            "GROUP_GREETING_TEXT", DEFAULT_GROUP_GREETING.format(bot_name=bot_name)
        ),
        welcome_text=source.get("WELCOME_TEXT", DEFAULT_WELCOME),
        user_agent=source["USER_AGENT"],
    )


def _parse_consent_mode(raw: str) -> ConsentMode:
    try:
        mode = ConsentMode(raw)
    except ValueError as exc:
        msg = "CONSENT_MODE must be one of: immediate, confirm, author_only"
        raise ConfigurationError(msg) from exc
    if mode is ConsentMode.CONFIRM:
        # N1 hook: the mode is reserved but the DM-confirmation flow is
        # not built yet. Fail loudly instead of degrading silently.
        msg = "CONSENT_MODE=confirm is not implemented in v1 (N1); use immediate or author_only"
        raise ConfigurationError(msg)
    return mode


def _parse_group_ids(raw: str) -> frozenset[int]:
    try:
        return frozenset(int(part) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        msg = "ALLOWED_GROUP_IDS must be a comma-separated list of integers"
        raise ConfigurationError(msg) from exc


def _parse_positive_int(source: dict[str, str] | os._Environ[str], key: str, default: int) -> int:
    raw = source.get(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"{key} must be an integer"
        raise ConfigurationError(msg) from exc
    if value <= 0:
        msg = f"{key} must be positive"
        raise ConfigurationError(msg)
    return value
