"""Shared, parametrized port contracts every implementation must satisfy.

Each module in this package asserts a domain PORT'S semantics against every
available implementation of it — the in-memory fake AND the real ToolsDb
adapter (backed by its SQL-level fake runner). A single list of impl builders
per port drives the parametrization, so making a future implementation prove
itself is one new list entry, and any implementation that drifts from the
port's contract fails the build.
"""
