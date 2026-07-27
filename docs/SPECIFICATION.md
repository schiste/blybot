# Product Spec: Blybot

**A privacy-first chat bot — Telegram or Discord — that publishes explicitly selected messages to a Meta-wiki page, anonymously.**

| | |
|---|---|
| Status | Draft for review |
| Name | Blybot |
| Platforms | Telegram Bot API or Discord gateway (one per instance, see §22) + MediaWiki API (Meta-wiki) |
| Runtime | Wikimedia Toolforge (continuous job) |
| Version | v1 scope defined below; later phases parked |

---

## 1. Summary

Blybot is a small, single-purpose Telegram bot in the spirit of the old IRC utility bots, rebuilt around a privacy-first premise. It does not journal conversations passively and keeps no statistics. It only ever ingests two things: a message a user explicitly marks with `/log`, and messages a user sends to it in a private chat. Marked messages are published, without attribution, to a configured Meta-wiki page. Private conversations ask the user to choose a shared group, then are transcribed to that group's Meta page as an anonymized discussion using a per-session pseudonym that is never persisted. The bot runs as a continuous job on Toolforge.

The design deliberately keeps the bot structurally incapable of seeing ordinary group chatter. That property is enforced by Telegram's privacy mode, not merely by application logic. (Capture-enabled v3 deployments deliberately trade this structural guarantee for an audited policy boundary — §21.)

---

## 2. Problem statement

Wikimedia community groups increasingly coordinate on Telegram, where useful decisions, quotes, and threads are produced but never make it onto the wikis where the movement's record actually lives. Copying content over by hand is friction nobody sustains, and naive logging bots either over-collect (capturing everyone's chatter) or leak identity in ways that are inappropriate for an open, public record. There is no lightweight, privacy-respecting way to move a specific message, or a deliberate private exchange, onto Meta.

---

## 3. Goals

1. Let a group member publish a specific Telegram message to a predefined Meta page in one gesture, without exposing who wrote it.
2. Let a person hold a private, anonymized exchange with the bot that lands on Meta as a readable discussion under a per-session pseudonym.
3. Collect no ordinary group traffic and persist no personal identifiers at rest.
4. Run reliably and unattended on Toolforge with near-zero state.
5. Produce on-wiki output that is safe (cannot break page structure or self-categorize) and etiquette-compliant with WMF API norms.

## 4. Non-goals (v1)

1. **No passive logging or statistics.** No message history, `/seen`, `/stats`, karma, or top-talkers. Deferred, possibly indefinitely.
2. **No quote store / random-quote features.** The IRC-bot quote database is explicitly out of scope for v1 and parked in Future Considerations.
3. **No media handling.** Photos, files, stickers, and voice notes are not published in v1. Text only.
4. **No multi-bot or cross-chat federation.** One bot instance, a small allowlist of groups.
5. **No stable cross-session identity.** Pseudonyms intentionally do not persist across sessions or restarts. Not a bug, a requirement.
6. **No message deletion tooling.** On-wiki content is permanent by nature; unpublishing is a wiki-side concern, not a bot feature in v1.

---

## 5. Target users

- **Group participant (publisher):** a member of a Wikimedia community Telegram channel who wants to move a specific message onto Meta. Comfortable with wiki norms, cares about not outing colleagues.
- **Private contributor:** someone who wants to contribute a statement or hold an exchange that is recorded on Meta without their Telegram identity attached.
- **Channel steward / operator:** the person who runs the bot on Toolforge, owns the on-wiki account, and decides the target pages and consent policy.

---

## 6. User stories

**Publishing from the group**
- As a group participant, I want to reply to a message with `/log` so that its content is published to our Meta page without anyone's name attached.
- As a group participant, I want the bot to confirm the publication so that I know it worked and can find the entry.

**Private contribution**
- As a private contributor, I want to message the bot directly and have my words recorded on Meta under an anonymous handle so that my contribution is preserved without my identity.
- As a private contributor, I want a fresh anonymous identity each session so that my separate exchanges cannot be trivially linked.

**Newcomers**
- As a newcomer joining the channel, I want a private, optional welcome so that I understand what the bot does before using it.

**Operator**
- As the operator, I want the bot to refuse to collect anything beyond marked messages and DMs so that I can stand behind its privacy claims.
- As the operator, I want user content sanitized before it hits the wiki so that a logged message cannot vandalize or miscategorize the target page.
- As the operator, I want the bot to survive restarts unattended so that I am not babysitting a process.

---

## 7. Requirements

### 7.1 Must-have (P0)

**R1. Privacy mode stays enabled.**
The bot operates with Telegram privacy mode ON (the BotFather default). It must function correctly without ever disabling it. (*Capture-enabled v3 deployments supersede this framing — see §21.*)
- Given the bot is in a group with privacy mode on, when a user replies to a message with `/log`, then the bot receives the command together with the referenced message in `reply_to_message`.
- The bot never receives or processes non-command group messages.

