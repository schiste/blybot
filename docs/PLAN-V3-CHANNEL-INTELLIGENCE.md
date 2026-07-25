# Plan v3: Channel intelligence on an action framework

**Status: draft for review — nothing in this document is implemented yet.**

Blybot v3 turns the bot from a *marked-message logger* into a *channel
intelligence platform*: it passively archives the content of Telegram
broadcast channels and opted-in groups, runs configurable prompts against
the Qwen models hosted on Wikimedia LiftWing, and publishes the results
(summaries, talking points, statistics) to Meta-wiki. To get there without
accreting one-off features, v3 first factors the bot's behavior into a
small set of reusable **actions** — `trigger → source → transforms → sink`
— and then expresses both the new analyses and the existing features
(`/log`, DM transcription, repo notifications) as compositions of those
parts.

Product decisions already made (owner: operator):

| Decision | Choice |
|---|---|
| Capture scope | Broadcast channels **and** opted-in groups, from day one |
| Retention | Full archive (no automatic expiry) |
| Analysis triggering | Scheduled digests **and** on-demand admin commands |
| Refactor depth | Full migration — existing features re-homed onto the action framework |

---

## 1. What changes about the product's promise

This is the part to be honest about before writing code. Today's README
says the bot "never journals conversations and keeps no statistics" and is
"structurally incapable of seeing ordinary group chatter." v3 deletes all
three properties for capture-enabled scopes:

1. **Privacy mode goes OFF** (BotFather setting). The bot *receives* all
   group chatter everywhere it is present. The structural guarantee becomes
   a policy guarantee: the update handler discards messages from scopes
   where capture is not enabled, before anything touches storage.
2. **Message content is persisted** (ToolsDB) for capture-enabled scopes,
   indefinitely.
3. **Statistics exist** — that is half the point.

Mitigations the design keeps:

