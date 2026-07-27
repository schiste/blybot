"""Discord ⇄ :class:`Scope` edge — the int64-snowflake side of PR-2's keys.

The mirror of the Telegram adapter's ``_common`` helpers. Inbound, a
Discord ``(channel_id, thread_id)`` pair becomes a ``Scope`` via
:func:`scope_of` / :func:`dm_scope`; outbound, :func:`discord_target`
turns a ``Scope`` back into the pair the transport sends to. Everything
past this boundary speaks ``Scope`` only.

Discord ids are int64 snowflakes; a ``Scope`` part is an opaque string
(PR-2's DB is string-keyed), so each id is stored as its decimal string.
A thread is itself a channel with its own snowflake, so an in-thread
address carries the thread id in ``Scope.thread`` and the parent channel
id in ``Scope.channel``; a plain channel leaves ``thread`` empty.
"""

from __future__ import annotations

from typing import Final

from blybot.domain.models import Scope

PLATFORM: Final = "discord"


def scope_of(channel_id: int, thread_id: int | None = None) -> Scope:
    """Build the :class:`Scope` addressing a channel (and optional thread)."""
    return Scope(PLATFORM, str(channel_id), str(thread_id) if thread_id else "")


def dm_scope(dm_channel_id: int) -> Scope:
    """A :class:`Scope` addressing a user's DM channel.

    Unlike Telegram (where the DM handle equals the user id), Discord's DM
    channel has its own snowflake distinct from the user id; that channel
    id is the durable per-user delivery handle a subscription remembers.
    """
    return Scope(PLATFORM, str(dm_channel_id))


def discord_target(scope: Scope) -> tuple[int, int | None]:
    """Convert a :class:`Scope` back into a Discord ``(channel_id, thread_id)``.

    ``thread_id`` is ``None`` for a plain-channel scope. The transport
    sends to the thread when present (a thread is a distinct sendable
    channel), else to the parent channel.
    """
    return int(scope.channel), int(scope.thread) if scope.thread else None