**R2. `/log` reply-to publication.**
- Given a user replies to a text message with `/log`, when the bot receives it, then the referenced message's text is sanitized (R7) and appended to the configured Meta log page with no attribution.
- Given the referenced message has no text (media only), then the bot declines with a short, ephemeral notice and publishes nothing.
- Given publication succeeds, then the bot replies with a brief confirmation (optionally carrying a link to the page or section).

**R3. Greet-on-entry.**
- Given the bot is added to a group, when it joins, then it posts one short greeting message. This both explains `/log` and establishes the bot as the last bot to have spoken, so bare `/log` replies are delivered reliably even before any command addressing.

**R4. DM transcription with per-session pseudonym.**
- Given a user sends the bot a private message, when no active destination exists for that chat, then the bot asks Telegram to let the user choose a shared group, resolves that group's configured page, mints a fresh random pseudonym held only in memory, and starts a session (sessions are created lazily by the first routed message, never by `/start`).
- Given an active session, when the user sends further messages, then each is sanitized (R7) and appended to the session's Meta discussion under the same pseudonym.
- Writes are incremental (per message or per debounced burst), never buffered until session end (R10, R-state).

**R5. Newcomer welcome via deep-link Start.**
- Given a newcomer joins, when the bot detects the join, then it posts a short in-group line with an inline button deep-linking to `https://t.me/<bot>?start=welcome`.
- Given the newcomer taps the button and presses Start, then the bot delivers the welcome privately; a pseudonymous session opens with their first transcribed message.
- The bot must never attempt to DM a user who has not initiated contact (doing so returns 403; see R-edge).

**R6. Anonymization guarantees.**
- No Telegram user ID, username, or display name is ever written to Meta.
- No Telegram identifier is written to disk anywhere by the application, **with one documented carve-out**: opt-in digest subscriptions (§21.1) durably store the subscriber's private chat id, and nothing else about them, solely to deliver the digest they requested. It is confined to the `subscriptions` table and the `domain/subscriptions.py` value object; it never reaches Meta, the pseudonymized content layer, or the identifier-free `ActionContext`. A subscriber's `/unsubscribe` deletes the row.
- Pseudonyms are random (not derived from user ID, to prevent reversal) and exist only in process memory.

**R7. Content sanitization before wiki write.**
User-supplied text must be neutralized so it cannot alter page structure or transclude/categorize:
- Wrap logged content in `<nowiki>...</nowiki>` and additionally neutralize template braces, category links (`[[Category:...]]`), signature tokens (`~~~~`), heading markup, and table/pipe syntax.
- Given a logged message contains `{{Delete}}`, `[[Category:Foo]]`, `== Heading ==`, or `~~~~`, when published, then none of these take effect on the target page.

**R8. WMF API etiquette.**
- Send a descriptive `User-Agent` per WMF policy (tool name, contact/URL).
- Honor `maxlag=5` with retry/backoff.
- Assert the intended account (`assert=user`, ideally a bot-flagged account) and use generic, non-identifying edit summaries.

### 7.2 Nice-to-have (P1)

- **N1. Consent-confirm flow.** Optional mode where, before publishing another person's message, the bot asks the original author to confirm via DM (see Open Questions, the pending governance decision). Implemented as a clearly marked hook in v1, activated later.
- **N2. Burst coalescing.** Debounce rapid DM messages into a single edit to reduce history noise and API load.
- **N3. Per-session Meta anchoring.** Each DM session writes to its own section (heading = pseudonym) or subpage so concurrent sessions never interleave.
- **N4. Rate/abuse throttle.** Per-user and per-group caps on `/log` frequency to prevent flooding the Meta page.

### 7.3 Future considerations (P2)

Design so these remain possible without rework: quote store and `/quote` retrieval, inline-mode quote sharing, `/seen`, lightweight stats, media publication, multi-group operation, and an approval-to-join welcome path (R5 alternative).

---

## 8. Telegram integration details

**Transport.** Long polling via `getUpdates`, using `python-telegram-bot` (async). Polling is outbound-only, so no public endpoint is required. This matters for Toolforge (see 13).

**Privacy mode.** Left ON (OFF only on capture-enabled v3 deployments — §21). Under privacy mode the bot still receives commands addressed to it and replies meant for it, and a `/log` command sent as a reply carries the referenced message in `reply_to_message`. Ordinary chatter is never delivered.

**Command addressing.** When a user selects `/log` from the `/` autocomplete, the client appends `@<bot>`, guaranteeing delivery. Bare `/log` typed by hand only reaches the bot if it was the last bot to speak in the group, which R3 (greet-on-entry) ensures.

**Update subscription.** `allowed_updates` must explicitly include `message`, `my_chat_member`, and `chat_member`. `chat_member` (reliable join detection) is only delivered if the bot is a group admin and the update type is opted into. The lighter `new_chat_members` service message arrives without admin but is unreliable for silent link joins in large supergroups.

**Newcomer DM constraint.** A bot cannot open a private chat with a user who has not contacted it first; attempting to do so returns `403 Forbidden: bot can't initiate conversation with a user`. Hence the deep-link Start pattern in R5. This restriction is treated as aligned with the project's privacy stance, not merely worked around.