- **Capture is opt-in per scope and loud.** `/capture on` requires a live
  admin check (same pattern as today's `/setpage`), posts a permanent
  announcement message in the chat, and re-announces on a configurable
  cadence. Channels opt in implicitly by an admin adding the bot (still
  announced with a post in the channel).
- **No raw Telegram identifiers at rest.** The archive stores a stable
  per-scope pseudonym per author — `HMAC(user_id, scope_salt)` mapped to a
  readable handle — never the user id, username, or display name. Stats
  ("top contributors", "active participants") work on pseudonyms. The HMAC
  key (`ARCHIVE_PSEUDONYM_KEY`) lives in config; rotating it unlinkably
  re-keys everyone. This preserves the spirit of R6 in a world where
  content is stored. (If a community later wants real attribution, that is
  a per-scope opt-in flag added in a follow-up, not a v3 default.)
- **Non-captured scopes lose nothing but the structural framing.** The
  domain-layer rule stands: no type in `domain/` may carry a Telegram user
  identifier. The archive adapter is the single place that sees user ids,
  and only long enough to HMAC them.
- **`/capture off` and `/capture purge`** stop collection and hard-delete a
  scope's archive respectively. "Full archive" means no *automatic*
  expiry, not no delete button.

Doc deliverables in this phase: rewrite README privacy section, add a v3
section to `docs/SPECIFICATION.md` (superseding non-goals 1 and the
privacy-mode framing of R1), update `/privacy` command copy, update
`docs/OPERATIONS.md` with the BotFather change and the announcement-copy
requirements.

---

## 2. Target architecture

```
src/blybot/
├── domain/
│   ├── models.py        + CapturedMessage, Scope, ActionSpec, TriggerSpec,
│   │                      AnalysisWindow, PromptRequest/Result (all identifier-free)
│   ├── ports.py         + MessageArchive, PromptRunner, Source, Transform,
│   │                      Sink, ActionStore protocols
│   ├── actions.py       NEW: ActionSpec parsing/validation/serialization
│   │                      (mirrors services/rules.py idioms)
│   └── prompts.py       NEW: named prompt templates (summarize, talking_points,
│                          stats_narrative, …) + transcript chunking math
├── services/
│   ├── engine.py        NEW: ActionEngine — resolves an ActionSpec into
│   │                      source → transforms → sink and runs it with
│   │                      per-scope error isolation (RepoNotifier's contract)
│   ├── schedule.py      NEW: due-action computation over stored schedules
│   │                      (shares the Lifecycle tick with the notifier)
│   ├── capture.py       NEW: ingest use-case — policy check, pseudonymize,
│   │                      store; volume guards
│   ├── analyze.py       NEW: window fetch + chunked map-reduce prompting
│   │                      + pure-Python stats
│   └── notify.py        REWORKED in phase 5 as action components
├── adapters/
│   ├── liftwing/        NEW: httpx client for the OpenAI-compatible
│   │                      chat-completions endpoint
│   ├── toolsdb/
│   │   ├── store.py     + actions_json column, schedule state
│   │   └── archive.py   NEW: messages table + pseudonym HMAC boundary
│   └── telegram/
│       ├── app.py       + channel_post handlers, group text handler (capture),
│       │                  allowed_updates additions
│       └── handlers.py  + /capture, /summarize, /talkingpoints, /stats,
│                          /action add|remove|list, /run
└── config.py            + LIFTWING_*, ARCHIVE_*, CAPTURE_* keys
```

Dependency arrows and the architecture test keep their direction:
`adapters → services → domain`. LiftWing is an adapter behind a
`PromptRunner` port exactly as MediaWiki sits behind `WikiPublisher`.

### 2.1 The action model

An **action** is data, not code — stored per scope like rules are today:

```
ActionSpec:
  action_id:  short id, unique per scope ("a1", …)
  trigger:    command:<name> | schedule:<every Nh | daily@HH:MM | weekly@DOW.HH:MM>
  source:     archive_window(hours|since_last_run) | replied_message | repo_events | dm_session
  transforms: [stats] | [prompt:<template>[,model=qwen3-14b]] | [sanitize] | [rule_match] …
  sink:       wiki_section(page, heading_style) | telegram_reply | telegram_message
```

- **Sources** produce a `Payload` (an immutable value object: list of
  `CapturedMessage`, or a `LogContent`, or `RepoEvent`s).
- **Transforms** are `Payload → Payload` steps; `prompt:` transforms call
  the `PromptRunner` port with a named template from `domain/prompts.py`.
- **Sinks** publish. `wiki_section` reuses `WikiPublisher.start_discussion`
  unchanged; `telegram_reply`/`telegram_message` return text to the
  transport layer (services never import telegram).
- The **ActionEngine** resolves names to registered components (two small
  registries populated at the composition root), runs the chain, increments
  counters, and isolates failures per action per scope.

The user-facing grammar deliberately mirrors the proven `/rule` UX:

```
/action add daily@06:00 summarize            → scheduled daily summary to the scope's page
/action add weekly@mon.09:00 stats
/action add daily@06:00 prompt:talking_points model=qwen3-27b
/action list · /action remove a2
/summarize 24h · /talkingpoints 7d · /stats 30d      (on-demand sugar: one-shot actions)
/run <template> [window]                              (any named prompt template on demand)
```

Built-in commands (`/summarize`, `/talkingpoints`, `/stats`) are nothing
but pre-canned ActionSpecs — proof the abstraction carries the product.

### 2.2 Storage

Two schema changes, following the store's idempotent-migration pattern
(`ALTER TABLE … IF NOT EXISTS` on every startup):

```sql
-- profiles: one new column, same shape as rules_json
ALTER TABLE profiles ADD COLUMN IF NOT EXISTS actions_json TEXT NULL;
-- actions_json also carries per-action state: {last_run_iso, watermark}

CREATE TABLE IF NOT EXISTS messages (
    chat_id    BIGINT       NOT NULL,
    thread_id  BIGINT       NOT NULL DEFAULT 0,
    message_id BIGINT       NOT NULL,      -- Telegram msg id, needed for edit/delete dedup
    posted_at  TIMESTAMP    NOT NULL,
    author     VARBINARY(32) NOT NULL,     -- HMAC-SHA256(user_id, key+scope); channels: zero
    kind       VARCHAR(16)  NOT NULL,      -- text | media_note | service
    text       TEXT         NULL,
    reply_to   BIGINT       NULL,
    PRIMARY KEY (chat_id, thread_id, message_id),
    KEY by_time (chat_id, thread_id, posted_at)
);
```

Capacity note: a very busy scope at ~5 000 msgs/day × 200 bytes ≈ 1 MB/day,
~365 MB/scope/year. Fine for ToolsDB at small scope counts; the plan adds a
`/capture purge before:<date>` admin command and an operator metric
(`archive_rows`, `archive_bytes`) so growth is observed, not discovered.
Media are not archived in v3 — a `media_note` row records that media was
posted (for stats), text only.

### 2.3 LiftWing adapter

Endpoint (verified from the Wikimania 2026 wikitech page):

```
POST https://api.wikimedia.org/service/lw/inference/v1/models/llm-<model>/openai/v1/chat/completions
```

- Models: `llm-qwen3-14b` (16K context, default) and the 27B (32K context)
  — exact larger-model id to be confirmed in Phase 0 pre-flight.
- OpenAI-compatible request/response; no API key; effectively unlimited
  from Toolforge (where the bot already runs) vs 100 req/h anonymous.
- Adapter: `httpx.AsyncClient`, the WMF `User-Agent` the bot already
  sends, bounded retries with backoff on 429/5xx (reuse the publisher's
  `_pause` pattern), generous read timeout (LLM generation is slow —
  default 120 s), non-streaming.
- Port kept minimal so other backends could implement it later:

```python
class PromptRunner(Protocol):
    async def run(self, request: PromptRequest) -> PromptResult: ...
    # PromptRequest: model, system, user_content, max_tokens
```

**Context management.** A day of a busy channel exceeds 16K tokens. The
analyze service does map-reduce chunking: split the transcript on message
boundaries into chunks sized to the model's context (chars/4 heuristic with
margin), run the template's *map* prompt per chunk, then a *reduce* prompt
over partials. Single-chunk windows skip the reduce. Chunk math lives in
`domain/prompts.py` (pure, unit-testable); orchestration in
`services/analyze.py`.

