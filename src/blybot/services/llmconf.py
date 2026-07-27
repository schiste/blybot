"""Per-scope LLM settings: parse, clamp, describe, serialize (v3 §2.4).

Pure text ⇄ :class:`~blybot.domain.models.LlmSettings` translation for
the ``/llm`` command, mirroring the rules/actions grammar modules. The
operator-set ceiling is enforced at parse time so a scope admin can
never configure runaway completion sizes.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Final

from blybot.domain.models import LlmSettings

_MODEL_ALIASES: Final = frozenset({"default", "large"})
_PLATFORMS: Final = frozenset({"liftwing"})
_MAX_LANG_LEN: Final = 8  # BCP-47 primary tags ("en", "pt-br") — not a free-text field


class LlmParseError(Exception):
    """The /llm arguments could not be parsed; the message is user-facing."""


def parse_llm_args(text: str, base: LlmSettings, max_tokens_ceiling: int) -> LlmSettings:
    """Apply ``key:value`` overrides from ``text`` on top of ``base``."""
    settings = base
    tokens = text.split()
    if not tokens:
        msg = "Give at least one setting, e.g. /llm set lang:fr model:large temp:0.4"
        raise LlmParseError(msg)
    for token in tokens:
        key, sep, value = token.partition(":")
        if not sep or not value:
            msg = (
                f"Expected key:value, got {token!r} (keys: platform, model, lang, temp, max_tokens)"
            )
            raise LlmParseError(msg)
        settings = _apply(settings, key, value, max_tokens_ceiling)
    return settings


def describe_llm(settings: LlmSettings) -> str:
    """One line for /llm show."""
    return (
        f"platform:{settings.platform} model:{settings.model} lang:{settings.lang} "
        f"temp:{settings.temperature:g} max_tokens:{settings.max_tokens}"
    )


def dumps_llm(settings: LlmSettings) -> str:
    """Serialize a scope's settings for the profile row."""
    return json.dumps(
        {
            "platform": settings.platform,
            "model": settings.model,
            "lang": settings.lang,
            "temperature": settings.temperature,
            "max_tokens": settings.max_tokens,
        },
        separators=(",", ":"),
    )


def loads_llm(text: str | None) -> LlmSettings | None:
    """Rebuild stored settings (``None``/empty → no scope override).

    A stored row is re-validated, not trusted: it may predate a rule
    change or have been tampered with directly in the DB. Any field that
    would break an invariant is coerced back to a safe default — ``lang``
    and ``model`` especially, since they flow into the prompt and the
    (unsanitized) wiki footer.
    """
    if not text:
        return None
    data = json.loads(text)
    platform = data.get("platform", "liftwing")
    model = data.get("model", "default")
    lang = str(data.get("lang", "en")).lower()
    temperature = float(data.get("temperature", 0.2))
    max_tokens = int(data.get("max_tokens", 1024))
    return LlmSettings(
        platform=platform if platform in _PLATFORMS else "liftwing",
        model=model if model in _MODEL_ALIASES else "default",
        lang=lang if valid_lang(lang) else "en",
        temperature=min(max(temperature, 0.0), 1.0),
        max_tokens=max(1, max_tokens),
    )


def valid_lang(lang: str) -> bool:
    """Whether ``lang`` is a short alphabetic language code (not free text)."""
    return lang.replace("-", "").isalpha() and len(lang) <= _MAX_LANG_LEN


def _apply(settings: LlmSettings, key: str, value: str, ceiling: int) -> LlmSettings:
    if key == "platform":
        if value not in _PLATFORMS:
            msg = f"Unknown platform {value!r}. Available: {', '.join(sorted(_PLATFORMS))}"
            raise LlmParseError(msg)
        return replace(settings, platform=value)
    if key == "model":
        if value not in _MODEL_ALIASES:
            msg = f"Model must be one of: {', '.join(sorted(_MODEL_ALIASES))}"
            raise LlmParseError(msg)
        return replace(settings, model=value)
    if key == "lang":
        cleaned = value.lower()
        if not valid_lang(cleaned):
            msg = "lang must be a short language code, e.g. lang:en or lang:fr"
            raise LlmParseError(msg)
        return replace(settings, lang=cleaned)
    if key == "temp":
        try:
            temperature = float(value)
        except ValueError as error:
            msg = "temp must be a number between 0 and 1"
            raise LlmParseError(msg) from error
        if not 0.0 <= temperature <= 1.0:
            msg = "temp must be between 0 and 1"
            raise LlmParseError(msg)
        return replace(settings, temperature=temperature)
    if key == "max_tokens":
        try:
            max_tokens = int(value)
        except ValueError as error:
            msg = "max_tokens must be a positive integer"
            raise LlmParseError(msg) from error
        if not 0 < max_tokens <= ceiling:
            msg = f"max_tokens must be between 1 and {ceiling}"
            raise LlmParseError(msg)
        return replace(settings, max_tokens=max_tokens)
    msg = f"Unknown setting {key!r} (keys: platform, model, lang, temp, max_tokens)"
    raise LlmParseError(msg)