**Supergroup migration.** When a group is upgraded to a supergroup, its `chat_id` changes. The bot must handle `migrate_to_chat_id` and update any in-memory group reference, or it will silently fail to post.

**Limits.** Respect ~1 message/second per chat and 20 messages/minute per group. Telegram message bodies cap at 4096 characters.

---

## 9. Meta-wiki publication

**Client.** A small async `httpx` client against `https://meta.wikimedia.org/w/api.php` (configurable via `WIKI_API_URL`). BotPassword login, CSRF token, section create/append, `assert=user`, and maxlag-aware retries — no `mwclient`/`pywikibot` dependency.

**Authentication.** A dedicated on-wiki bot account. v1 uses a BotPassword with least-privilege grants; OAuth (owner-only consumer) is the P1 upgrade. Credentials live in the tool's home directory, never in the repository.

**Write method.** Prefer `action=edit` with `appendtext` for appends. `appendtext` is server-side and largely conflict-free, which suits incremental writes from multiple concurrent DM sessions.

**Page layout.** Output is talk-page style: **one section = one log**.
- **Group log:** every `/log` opens its own section on the configured log talk page (`section=new`, an atomic append), heading `"YYYY-MM-DD - HH:MM UTC : Pseudonym"` at the configured granularity. The entry renders as `": message --Pseudonym"` where the pseudonym is a **one-off label minted for that single entry** — it never repeats, so it carries zero linkage (R6). After handling, the bot deletes the `/log` command message from the group (requires the "Delete messages" admin right), hiding who requested the publication.
- **DM discussions:** each session is one section on the selected group's log page, heading `"YYYY-MM-DD - HH:MM UTC : Pseudonym"` (session start time), holding the whole exchange. Each message renders as `": message --Pseudonym"`, indented one level deeper than the last (`:`, `::`, `:::`) to track the back-and-forth. Appends target the session's section by heading, so concurrent sessions never interleave (N3); if the section is missing (archived mid-session), it is recreated.

**Timestamps.** Heading timestamps are configurable: `none`, `date`, or `minute` (`"YYYY-MM-DD - HH:MM UTC"`). The MediaWiki edit history records the precise edit time regardless, so minute granularity adds little correlation exposure; this residual exposure is acknowledged, not eliminated.

**Edit summaries.** Generic and non-identifying (for example, "Log entry via Blybot").

---

## 10. Anonymization and session model

- **Pseudonym generation:** random, from a CSPRNG, not a hash or transform of the Telegram user ID. This makes reversal or linkage across sessions infeasible even for the operator.
- **Session store:** an in-memory map keyed by the private chat_id, holding `{pseudonym, last_seen, meta_anchor}`. Never serialized to disk.
- **Session lifecycle:** created lazily on the first transcribed DM; ended by an inactivity timeout (default 45 minutes, configurable 30 to 60) or by an explicit `/flush`, which forces a new identity; also lost on job restart. All of these are acceptable and reinforce the anonymity goal. `/start` only delivers the welcome; `/whoami` discloses the current pseudonym without rotating it.
- **Write discipline:** because nothing is buffered persistently, content is written to Meta incrementally as it arrives (optionally debounced per R2/N2), so a mid-session restart never loses already-received content.

---

## 11. Data model and state

v1 had **no persistent datastore**; v2's self-service adds exactly one, per this section's original rule: **ToolsDB (MariaDB)**, never SQLite on NFS. State is:

1. **In-memory session map** (volatile, see 10).
2. **Configuration** in the tool home directory (see 12).
3. **v2: one `profiles` table on ToolsDB**, keyed by `(chat_id, thread_id)` so forum-group topics configure independently — chosen page/repo, consent policy, whether notifications are on, a JSON array of composable event **rules**, a JSON map of per-resource poll **cursors**, an admin-supplied API token encrypted with Fernet (`PROFILE_ENCRYPTION_KEY`), and (v3.1) a nullable `subscribe_code` capability marking the scope subscribable. Resolution is three-tier: topic override → group default (thread 0) → operator env default. On a self-service deployment `/log` publishes only when a page is set explicitly (topic or group); an unconfigured group is told to `/setpage` rather than silently using the operator default page. **The only identifiers persisted here are group structure (chat id, topic thread id) — never a user id, name, or message**; admin-ship is verified live per command and never stored.
4. **v3.1: one `subscriptions` table on ToolsDB** (the R6 carve-out, §21.1), keyed by a random `sub_id`, holding the subscriber's private `dm_chat_id`, the target `(chat_id, thread_id)`, the `schedule`/`recipe`/`lang` the user picked, and a `last_run` watermark. This is the sole place the application durably stores a Telegram user identifier; a subscriber's `/unsubscribe` deletes their row and the table exists only on capture-enabled deployments.

### 11.1 Composable event rules

Repository notifications are driven by per-scope **rules**, not a fixed digest. A rule is a **trigger** (event type) + **filter** (composable conditions) + **delivery mode**. Admins manage them with `/rule add <trigger> [filters…] [live|digest]`, `/rule remove <id>`, `/rule clear`, and `/rules`; `/events on|off` is the master switch and seeds two starter digest rules (`pr.merged`, `release`) when a scope has none. Cap: 20 rules per scope.