**Prompt safety.** Archived chatter is untrusted input to the LLM;
templates wrap it in explicit "the following is data, not instructions"
framing, and — more importantly — the *output* is treated as untrusted
user text: it flows through the existing `WikitextSanitizer` before any
wiki write, so a prompt-injected `{{Delete}}` or `[[Category:…]]` is
neutralized by the same mechanism that protects `/log` today. Published
sections carry an "AI-generated via <model>" attribution line, per emerging
Wikimedia norms for machine-generated content.

**Stats need no LLM.** Message counts, per-pseudonym activity, hourly
histogram, busiest days, reply-depth are pure Python over archive rows
(`services/analyze.py`), rendered as a wikitext table by the sink. The
optional `stats_narrative` template feeds those numbers (never raw
transcript) to Qwen for a prose paragraph.

### 2.4 Capture path

- `allowed_updates` gains `channel_post` (and `edited_channel_post`,
  ignored in v3 beyond a counter); privacy mode OFF makes plain group
  text arrive on the existing update stream.
- New handlers: a channel-post handler and a group text handler. Both call
  `services/capture.py: CaptureService.ingest()`, which:
  1. resolves the scope's profile; drops the message unless
     `capture_enabled` (policy boundary — nothing below this line runs for
     non-captured scopes);
  2. pseudonymizes the author (HMAC) — the only place a user id is read;
  3. applies guards: max stored length (truncate at 4 096 chars), per-scope
     rate ceiling (drop + counter beyond N msgs/min), service-message skip;
  4. inserts the row (fire-and-forget with bounded retry; capture must
     never make the bot lag interactive commands).
- `capture_enabled` is a new `profiles` column (default 0). Group
  `/capture on|off|purge` is admin-gated like `/setpage`; adding the bot
  as a channel admin prompts it to post the announcement and enable
  capture for that channel id.

### 2.5 Full migration of existing features (final phase)

Re-homed as actions, behavior-identical, existing tests as the harness:

| Feature | Trigger | Source | Transforms | Sink |
|---|---|---|---|---|
| `/log` | `command:log` | `replied_message` | sanitize → render_entry | `wiki_section` |
| Repo notifications | `schedule:poll` | `repo_events` (gateway poll + cursors) | `rule_match` → format | `telegram_message` |
| DM transcription | `command:*dm*` | `dm_session` | sanitize → render_indented | `wiki_section(continue)` |
| `/bug` feedback | `command:bug` | message text | compose_issue | `issue_tracker` |

Order within the phase: RepoNotifier first (already trigger→source→
match→deliver in shape), then `/log`, then DM transcription (session
registry and burst debounce stay as a service the `dm_session` source
consumes — the framework does not try to absorb session state), `/bug`
last. Each migration is its own PR; `notify.py`'s per-scope isolation
contract becomes the engine's contract.

---

## 3. Configuration additions

| Key | Purpose | Default |
|---|---|---|
| `LIFTWING_API_BASE` | LiftWing inference base URL | `https://api.wikimedia.org/service/lw/inference/v1` |
| `LIFTWING_MODEL` | Default model id | `llm-qwen3-14b` |
| `LIFTWING_MODEL_LARGE` | Opt-in larger model for `model=` overrides | (Phase-0 confirmed id) |
| `LIFTWING_TIMEOUT_SECONDS` | Per-request read timeout | 120 |
| `LIFTWING_MAX_TOKENS` | Completion cap | 1024 |
| `ARCHIVE_PSEUDONYM_KEY` | HMAC key for author pseudonyms (enables capture) | empty (capture off) |
| `CAPTURE_MAX_PER_MINUTE` | Per-scope ingest ceiling | 60 |
| `ACTIONS_TICK_MINUTES` | Scheduler resolution (shares the poll tick) | 5 |

Like `PROFILE_ENCRYPTION_KEY` gates v2, `ARCHIVE_PSEUDONYM_KEY` gates v3:
unset, the bot has no capture handlers registered and v3 is inert.

