"""Adapter layer: implementations of the domain ports against real I/O.

Adapters are the only modules allowed to import transport and API
libraries (``python-telegram-bot``, ``mwclient``). Nothing in
``blybot.domain`` or ``blybot.services`` may import from this package.
"""