- **Triggers** (each reliably detectable from a REST list endpoint by comparing item timestamps against a per-resource watermark): `issue.opened`, `issue.closed`, `pr.opened`, `pr.closed`, `pr.merged`, `comment`, `release`.
- **Filters** (distinct keys AND; comma-separated or repeated values are any-of; unset never constrains): `label:`, `author:`, `base:`, `assignee:`, `milestone:`, `draft:true|false`, and `title:` — a substring, or `title:/regex/` for a case-insensitive pattern.
- **Delivery**: `live` sends one message per matching event as it is found; `digest` accumulates one combined message per poll cycle (silent when nothing matched). An event matching both a live and a digest rule is delivered once in each mode.

Each poll cycle the notifier polls only the resource streams the scope's rules need, matches every fresh event against every rule, advances per-resource cursors under a repo guard, and never replays history on the first poll. **Deferred** (need the issue-events *timeline* API, not a list snapshot): fine-grained `issue.reopened|labeled|assigned|milestoned` and `pr.ready`; also GitHub Discussions (GraphQL-only), PR reviews, path filters, and scheduled digests. Their conditions are still expressible as *filters* on the triggers above (e.g. `issue.opened label:bug`).

---

## 12. Configuration

Loaded from the tool home directory (env or a `0600`-permission file), not the repo:

| Key | Purpose | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token | (required) |
| `WIKI_API_URL` | MediaWiki endpoint | `https://meta.wikimedia.org/w/api.php` |
| `WIKI_USERNAME` / `WIKI_BOTPASSWORD` | On-wiki credentials (or OAuth keys) | (required) |
| `LOG_TARGET_PAGE` | Page for group `/log` entries | (required) |
| `DM_TARGET_BASE` | Legacy/fallback page base for DM discussions | (required) |
| `ALLOWED_GROUP_IDS` | Optional allowlist of group chat IDs | empty (allow configured) |
| `SESSION_TTL_MINUTES` | DM session inactivity timeout | 45 |
| `BURST_DEBOUNCE_SECONDS` | Coalesce window for DM writes | 8 |
| `TIMESTAMP_GRANULARITY` | `none`, `date`, or `minute` | `date` |
| `CONSENT_MODE` | `immediate`, `confirm`, or `author_only` (the open decision) | `immediate` |
| `WELCOME_TEXT` / `GROUP_GREETING_TEXT` | Message copy | provided |
| `USER_AGENT` | WMF-compliant UA string | (required) |
| `BOT_NAME` | Bot name used in greeting and edit summaries | `Blybot` |
| `MAINTAINER` | Shown in `/help` as the operator | empty (omitted) |
| `NEWCOMER_WELCOME` | `prompt` (R5 deep-link line on joins) or `off` | `prompt` |
| `LOG_THROTTLE_PER_MINUTE` | N4 cap on `/log` per group and user | 6 |
| `BUG_THROTTLE_PER_HOUR` | Cap on `/bug` reports per chat | 3 |
| `WIKI_MAX_RETRIES` | Bounded attempts per wiki write | 5 |
| `LOG_CLEANUP_SECONDS` | Delay before deleting the `/log` command (0 = keep) | 5 |
| `REPLY_CLEANUP_SECONDS` | Delay before the bot deletes its own `/log` replies (0 = keep) | 15 |
| `GITHUB_REPO` / `GITHUB_TOKEN` | `/bug` issue filing (token optional; absent = degrade to link) | `schiste/blybot` / empty |
| `WIKI_PAGE_SUFFIX` | Leaf appended to every `/setpage` base (`<base>/<suffix>`); empty disables self-service pages | empty |
| `PROFILE_ENCRYPTION_KEY` | Fernet key enabling ToolsDB profiles + encrypted group tokens | empty (self-service off) |
| `TOOLSDB_HOST` / `TOOLSDB_NAME` / `TOOLSDB_CNF` | ToolsDB connection (name defaults to `<cnf user>__blybot`) | Toolforge conventions |
| `EVENTS_POLL_MINUTES` | Minutes between repository-event polls for `/events` digests | 5 |

---

## 13. Architecture

A single long-running asyncio process, structured as ports-and-adapters (see the tree in the README):

- **Transport:** `python-telegram-bot`, long polling.
- **Dispatcher / handlers:** `/log` reply handler; DM message handler; `/start` (welcome / config deep link), `/flush`, `/whoami`, `/help`, `/privacy`, `/bug` handlers; join handler (deep-link button); `my_chat_member` handler (greet-on-entry); the group self-service commands (`/setup`, `/setpage`, `/setconsent`, `/setrepo`, `/events`, `/rule`, `/rules`, `/revoke`, `/settings`, `/reset`) and `/issue` + `/repo`.
- **Publisher:** MediaWiki client over `httpx` (not `mwclient`/`pywikibot`) — section create/append via the API, maxlag-aware, retrying with `assert=user`.
- **Anonymizer:** in-memory session store with a periodic TTL sweep task.
- **Sanitizer:** wikitext neutralization (R7), applied to all user content before publish.
- **Self-service (v2):** a ToolsDB profile store (encrypted per-group tokens), a per-group GitHub gateway, and a rules engine that polls bound repos and delivers matched events live or as digests. All optional — absent its config keys, the bot is the pure v1 single-tenant logger.
- **Config loader.**