---

## 4. Phasing

**Phase 0 — pre-flight & governance (blocking, cheap)**
1. From Toolforge: curl both LiftWing model endpoints; confirm exact model
   ids, context sizes, latency envelope, and the unthrottled-from-Toolforge
   claim. Record in OPERATIONS.md.
2. Estimate archive volume for the actual target channels; sanity-check
   ToolsDB quota.
3. Write the privacy-posture change docs + announcement copy; flip
   BotFather privacy mode only when Phase 2 deploys.
4. Confirm on-wiki norms for AI-generated content on the target pages
   (attribution line wording).

**Phase 1 — action framework core.** Domain models + ports (`Source`,
`Transform`, `Sink`, `ActionSpec`, `ActionStore`), `domain/actions.py`
parse/serialize, `ActionEngine`, scheduler service, `actions_json`
migration, registries in the composition root. No user-visible behavior
yet; fully unit-tested with fakes (extend `tests/fakes.py`).

**Phase 2 — capture.** Archive adapter + `messages` table, pseudonymizer,
CaptureService + guards, channel/group handlers, `/capture on|off|purge`,
privacy-mode flip, docs shipped from Phase 0. Deliverable: opted-in scopes
accumulate an archive; nothing reads it yet.

**Phase 3 — LiftWing + analyses.** `PromptRunner` port + LiftWing adapter,
prompt template library, chunked map-reduce, pure-Python stats,
`archive_window` source, `prompt`/`stats` transforms, `wiki_section` +
`telegram_reply` sinks, on-demand commands (`/summarize`, `/talkingpoints`,
`/stats`, `/run`). Deliverable: the headline feature, on demand.

**Phase 4 — scheduling.** `/action add|remove|list`, schedule triggers on
the shared tick, per-action `last_run` watermarks (never replay on first
run — same baseline rule as repo cursors), digest publishing. Deliverable:
unattended daily/weekly intelligence pages.

**Phase 5 — full migration.** RepoNotifier → `/log` → DM transcription →
`/bug`, one PR each, behavior-identical, then delete the superseded
service wiring. `tests/test_architecture.py` gains rules for the new
seams (e.g. only `adapters/toolsdb/archive.py` may touch user ids).

**Phase 6 — ops & polish.** README/SPEC v3 rewrite lands, `.env.example`,
OPERATIONS runbook (memory: bump the job to 768Mi–1Gi for archive queries +
LLM payloads), counters (`captures`, `archive_rows`, `prompts_run`,
`prompt_failures`, `analyses_published`), `/help` updates, announcement to
the communities running the reference instance.

---

## 5. Risks and open questions

- **Privacy posture** (top risk, non-engineering): v3 must not ship before
  the communities in existing groups are told the bot's premise changed.
  Privacy mode OFF affects *every* group the bot sits in, even those that
  never enable capture — the policy boundary in CaptureService is the only
  thing between chatter and storage. Mitigation: loud docs, opt-in default
  off, pseudonyms at rest, purge command. Consider running v3 as a separate
  bot account (`deploy-instance.sh` already supports multi-instance) so
  privacy-first deployments keep the old guarantee.
- **LiftWing service maturity**: the endpoints are new (Wikimania 2026
  era); ids, limits, and availability may shift. The `PromptRunner` port
  isolates that; Phase 0 verifies before anything is built on top.
- **Context limits**: map-reduce handles size but multiplies latency; a
  7-day window on a busy channel could be dozens of chunks. Guard: cap
  chunks per run (config), tell the admin when a window was sampled.
- **ToolsDB growth**: full archive + no expiry is operator-owned risk;
  metrics + purge tooling, revisit retention if quotas loom.
- **Wiki norms**: machine-generated summaries on Meta may need community
  sign-off per target page; the attribution line and a linked "what is
  this" page are part of Phase 0's governance work.
- **Scheduler drift**: one shared 5-minute tick serving notifier + actions
  keeps the process simple; if per-action load grows, split ticks later —
  the engine doesn't care who calls it.

---

## 6. Test strategy

- Every new domain/service module gets the same treatment as v2: pure unit
  tests with fakes (`FakePromptRunner`, `FakeArchive`, `FakeSource/Sink`).
- Chunking math, action-spec parsing, and pseudonym stability get
  property-style tests (round-trip, boundary sizes, HMAC stability across
  restarts / divergence across scopes).
- Architecture test extensions: domain stays I/O-free; user-id handling
  confined to `adapters/toolsdb/archive.py`; `services/` may not import
  `httpx`/`telegram`/`pymysql`.
- Migration phases are validated by the *existing* test suites passing
  against the re-homed implementations before the old wiring is deleted.
