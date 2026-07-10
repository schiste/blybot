# Contributing to Blybot

Thanks for helping! Blybot welcomes pull requests. This document explains the
workflow, the quality gates, and the design rules the codebase holds itself to.

## Ground rules

Blybot's whole value is its privacy posture. Every change is reviewed against
these invariants — they are non-negotiable and most are enforced by tests:

1. **No Telegram identifier may cross into the domain or service layers.**
   Handlers extract message *text* only. If your change needs a user ID past
   the adapter boundary, the design is wrong — bring it to an issue first.
2. **Nothing identifier-bearing is written to disk or logs.** Logs carry event
   types, outcomes, and error codes; never message content, chat ids, or names.
3. **Pseudonyms stay random and input-free.** `PseudonymFactory.mint()` takes
   no arguments and draws from `secrets`. Both are asserted by tests.
4. **All user text passes the sanitizer before any wiki write.** New publish
   paths must go through a `Sanitizer` port.
5. **Privacy mode stays on.** No feature may require disabling it.

## Setup

```sh
git clone <your fork>
cd blybot
make install   # uv sync
make hooks     # installs pre-commit, commit-msg AND pre-push hooks
```

`make hooks` is not optional — CI runs the same gates, so installing them
locally just saves you a round-trip.

## Quality gates

| Stage | What runs | Why |
|---|---|---|
| `pre-commit` | ruff (lint+format), gitleaks, codespell, hygiene hooks | fast, per-file feedback |
| `commit-msg` | conventional commits check | readable history, mechanical changelogs |
| `pre-push` | `mypy --strict`, `pytest` with **100 % branch coverage** and warnings-as-errors | nothing broken leaves your machine |
| CI | all of the above, tests on Python 3.11–3.13 | the same bar for every PR |

Run everything at once with `make check`.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
`feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `refactor: ...`, `chore: ...`.

## Design rules

- **Layering** (enforced by `tests/test_architecture.py`):
  `domain` imports nothing from the app and no I/O libraries; `services`
  import `domain` only; `adapters` are the only place `python-telegram-bot`
  or `mwclient` may appear; `__main__.py` is the sole composition root.
- **Dependency injection over globals.** Services receive collaborators via
  constructors, typed as the Protocols in `blybot/domain/ports.py`. New
  external capability = new port + adapter, not a direct import.
- **DRY at the right altitude.** Extract shared behavior into the domain, not
  into helper grab-bags. Two adapters sharing code is a smell that a port is
  missing.
- **Every behavior change comes with a test.** Spec requirements map to test
  files (R7 → `test_sanitizer.py`, R4/§10 → `test_sessions.py`, ...). New
  requirements should keep that traceability.
- **No new dependencies without discussion.** The runtime footprint is
  deliberately tiny; open an issue before adding one.

## Pull request checklist

- [ ] `make check` passes locally
- [ ] tests added or updated for the behavior change
- [ ] no identifier or content leaks into logs, disk, or the wiki
- [ ] docs updated (README / docstrings) if behavior or config changed
- [ ] scope: one logical change per PR

## Where help is wanted

- **N1 consent-confirm flow:** the `CONSENT_MODE=confirm` hook exists (see
  `_parse_consent_mode` in `config.py` and the marked branch in
  `GroupHandlers.on_log`) but the DM-confirmation flow itself is unbuilt.
- **OAuth (owner-only consumer)** as the P1 upgrade over BotPassword in
  `adapters/mediawiki/publisher.py`.
- Anything labeled `good first issue`.

## Reporting security or privacy issues

Please do **not** open a public issue for vulnerabilities or privacy leaks —
see [SECURITY.md](SECURITY.md).