The process is near-stateless and idempotent to restart. Concurrency is low and section append avoids edit conflicts, so no locking layer is needed.

---

## 14. Deployment and operations (Toolforge)

**Job type.** Continuous job under the jobs framework; Kubernetes restarts it if it exits:

```
toolforge jobs run blybot \
  --command ./run.sh \
  --image python3.x \
  --continuous \
  --mem 768Mi
```

`run.sh` activates the virtualenv and execs the bot module. Logs go to stdout and are captured by the jobs framework.

**Build.** Either a virtualenv in `$HOME` against a `--image python3.x`, or the build service (buildpacks) from a repo with `requirements.txt`.

**Pre-flight (blocking, do before writing much code):**
1. Confirm the tool account can reach `api.telegram.org` over outbound HTTPS from a bastion. If blocked, the fallback is webhooks served from a continuous job exposed at `<tool>.toolforge.org`, a heavier setup used only if forced.
2. Confirm outbound to the Meta API (native and expected to work).
3. Create the on-wiki account, issue a BotPassword, and (ideally) request the bot flag.
4. Create the target Meta pages and confirm the account can edit them.
5. Confirm privacy mode is ON for the bot — unless this is a
   **capture-enabled v3 deployment** (§21), which requires privacy mode
   OFF, flipped only after the announcement procedure in
   OPERATIONS.md; with it ON, `/capture` never receives the chatter it
   is meant to archive.

**Secrets.** Token and wiki credentials in `$HOME` at `0600`, isolated by the Toolforge tool account. Never in git.

**Acceptable use.** A Meta-publishing coordination tool for a Wikimedia community group is squarely movement infrastructure, which resolves the earlier scope concern.

---

## 15. Error handling and edge cases

| Case | Expected behavior |
|---|---|
| DM to a user who never started the bot | Never attempted; welcome is delivered only after the user taps Start (403 avoided by design). |
| `/log` on a media-only message | Decline with an ephemeral notice; publish nothing. |
| Logged text contains wikitext/templates/categories/signatures | Neutralized by the sanitizer; no page-structure effect. |
| Group upgraded to supergroup | Handle `migrate_to_chat_id`; update in-memory reference. |
| Meta edit conflict | Rare due to `appendtext`; on failure, retry with backoff. |
| `maxlag` / transient API error | Backoff and retry; drop after a bounded number of attempts and log operationally. |
| Target page protected or account lacks rights | Fail loudly in operator logs; reply to the user with a neutral error. |
| Message exceeds 4096 chars | Handle Telegram's limit; chunk or truncate on the wiki side as configured. |
| User blocks the bot mid-session | Session ages out normally; no error surfaced to others. |
| Job restart mid-session | In-flight session state lost; already-written content persists (incremental writes). |
| `/log` flooding | Per-user/per-group throttle (N4) caps entries per minute. |

---

## 16. Observability

- Operational logs to stdout (Toolforge job logs). **Logs must not contain message content or Telegram identifiers**, only event types, outcomes, and error codes.
- Emit counters for: publishes attempted/succeeded/failed, sanitizer neutralizations, sessions opened/expired, and API retry counts.
- A basic liveness signal so a wedged-but-running process can be restarted.

---

## 17. Data protection considerations

These shape the design and one of them remains an open governance decision.

- **Third-party consent (open, see 18).** In the group flow, person B can `/log` person A's message. A wrote it but did not consent to permanent publication. `CONSENT_MODE` encodes the chosen policy: `immediate`, `confirm` (bot DM-asks the author first), or `author_only` (only the author may log their own message).
- **No identifiers at rest.** Pseudonyms and session state are memory-only; nothing links wiki content back to a Telegram account after the process forgets it.
- **Irreversibility.** On-wiki publication is permanent in practice (page history, dumps, mirrors). `/log` means "publish forever." This informs the case for friction or a clear norm around it.
- **Content self-identification.** Anonymizing the authorship label does nothing for content that names its author ("as chair of the X group, I..."). The anonymity offered is of the label, not the content or timing.
- **Erasure.** Any takedown after publication is a wiki-side process (oversight/admin), outside the bot's scope in v1.

---

## 18. Open questions

- **[Governance / community] Consent mode default.** Which of `immediate` / `confirm` / `author_only` is the shipped default? Blocking for the group flow's social contract, not for the code (hook exists either way). *Recommendation to consider: `confirm` or `author_only` as the privacy-respecting default, but this is your call.*
- **[Design] DM anchoring.** One shared discussion page, a section per session, or a subpage per session?
- **[Wiki-admin / community] Target pages.** Which Meta pages, at what protection level, and is a bot flag approved?
- **[Engineering, blocking] Telegram outbound on Toolforge.** Verify before building; determines polling vs webhook.
- **[Engineering] Approval-to-join path.** If the R5 alternative (join requests granting PM permission) is chosen, verify the DM actually delivers, as this behavior has varied historically.
- **[Product] Media and multi-group.** Confirm both stay out of v1.

