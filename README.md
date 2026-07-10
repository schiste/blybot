# Blybot

**A privacy-first Telegram bot that publishes explicitly selected messages to a
[Meta-wiki](https://meta.wikimedia.org/) page, anonymously.**

Blybot is a small, single-purpose bot in the spirit of the old IRC utility
bots, rebuilt around a privacy-first premise. It never journals conversations
and keeps no statistics. It only ever ingests two things:

1. a group message a user **explicitly marks** by replying with `/log` —
   published to a configured Meta page with **no attribution**;
2. messages sent to it in a **private chat** — transcribed to Meta as an
   anonymized discussion under a per-session pseudonym that is never persisted.

The bot is *structurally* incapable of seeing ordinary group chatter: it runs
with Telegram's privacy mode **on**, so non-command messages are never even
delivered to it.

## Privacy guarantees

- **No passive collection.** Only `/log`-marked messages and DMs are processed.
- **No identifiers at rest.** No Telegram user ID, username, or display name is
  ever written to the wiki or to disk. Enforced by architecture and by tests.
- **Unlinkable pseudonyms.** DM pseudonyms come from a CSPRNG — never derived
  from a user ID — live only in process memory, and die with the session.
- **Sanitized output.** All published text is neutralized so it cannot alter
  wiki page structure, transclude templates, or categorize pages.

What Blybot *cannot* protect: content that identifies its author in its own
words, and the precise edit times in the wiki's public history. `/log` means
**publish forever** — on-wiki content is permanent in practice.

## Status

Repository scaffolding and core domain logic (sanitizer, sessions,
pseudonyms, publication use-case) with full test coverage. Transport wiring
(Telegram long polling, MediaWiki client) lands in Phase 1 — see
[docs/SPECIFICATION.md](docs/SPECIFICATION.md) for the full product spec and
phasing.

## Architecture

```
src/blybot/
├── domain/       pure business logic — no I/O, no third-party imports
│   ├── models.py       identifier-free value objects
│   ├── ports.py        Protocols: WikiPublisher, Sanitizer, PseudonymFactory, Clock
│   ├── sanitizer.py    wikitext neutralization (entity encoding)
│   └── pseudonym.py    CSPRNG pseudonym minting
├── services/     use-cases, depend on domain ports only
│   ├── publish.py      /log → sanitized append to the Meta log page
│   └── sessions.py     volatile DM session registry (TTL, reset, sweep)
├── adapters/     the only layer allowed to touch I/O libraries
│   ├── telegram/       python-telegram-bot long polling (Phase 1)
│   ├── mediawiki/      appendtext publisher, maxlag-aware (Phase 1)
│   └── system.py       wall clock
├── config.py     env-based configuration, validates without echoing secrets
└── __main__.py   composition root
```

Dependency arrows point inward (`adapters → services → domain`), and a test
suite (`tests/test_architecture.py`) fails any change that breaks the
layering. That layering is also the privacy boundary: no type in the domain
layer can carry a Telegram identifier.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.11.

```sh
make install   # create venv, install everything
make hooks     # install pre-commit + commit-msg + pre-push gates  ← do this once
make check     # lint, typecheck, tests, all hooks — what CI runs
```

Without `uv`: `python -m venv .venv && . .venv/bin/activate && pip install -e . --group dev`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow, gates, and design
rules. PRs are welcome.

## Deployment

Blybot runs as a continuous job on
[Wikimedia Toolforge](https://wikitech.wikimedia.org/wiki/Portal:Toolforge):

```sh
toolforge jobs run blybot --command ./run.sh --image python3.13 --continuous --mem 512Mi
```

Configuration is read from the environment (template in
[.env.example](.env.example)); credentials live in the tool home directory at
`0600`, never in this repository.

## License

[AGPL-3.0-or-later](LICENSE). Blybot is movement infrastructure: if you run a
modified version as a service, the license obliges you to offer its source to
your users.
