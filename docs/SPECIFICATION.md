# Product Spec: Blybot

**A privacy-first Telegram bot that publishes explicitly selected messages to a Meta-wiki page, anonymously.**

| | |
|---|---|
| Status | Draft for review |
| Name | Blybot |
| Platform | Telegram Bot API + MediaWiki API (Meta-wiki) |
| Runtime | Wikimedia Toolforge (continuous job) |
| Version | v1 scope defined below; later phases parked |

---

## 1. Summary

Blybot is a small, single-purpose Telegram bot in the spirit of the old IRC utility bots, rebuilt around a privacy-first premise. It does not journal conversations passively and keeps no statistics. It only ever ingests two things: a message a user explicitly marks with `/log`, and messages a user sends to it in a private chat. Marked messages are published, without attribution, to a predefined Meta-wiki page. Private conversations are transcribed as an anonymized discussion on Meta using a per-session pseudonym that is never persisted. The bot runs as a continuous job on Toolforge.

The design deliberately keeps the bot structurally incapable of seeing ordinary group chatter. That property is enforced by Telegram's privacy mode, not merely by application logic.

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
The bot operates with Telegram privacy mode ON (the BotFather default). It must function correctly without ever disabling it.
- Given the bot is in a group with privacy mode on, when a user replies to a message with `/log`, then the bot receives the command together with the referenced message in `reply_to_message`.
- The bot never receives or processes non-command group messages.

**R2. `/log` reply-to publication.**
- Given a user replies to a text message with `/log`, when the bot receives it, then the referenced message's text is sanitized (R7) and appended to the configured Meta log page with no attribution.
- Given the referenced message has no text (media only), then the bot declines with a short, ephemeral notice and publishes nothing.
- Given publication succeeds, then the bot replies with a brief confirmation (optionally carrying a link to the page or section).

**R3. Greet-on-entry.**
- Given the bot is added to a group, when it joins, then it posts one short greeting message. This both explains `/log` and establishes the bot as the last bot to have spoken, so bare `/log` replies are delivered reliably even before any command addressing.

**R4. DM transcription with per-session pseudonym.**
- Given a user sends the bot a private message, when no active session exists for that chat, then the bot mints a fresh random pseudonym held only in memory and starts a session (sessions are created lazily by the first transcribed message, never by `/start`).
- Given an active session, when the user sends further messages, then each is sanitized (R7) and appended to the session's Meta discussion under the same pseudonym.
- Writes are incremental (per message or per debounced burst), never buffered until session end (R10, R-state).

**R5. Newcomer welcome via deep-link Start.**
- Given a newcomer joins, when the bot detects the join, then it posts a short in-group line with an inline button deep-linking to `https://t.me/<bot>?start=welcome`.
- Given the newcomer taps the button and presses Start, then the bot delivers the welcome privately; a pseudonymous session opens with their first transcribed message.
- The bot must never attempt to DM a user who has not initiated contact (doing so returns 403; see R-edge).

**R6. Anonymization guarantees.**
- No Telegram user ID, username, or display name is ever written to Meta.
- No Telegram identifier is written to disk anywhere by the application.
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

**Privacy mode.** Left ON. Under privacy mode the bot still receives commands addressed to it and replies meant for it, and a `/log` command sent as a reply carries the referenced message in `reply_to_message`. Ordinary chatter is never delivered.

**Command addressing.** When a user selects `/log` from the `/` autocomplete, the client appends `@<bot>`, guaranteeing delivery. Bare `/log` typed by hand only reaches the bot if it was the last bot to speak in the group, which R3 (greet-on-entry) ensures.

**Update subscription.** `allowed_updates` must explicitly include `message`, `my_chat_member`, and `chat_member`. `chat_member` (reliable join detection) is only delivered if the bot is a group admin and the update type is opted into. The lighter `new_chat_members` service message arrives without admin but is unreliable for silent link joins in large supergroups.

**Newcomer DM constraint.** A bot cannot open a private chat with a user who has not contacted it first; attempting to do so returns `403 Forbidden: bot can't initiate conversation with a user`. Hence the deep-link Start pattern in R5. This restriction is treated as aligned with the project's privacy stance, not merely worked around.

**Supergroup migration.** When a group is upgraded to a supergroup, its `chat_id` changes. The bot must handle `migrate_to_chat_id` and update any in-memory group reference, or it will silently fail to post.

**Limits.** Respect ~1 message/second per chat and 20 messages/minute per group. Telegram message bodies cap at 4096 characters.

---

## 9. Meta-wiki publication

**Client.** `mwclient` or `pywikibot` against `https://meta.wikimedia.org/w/api.php` (configurable).

**Authentication.** A dedicated on-wiki bot account. v1 uses a BotPassword with least-privilege grants; OAuth (owner-only consumer) is the P1 upgrade. Credentials live in the tool's home directory, never in the repository.