---

## 19. Phasing

- **Phase 0 (pre-flight):** outbound checks, on-wiki account + BotPassword, target pages, confirm privacy mode ON (OFF for capture-enabled v3 deployments — §21 and §14 item 5).
- **Phase 1 (MVP):** `/log` group flow, sanitizer, append to Meta, greet-on-entry, config, Toolforge continuous job.
- **Phase 2:** DM transcription, per-session pseudonyms, incremental writes, burst coalescing, per-session anchoring.
- **Phase 3:** newcomer welcome (join detection + deep-link Start).
- **Phase 4 (deferred):** consent-confirm flow, abuse throttle hardening, then the parked IRC-bot feature set as separately specced.

---

## 20. Success criteria

**Leading**
- Publish success rate above 99% (excluding declined media-only logs).
- Zero page-structure incidents from logged content (sanitizer correctness).
- Zero identifiers persisted, verifiable by inspecting disk and logs.
- Welcome delivered to a high share of newcomers who tap Start.
- Continuous-job uptime with automatic recovery across restarts.

**Lagging**
- The channel actually adopts `/log` as the way things reach Meta.
- No privacy or attribution incidents.
- Negligible operator maintenance burden.

---

## 21. Channel intelligence (v3)

v3 extends the bot into a channel-intelligence platform per
[PLAN-V3-CHANNEL-INTELLIGENCE.md](PLAN-V3-CHANNEL-INTELLIGENCE.md), the
normative design for this section. Where they conflict, v3 supersedes
two v1 statements for **capture-enabled deployments only**: non-goal 1
("no passive logging or statistics") and R1's privacy-mode-ON framing.
A deployment without `ARCHIVE_PSEUDONYM_KEY` remains exactly the v1/v2
bot, privacy mode ON.

**R-v3.1 — Capture is opt-in, loud, and reversible.** Message content is
archived only for scopes with capture explicitly enabled: groups via an
admin's `/capture on` (the confirmation is a permanent in-chat
announcement), broadcast channels by an admin adding the bot as channel
admin (announced with a channel post — the announcement precedes the
enable, so a channel that cannot be told is never archived). For groups,
`/capture off` stops collection and `/capture purge` hard-deletes the
scope's archive; for channels, admin status *is* the consent — demoting
or removing the bot disables capture, and a re-promotion re-announces
before re-enabling. The enable check is the single policy boundary
between the update stream and storage.

**R-v3.2 — Pseudonymized archive.** The `messages` table stores group
structure, timestamps, text, and an author label that is
HMAC-SHA256(operator key, scope‖user) — never a Telegram user id,
username, or display name. Labels are stable within a scope (statistics
work) and unlinkable across scopes. Rotating the key unlinkably re-keys
every author *going forward*: already-archived rows keep their old
labels — by design they cannot be recomputed, because the user id that
fed the HMAC is never stored — so rotation severs the link between an
author's past and future labels; erasing the old labels themselves is
`/capture purge`. Labels are likewise scoped to the chat id, so a
group→supergroup upgrade (which changes the id) restarts them: migrated
rows keep their old labels and later messages get new ones — a one-time
discontinuity where stats windows spanning the migration count an
author under two labels. Media bodies are not archived (a `media_note` row records
that media was posted). Ingest is guarded by text truncation at 4096
chars and a per-scope per-minute ceiling. The archive reflects neither
edits nor deletions: an edited or deleted Telegram message keeps its
originally-captured text — edits are a v3 non-goal, and the Bot API never
reports deletions to bots at all — so `/capture purge` (optionally with a
`CAPTURE_RETENTION_DAYS` window that purges older messages on the tick) is
the erasure mechanism.

**R-v3.3 — Actions.** Bot behavior composes as
`trigger → source → transforms → sink` pipelines stored per scope as
data ([ACTIONS.md](ACTIONS.md)): schedules (`every:<N>h`, `daily@HH:MM`,
`weekly@<dow>.HH:MM`, UTC) and commands trigger them; failures are
isolated per action and per scope; a failed run waits for its next slot.

**R-v3.4 — LLM analyses.** Prompt-driven analyses run against the Qwen
models on Wikimedia LiftWing behind a `PromptRunner` port. The model
never writes wikitext: templates demand structured JSON, every string
field passes the R7 sanitizer, and all markup comes from trusted render
code. A failed analysis publishes nothing. Published sections carry a
scope line and a machine-generated attribution footer. Output language,
platform, model, and sampling parameters are per-scope settings
(`/llm`), defaulting to LiftWing, `llm-qwen3-14b`, and English.

**R-v3.5 — Injection containment.** Archived chatter is adversarial
input: instructions and data are separated with per-request random
fencing, chat-template control tokens are scrubbed, the reduce step of a
chunked analysis sees only model partials, and structural guarantees (no
tool calls, destinations fixed before the model runs, R-v3.4's output
contract) cap the worst case at a false sentence inside a labeled
machine-generated section.

