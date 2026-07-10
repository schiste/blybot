"""Service layer: use-cases orchestrating the domain through ports.

Services depend on :mod:`blybot.domain` only. They receive their
collaborators (publisher, sanitizer, clock, ...) by constructor
injection, so every service is unit-testable with in-memory fakes and no
network.
"""