**Write method.** Prefer `action=edit` with `appendtext` for appends. `appendtext` is server-side and largely conflict-free, which suits incremental writes from multiple concurrent DM sessions.

**Page layout.** Output is talk-page style: **one section = one log**.
- **Group log:** every `/log` opens its own section on the configured log talk page (`section=new`, an atomic append), heading `"YYYY-MM-DD - HH:MM UTC : Pseudonym"` at the configured granularity. The entry renders as `": message --Pseudonym"` where the pseudonym is a **one-off label minted for that single entry** — it never repeats, so it carries zero linkage (R6). After handling, the bot deletes the `/log` command message from the group (requires the "Delete messages" admin right), hiding who requested the publication.
- **DM discussions:** each session is one section on the DM talk page, heading `"YYYY-MM-DD - HH:MM UTC : Pseudonym"` (session start time), holding the whole exchange. Each message renders as `": message --Pseudonym"`, indented one level deeper than the last (`:`, `::`, `:::`) to track the back-and-forth. Appends target the session's section by heading, so concurrent sessions never interleave (N3); if the section is missing (archived mid-session), it is recreated.

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

v1 has **no persistent datastore.** State is:

1. **In-memory session map** (volatile, see 10).
2. **Configuration** in the tool home directory (see 12).

No message log, no user table, no quotes table. If persistence is ever introduced (future phases), it must use ToolsDB (MariaDB), not SQLite on NFS, because a continuously writing SQLite file over the shared NFS home invites locking failures.

---

## 12. Configuration

Loaded from the tool home directory (env or a `0600`-permission file), not the repo:

| Key | Purpose | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token | (required) |
| `WIKI_API_URL` | MediaWiki endpoint | `https://meta.wikimedia.org/w/api.php` |
| `WIKI_USERNAME` / `WIKI_BOTPASSWORD` | On-wiki credentials (or OAuth keys) | (required) |
| `LOG_TARGET_PAGE` | Page for group `/log` entries | (required) |
| `DM_TARGET_BASE` | Page or subpage base for DM discussions | (required) |
| `ALLOWED_GROUP_IDS` | Optional allowlist of group chat IDs | empty (allow configured) |
| `SESSION_TTL_MINUTES` | DM session inactivity timeout | 45 |
| `BURST_DEBOUNCE_SECONDS` | Coalesce window for DM writes | 8 |
| `TIMESTAMP_GRANULARITY` | `none`, `date`, or `minute` | `date` |
| `CONSENT_MODE` | `immediate`, `confirm`, or `author_only` (the open decision) | `immediate` |
| `WELCOME_TEXT` / `GROUP_GREETING_TEXT` | Message copy | provided |
| `USER_AGENT` | WMF-compliant UA string | (required) |

---

## 13. Architecture

A single long-running asyncio process:

- **Transport:** `python-telegram-bot`, long polling.
- **Dispatcher / handlers:** `/log` reply handler; DM message handler; `/start` (welcome), `/flush` (identity reset), `/whoami`, `/help`, and `/privacy` handlers; join handler (deep-link button); `my_chat_member` handler (greet-on-entry).
- **Publisher:** MediaWiki module (`mwclient`/`pywikibot`), `appendtext`, maxlag-aware, retrying.
- **Anonymizer:** in-memory session store with a periodic TTL sweep task.
- **Sanitizer:** wikitext neutralization (R7), applied to all user content before publish.
- **Config loader.**

The process is near-stateless and idempotent to restart. Concurrency is low and `appendtext` avoids edit conflicts, so no locking layer is needed in v1.

---

## 14. Deployment and operations (Toolforge)

**Job type.** Continuous job under the jobs framework; Kubernetes restarts it if it exits:

```
toolforge jobs run blybot \
  --command ./run.sh \
  --image python3.x \
  --continuous \
  --mem 512Mi
```

`run.sh` activates the virtualenv and execs the bot module. Logs go to stdout and are captured by the jobs framework.

**Build.** Either a virtualenv in `$HOME` against a `--image python3.x`, or the build service (buildpacks) from a repo with `requirements.txt`.

**Pre-flight (blocking, do before writing much code):**
1. Confirm the tool account can reach `api.telegram.org` over outbound HTTPS from a bastion. If blocked, the fallback is webhooks served from a continuous job exposed at `<tool>.toolforge.org`, a heavier setup used only if forced.
2. Confirm outbound to the Meta API (native and expected to work).
3. Create the on-wiki account, issue a BotPassword, and (ideally) request the bot flag.
4. Create the target Meta pages and confirm the account can edit them.
5. Confirm privacy mode is ON for the bot.

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

- **Phase 0 (pre-flight):** outbound checks, on-wiki account + BotPassword, target pages, confirm privacy mode ON.
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

## Appendix: naming

The bot is named **Blybot**. The name is kept in config rather than hard-coded, so the greeting, edit summaries, and job name all read from a single source and a future rename stays a one-line change.