### 21.1 Opt-in digest subscriptions (v3.1)

Any individual can subscribe, in a private DM, to a recurring digest of a
capture-enabled scope's discussion, delivered privately to them. It reuses
the analysis pipeline for generation and the deep-link DM onboarding flow
for authorization; it adds no new content collection.

**Authorization is two-sided.** A scope admin opts the scope in with
`/subscribable on` (groups only, like every config command), which mints a
random `subscribe_code` on the profile and replies with a share link
`https://t.me/<bot>?start=sub_<code>`; `/subscribable off` clears the code
and no new subscriptions can be created (existing ones stop delivering on
the next tick — see below). A user opens the link, which resolves and
re-verifies the scope is **both** subscribable and capture-enabled, then
`/subscribe [schedule] [recipe] [lang:xx]` (defaults `daily@08:00`,
`summarize`, the scope's language) creates the subscription. `/mysubs`
lists a user's subscriptions and `/unsubscribe <id>` removes one.

**The R6 carve-out (see R6, §11).** The subscription record is the one place
the application durably stores a Telegram user identifier — the subscriber's
private chat id — kept only to deliver the digest and erased on
`/unsubscribe`. It lives in the `subscriptions` table and the
`domain/subscriptions.py` value object, deliberately outside the
identifier-free `domain/models.py`; it never enters `ActionContext`, Meta,
or the pseudonymized content layer. The public `/privacy` statement
discloses it in plain language.

**Delivery.** A `SubscriptionScheduler` collector mirrors the action
scheduler on the shared tick: it loads subscriptions, selects those due via
`Schedule.is_due`, **stamps `last_run` durably before sending** (so a
crash-after-send cannot double-deliver), **re-gates on the live
`subscribe_code` + `capture_enabled`** (an admin's `/subscribable off` or
`/capture off` silently skips delivery), runs the engine on the **group**
scope with a `window=since_last_run` summarize spec, and re-targets each
outbound message to the subscriber's DM via
`dataclasses.replace(chat_id=dm_chat_id, thread_id=0)` — the only step that
re-attaches the identifier. The existing rate-limited delivery loop sends
it. A per-tick cap (`max_per_tick`, default 200) bounds fan-out.

**Deferred:** a per-user subscription cap (abuse guard) and making a
broadcast channel's `subscribe_code` command-configurable (channels have no
config commands today — the same limitation as `/action`).

---

## 22. Platform contract & capabilities

The bot ran Telegram-only through v3.1. A later refactor factored every
Telegram assumption out of the core so a second chat platform (Discord,
shipped) plugs in behind the same ports, and a third (Slack, IRC, …) needs
only an adapter — no service or domain change. The core (`domain/` +
`services/`) now speaks one neutral vocabulary; each platform adapter is
the sole place that vocabulary meets an SDK.

**Scope: opaque, platform-tagged identity.** The old Telegram-specific
`(chat_id: int, thread_id: int)` pair is replaced by
`Scope(platform, channel, thread)` (`domain/models.py`), three **opaque
strings** the owning adapter mints and interprets — the core never parses
them, only compares and stores them. `thread` is `""` for a platform
without threads, or the channel default (what Telegram encoded as
`thread_id == 0`). A DM target is just a `Scope` whose `channel` is the DM
handle: on Telegram that handle equals the user id (the R6 subscription
carve-out, §21.1), on Discord it is the DM channel id — distinct from the
user id — so one addressing type covers group and DM delivery on both.
`Scope.key` (`"<platform>:<channel>/<thread>"`) is the collision-free
dict/registry/log key; the reserved separators `:` and `/` are forbidden
in the opaque parts so the key never needs escaping.

The **R6 privacy boundary is unchanged**: identity is now platform-tagged,
but still nothing user-identifying crosses inward. The ToolsDB `profiles`,
`messages`, and `subscriptions` tables are keyed by the string
`(platform, channel, thread)`; the legacy integer `chat_id`/`thread_id`
columns are kept **nullable and dual-written** on the Telegram path so the
prior release still reads every row after a rollback (a non-Telegram
platform leaves them `NULL`). Author labels remain HMAC-derived and scoped
(R-v3.2), unchanged by the retagging.

**Transport port + error taxonomy.** Outbound delivery goes through the
`Transport` port (`domain/ports.py`): one method `send(OutboundMessage)`
plus a `capabilities` descriptor. Each adapter maps its SDK's send
exceptions onto an **abstract, platform-neutral taxonomy** the core reasons
about:

- **`RateLimited(retry_after)`** — the platform is throttling; wait the
  stated interval and retry.
- **`TransientTransportError`** — a retryable blip (timeout, 5xx).
- **`PermanentTransportError`** — non-retryable (kicked, muted, channel
  gone); drop this message.

