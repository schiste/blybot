# The action framework

Blybot v3 factors bot behavior into **actions**: small, reusable pipelines
of the shape

```
trigger  →  source  →  transform(s)  →  sink
```

An action is *data*, not code — an
[`ActionSpec`](../src/blybot/domain/models.py) stored per
(group, topic) scope — while the named components it references are
registered once at the composition root. Adding a new capability to the
bot usually means writing one new component and composing it, not a new
feature module.

Status: the framework core shipped in Phase 1 of the
[v3 plan](PLAN-V3-CHANNEL-INTELLIGENCE.md) (this document). Built-in
sources, transforms, and sinks arrive with the capture and analysis
phases; until then the registries are empty and no user-facing command
creates actions yet.

## Concepts

| Piece | Contract | Example (planned) |
|---|---|---|
| **Trigger** | when the action runs: a chat command, or a `Schedule` evaluated on the shared background tick | `daily@06:00`, `/summarize` |
| **Source** | produces the run's initial payload; `None` = nothing to do, run ends quietly | `archive_window` |
| **Transform** | payload → payload; each occurrence carries its own parameters; `None` ends the run quietly | `prompt` (LLM template), `stats`, `sanitize` |
| **Sink** | publishes the final payload; returns `OutboundMessage`s for the transport to send, or nothing when it wrote externally | `wiki_section`, `telegram_reply` |

The engine (`services/engine.py`) resolves every component name *before*
any I/O — a spec naming an unregistered component fails fast with an
admin-safe `ActionError` — then threads the payload through the chain.
The scheduler (`services/schedule.py`) selects due schedule-triggered
actions each tick with the same two-level error isolation the repo
notifier pioneered: a broken scope never blocks other scopes, and a
failing action never blocks its scope's other actions. A failed run
waits for its next slot; it is not retried every tick.

## Grammar

`/action add` payloads (parsed by `services/actions.py`):

```
<schedule> <recipe> [key=value ...]

schedule:  every:<N>h | daily@HH:MM | weekly@<dow>.HH:MM     (all UTC)
recipe:    summarize | talking_points | stats | prompt:<template>
params:    window=<N>h|<N>d   → source
           page=<title>       → sink
           model=default|large, lang=<code>, temp=<0..1> → prompt transform
```

Examples:

```
/action add daily@06:00 summarize
/action add weekly@mon.09:00 stats page=Meta:Chat_stats
/action add daily@18:00 prompt:decision_log model=large lang=fr
```

Parameters route to the step that understands them, so one grammar
serves every recipe. Each scope holds at most 20 actions
(`MAX_ACTIONS`), stored as one JSON document in the profile row's
`actions_json` column — the same pattern as the rules engine's
`rules_json`.

## Scheduling semantics

- All times are UTC; `Schedule.is_due` is pure math in the domain layer.
- `daily`/`weekly` fire once per slot. A slot missed during downtime is
  made up exactly once at the next tick, never replayed per-slot.
- A new action's `last_run` is primed at creation, so it never fires for
  slots that predate it. A stored action *without* a `last_run` (older
  row, hand-edited state) is baselined — stamped, skipped, and run at
  its next slot — the same never-replay rule the repo poll cursors
  follow.
- `last_run` is stamped when a run is *attempted*, before its outcome is
  known: a permanently failing action costs one try per slot, not one
  per tick.

## Adding a new component

1. **Pick the seam.** New way to obtain data → `Source`. New processing
   step → `Transform`. New destination → `Sink`. The protocols live in
   `domain/ports.py`; implement them in `services/` (pure orchestration)
   or `adapters/` (anything touching I/O libraries), per the
   architecture rules.
2. **Honor the payload contract.** Payloads are immutable values; a
   component that receives a payload type it cannot handle should raise
   `ActionError` with an admin-readable message. Return `None` for
   "nothing to do" — never publish an empty artifact.
3. **Register it** in the composition root (`__main__.py`) under a
   short snake_case name. The name is what specs reference; treat it as
   a public, stable identifier.
4. **Expose it in the grammar** if admins should compose it directly:
   add a recipe or parameter route in `services/actions.py`.
5. **Test it** with the fakes in `tests/fakes.py` (`FakeSource`,
   `SuffixTransform`, `FakeSink`, `InMemoryActions`) and add the new
   name to the grammar tests when step 4 applies.

## Adding a new *action* (no code)

Once components exist, a new scheduled behavior is pure configuration:
`/action add <schedule> <recipe> [params]` from an admin in the target
chat. The framework exists precisely so that "summarize this channel
weekly in French to page X" is a chat command, not a pull request.
