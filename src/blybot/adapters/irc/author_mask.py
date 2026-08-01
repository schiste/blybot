"""IRC author pseudonymization at the capture boundary (spec R6).

Same contract as the Telegram and Discord maskers: HMAC-SHA256 of the
operator key over ``scope‖author``, truncated to a short stable label, so
a captured author is reduced to a pseudonym *here* before anything
crosses into the neutral services.

Mirrored rather than shared because each platform's masker lives beside
its own adapter and the architecture guard forbids one platform package
importing another's.

The identity hashed is the **nick**, which is weaker than a Telegram or
Discord account id: nicks are not durable, so the same person appears as
different pseudonyms after a rename, and a reused nick could inherit
someone else's label. That is inherent to IRC, and it is why capture
consent is announced per channel (#21).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Final

_LABEL_CHARS: Final = 12  # 48 bits of the HMAC: no realistic collisions per scope


@dataclass(frozen=True)
class IrcAuthorMasker:
    """Derives stable per-channel pseudonym labels from an operator key."""

    key: str

    def mask(self, channel: str, nick: str) -> str:
        """Return the pseudonym label for this channel's author reference."""
        payload = f"{channel.lower()}:{nick.lower()}".encode()
        digest = hmac.new(self.key.encode(), payload, hashlib.sha256)
        return digest.hexdigest()[:_LABEL_CHARS]
