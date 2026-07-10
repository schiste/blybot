# Blybot

**A privacy-first Telegram bot that publishes explicitly selected messages to a
[Meta-wiki](https://meta.wikimedia.org/) page, anonymously.**

Blybot is a small, single-purpose bot in the spirit of the old IRC utility
bots, rebuilt around a privacy-first premise. It never journals conversations
and keeps no statistics. It only ever ingests two things:

1. a group message a user **explicitly marks** by replying with `/log` —
   published to a configured Meta talk page with **no attribution**, one
   section per entry;
2. messages sent to it in a **private chat** — transcribed to Meta as an
   anonymized talk-page discussion (one section per session, each message
   indented one level deeper) under a per-session pseudonym that is never
   persisted.

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

**Feature-complete for v1** (spec Phases 1–3): the group `/log` flow with
confirmation and consent policy, greet-on-entry, DM transcription with
per-session talk-page sections and burst coalescing, the newcomer deep-link
welcome,
rate limiting, and a maxlag-aware MediaWiki publisher. What remains before
going live is Phase 0 operations — BotFather registration (privacy mode ON),
the on-wiki BotPassword, target pages, and the Toolforge outbound check —
plus the deferred N1 consent-confirm flow. See
[docs/SPECIFICATION.md](docs/SPECIFICATION.md) for the full product spec.

## Architecture

```
src/blybot/
├── domain/       pure business logic — no I/O, no third-party imports
│   ├── models.py       identifier-free value objects, ConsentMode
│   ├── ports.py        Protocols: WikiPublisher, Sanitizer, PseudonymFactory, Clock
│   ├── sanitizer.py    wikitext neutralization (entity encoding)
│   └── pseudonym.py    CSPRNG pseudonym minting
├── services/     use-cases, depend on domain ports only
│   ├── publish.py      /log → one talk-page section per entry
│   ├── transcribe.py   DM sessions: one section each, indented discussion
│   ├── sessions.py     volatile DM session registry (TTL, peek, reset, sweep)
│   └── policy.py       group allowlist + supergroup migration, /log throttle
├── adapters/     the only layer allowed to touch I/O libraries
│   ├── telegram/       handlers (the anonymity boundary) + polling bootstrap
│   ├── mediawiki/      async discussion publisher: sections, maxlag, assert=user
│   └── system.py       wall clock
├── observability.py    identifier-free event logging and counters
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
`0600`, never in this repository. `run.sh` refuses to start if the config
file is not `chmod 600`.

Full runbook — including **running several instances** (different bot,
group, and wiki pages) off one checkout with `deploy-instance.sh` — in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

Pre-flight checklist (spec Phase 0) before the first launch:

1. Confirm outbound HTTPS from the tool account to `api.telegram.org`
   (decides long polling vs. the webhook fallback).
2. Register the bot with BotFather and confirm **privacy mode is ON**.
3. Create the on-wiki bot account, issue a least-privilege BotPassword,
   and ideally request the bot flag.
4. Create the `LOG_TARGET_PAGE` and `DM_TARGET_BASE` talk pages and confirm
   the account can edit them (every log/session becomes a section on them).
5. For reliable newcomer detection (`chat_member` updates), make the bot a
   group admin; without admin, greet-on-entry still works but silent link
   joins may be missed.

## License

[AGPL-3.0-or-later](LICENSE). Blybot is movement infrastructure: if you run a
modified version as a service, the license obliges you to offer its source to
your users.
