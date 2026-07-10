"""Meta-wiki publisher (spec sections 8-9, R8). Implementation lands in Phase 1.

Contract for the implementation (tracked in the Phase 1 milestone):

* ``action=edit`` with ``appendtext`` — server-side append, conflict-free;
* descriptive ``User-Agent`` from config, per WMF API etiquette;
* ``maxlag=5`` honored, with bounded exponential backoff on lag/5xx;
* ``assert=user`` so a dropped login fails loudly instead of editing
  logged-out;
* edit summaries passed through verbatim (already generic per config).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MetaWikiPublisher:
    """:class:`blybot.domain.ports.WikiPublisher` backed by the MediaWiki API."""

    api_url: str
    username: str
    botpassword: str
    user_agent: str

    async def append(self, page: str, text: str, summary: str) -> None:
        """Append ``text`` to ``page``. Not yet implemented (Phase 1)."""
        raise NotImplementedError