The platform-neutral retry loop lives in `services/delivery.py`
(`message_loop` / `_deliver`): it retries `RateLimited`/`Transient` up to a
bounded budget, drops `Permanent` at once, and — importing nothing but
`domain` + `ports` — drives **every** platform's transport unchanged. The
Telegram adapter maps python-telegram-bot's `RetryAfter`/`TimedOut`/other
`TelegramError`; the Discord adapter maps discord.py's
`RateLimited`/leaked HTTP 429, `DiscordServerError`/`TimeoutError`, and
other `HTTPException` — the loop never sees either SDK's classes.

**PlatformCapabilities: features gate on the platform, not vice versa.**
Each transport exposes a frozen `PlatformCapabilities` (`domain/models.py`)
so services and handlers gate Telegram-shaped features without importing
any adapter:

- `durable_dm` — the platform can durably DM a user who opted in →
  opt-in digest **subscriptions** (§21.1) are offered.
- `message_delete` — the bot can delete a message → `/log` and capture
  **cleanup** delete the command/reply.
- `deep_links` — deep-link Start (R5) → newcomer/subscription
  **onboarding** via a share link (Discord has none; onboarding is slash
  commands).
- `chat_picker` — a native "choose a chat" picker (Telegram) vs none.
- `id_can_change` — ids migrate (Telegram's group→supergroup upgrade) →
  the `migrate` re-key path (R-v3.2) is exercised.
- `threads` — the platform has threads/topics at all.
- `rich_choices` — rich inline choices are available.
- `max_message_chars` — the platform's hard per-message cap, **injected**
  wherever outbound text is bounded so no service hard-codes one platform's
  limit (the chunker and reply sink read it; the Discord transport chunks
  sends at it).

### 22.1 Capability matrix

Telegram and Discord values are the shipped `TELEGRAM_CAPABILITIES` /
`DISCORD_CAPABILITIES` constants; Slack and IRC are aspirational targets
for a future adapter, **not built** — their cells describe the expected
shape, nothing works yet.

| Capability | Telegram | Discord | Slack (future) | IRC (future) |
|---|---|---|---|---|
| `max_message_chars` | 4096 | 2000 | unbuilt | unbuilt |
| `threads` | yes | yes | unbuilt | unbuilt |
| `durable_dm` | yes | yes | unbuilt | unbuilt |
| `deep_links` | yes | no | unbuilt | unbuilt |
| `chat_picker` | yes | no | unbuilt | unbuilt |
| `message_delete` | yes | yes | unbuilt | unbuilt |
| `id_can_change` | yes | no | unbuilt | unbuilt |
| `rich_choices` | yes | yes | unbuilt | unbuilt |

Telegram supports every gate the core knows about, so on Telegram the
gates never change behavior — they exist for the platforms (Discord, and
later ones) that do not.

### 22.2 The two edges

Each adapter has exactly **two edges** where an SDK/int identity meets a
`Scope`, and the conversion lives only there:

- **Inbound:** an SDK `(channel, thread)` becomes a `Scope`
  (`telegram/_common.py`; `discord/scope.py` `scope_of` / `dm_scope`),
  and the author is pseudonymized (R6) — before anything crosses into a
  neutral service.
- **Outbound:** the transport turns a `Scope` back into the SDK target
  (`telegram_target`; `discord_target`) to send.

Everything between compares and stores opaque `Scope`s only. Discord ids
are int64 snowflakes stored as their decimal strings; a thread is itself a
channel, so an in-thread `Scope` carries the thread snowflake in `thread`
and the parent in `channel`.

### 22.3 Drift detection

Two mechanisms make an adapter that drifts from the contract fail CI, so a
third platform cannot half-implement the seam unnoticed:

**Shared conformance suite (`tests/conformance/`).** Every `Transport`
implementation (real Telegram driven by a fake bot, real Discord driven by
a fake client, and the in-repo `FakeTransport`) runs the *same* transport
contract; every store port's contract runs against *both* the in-memory
fake and the real ToolsDB adapter over its SQL-level fake runner. One
registry line per implementation fans the whole contract over it — a new
transport or store proves itself by being added, and any impl whose
behavior diverges from the port fails the build. (The SDK-error → taxonomy
mapping is platform-specific, so those cases are asserted per adapter.)

**Architecture guards (`tests/test_architecture.py`).** Fitness tests
enforce the layering and neutrality, generalized over a discovered set of
platforms (so a new adapter package is covered the moment it lands):

- the core imports **no** platform SDK; each adapter imports **only its
  own** platform's SDK, never another's;
- every platform package's `transport` module exports a `Transport`
  implementation **and** a `*_CAPABILITIES` descriptor (registry
  conformance);
- **no platform-branded string literal or identifier** appears in the
  domain data model (prose docstrings that explain the boundary are
  exempt);
- **no hard-coded per-message size literal** (`4096`, `3500`) appears in
  services — caps live only in `PlatformCapabilities`;
- a platform's user-identity attribute (Telegram's `from_user`) never
  appears outside its own adapter — the pseudonymization boundary, keyed
  per platform.

---

## Appendix: naming

The bot is named **Blybot**. The name is kept in config rather than hard-coded, so the greeting, edit summaries, and job name all read from a single source and a future rename stays a one-line change.
