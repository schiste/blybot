"""The IRC platform adapter (issue #19).

Mirrors the Discord package's shape: a :mod:`transport` implementing the
neutral ``Transport`` port and publishing ``IRC_CAPABILITIES``, a
:mod:`gateway` translating inbound lines into neutral service calls, plus
the :mod:`scope`, :mod:`author_mask` and :mod:`protocol` edges.

The line protocol is hand-rolled (see :mod:`protocol` for why) so the
package carries no third-party dependency and stays fully typed.
"""

from __future__ import annotations
