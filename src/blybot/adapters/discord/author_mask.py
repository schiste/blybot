"""Discord author pseudonymization at the capture boundary (spec R6).

The Discord counterpart of the Telegram ``HmacAuthorMasker``
(``adapters/telegram/capture.py``): the same contract — HMAC-SHA256 of an
operator key over ``scope‖user_id`` truncated to a short label — so a
captured Discord author is reduced to a stable per-scope pseudonym
*here*, at the adapter edge, before anything crosses into the neutral
services layer.

It is mirrored rather than shared because the Telegram masker's home
module imports ``telegram``; the architecture guard forbids the Discord
package touching another platform's SDK. The label algorithm is
identical, so a rotation of ``ARCHIVE_PSEUDONYM_KEY`` re-keys every future
label on both platforms the same way; archived rows keep theirs.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Final

_LABEL_CHARS: Final = 12  # 48 bits of the HMAC: no realistic collisions per scope


@dataclass(frozen=True)
class DiscordAuthorMasker:
    """Derives stable per-scope pseudonym labels from an operator key.

    The label is HMAC-SHA256(key, ``channel‖thread‖author``) truncated for
    readability: stable within a scope (so activity stats work), unlinkable
    across scopes, and unlinkable to the account without the key.
    """

    key: str

    def mask(self, channel_id: int, thread_id: int, author_id: int) -> str:
        """Return the pseudonym label for this scope's author reference."""
        payload = f"{channel_id}:{thread_id}:{author_id}".encode()
        digest = hmac.new(self.key.encode(), payload, hashlib.sha256)
        return digest.hexdigest()[:_LABEL_CHARS]
