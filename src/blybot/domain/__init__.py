"""Domain layer: pure business logic.

Modules in this package MUST NOT import from ``blybot.adapters`` or any
third-party transport/API library. They operate on plain values and the
value objects defined in :mod:`blybot.domain.models`.

This boundary is what makes the privacy guarantees (spec R1, R6)
structural: Telegram identifiers cannot reach publication logic because
no type in this layer can carry them.
"""
