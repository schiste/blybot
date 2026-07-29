# Operations runbook

How to run one or many Blybot instances on Wikimedia Toolforge. An
**instance** = one config file `~/<name>.env` publishing to its own wiki
pages; it runs **one continuous job per platform it has a token for**
(`<name>-telegram`, `<name>-discord`, …) — see "Choosing a platform"
below. All instances on a tool share the repository checkout, the
virtualenv, and the on-wiki account.

Everything below runs on a Toolforge bastion **as the tool user**:

```sh
ssh <you>@login.toolforge.org
become <tool>
```

## One-time tool setup

```sh
git clone https://github.com/schiste/blybot.git ~/blybot
~/blybot/deploy-instance.sh init <name>     # creates ~/<name>.env (0600)
nano ~/<name>.env                           # fill it in — see below
~/blybot/deploy-instance.sh start <name>    # builds the venv if needed, starts the job
```

The helper names everything after the instance: config `~/<name>.env`,
wrapper `~/run-<name>.sh`, job `<name>`, logs `~/<name>.out|.err`.

## Choosing a platform

One instance runs **every platform it has a token for**, each as its own
isolated continuous job. Put whichever bot tokens you have in the single
base env `~/<name>.env`, and `deploy-instance.sh` starts one job per
platform whose token is present: `TELEGRAM_BOT_TOKEN` → job
`<name>-telegram`, `DISCORD_BOT_TOKEN` → job `<name>-discord`. Deploying an
instance always (re)deploys all of them; removing a token retires that
platform's job on the next deploy. You never hand-create per-platform
instances, and a crash in one platform's job cannot touch the other's
(separate process, memory, and `<name>-<platform>.out`/`.err` logs).

Every non-chat feature (wiki publishing, ToolsDB, capture, LLM analyses,
subscriptions) is shared: the platforms run against the **same** ToolsDB,
whose rows are platform-tagged (`platform, channel, thread`), so Telegram
and Discord state coexist without collision.

**Shared env vars (all platforms).** `WIKI_USERNAME`, `WIKI_BOTPASSWORD`,
`WIKI_API_URL`, `LOG_TARGET_PAGE`, `DM_TARGET_BASE`, `USER_AGENT`,
`PROFILE_ENCRYPTION_KEY`, `TOOLSDB_HOST` / `TOOLSDB_NAME` / `TOOLSDB_CNF`,
`ARCHIVE_PSEUDONYM_KEY`, and the `LIFTWING_*` / `LLM_*` analysis keys mean
the same thing for every platform.

**Per-platform tokens:** `TELEGRAM_BOT_TOKEN` (Telegram prerequisites
below), `DISCORD_BOT_TOKEN` (Discord runbook below). Set as many as you
want to run; an instance with none fails fast with
`deploy-instance: … has no bot token yet`.

(`PLATFORM` in the env file is only consulted for a direct
single-platform run — `python -m blybot`; the Toolforge deploy overrides it
per job from the tokens present, so you normally leave it unset.)

### Discord setup runbook

1. **Create the application + bot.** At
   [discord.com/developers/applications](https://discord.com/developers/applications)
   → **New Application**. Open **Bot**, then **Reset Token** and copy the
   value into `DISCORD_BOT_TOKEN` in the instance env file (over SSH,
   never into a chat or commit).
2. **Enable the privileged intents.** On the same **Bot** page, turn on
   **Message Content Intent** (capture cannot read message text without
   it) and **Server Members Intent**. Both are privileged and off by
   default.
3. **Invite the bot.** **OAuth2 → URL Generator**: scopes `bot` and
   `applications.commands`; bot permissions **View Channels**, **Send
   Messages**, **Send Messages in Threads**, **Read Message History**.
   Open the generated URL and add the bot to the server.
4. **Configure the instance.** Add `DISCORD_BOT_TOKEN=…` to `~/<name>.env`
   (alongside any `TELEGRAM_BOT_TOKEN` — both run side by side), then
   `~/blybot/deploy-instance.sh start <name>`. That starts (or restarts) a
   `<name>-discord` job automatically; no `PLATFORM` line is needed.

Slash commands are published on startup (`CommandTree.sync`) and can take
**several minutes** to appear in the client the first time — this is
Discord-side propagation, not a bot fault. Onboarding is slash commands,
not deep links (`deep_links=False`): a channel becomes subscribable the
first time anyone runs `/subscribe` in it, rather than via an
admin-shared link.

The slash commands the gateway registers:

- **server admins** — `/capture on|off`, `/setpage <path>`, `/settings`,
  `/reset`, `/revoke`, `/llm show|set|reset`, `/setrepo owner/repo`,
  `/settoken`, `/events on|off`, `/rule add|remove|clear`, `/rules`;
- **on-demand analyses** — `/summarize`, `/stats`, `/talkingpoints`
  (deferred first: a chunked run outlives Discord's 3-second deadline);
- **durable-DM digests** — `/subscribe [schedule] [recipe] [lang:xx]`,
  `/mysubs`, `/unsubscribe <id>`.

`/rule` uses Discord's native subcommand routing (`/rule add …`), where
Telegram parses the same grammar out of its argument list; both call the
identical neutral `CommandService`, so the rule syntax and every reply are
shared. Admin replies are ephemeral (only the caller sees them) and
server-admin checks are live (`guild_permissions.administrator`), never
stored. Capture ingestion, pseudonymization, the archive, LLM analyses,
repo notifications, and subscription delivery all reuse the same neutral
services as Telegram.

Repo notifications need a repo bound **at the channel itself**: run
`/setrepo owner/repo` there, then `/events on`. The background
`repo_notify` poller runs on every Discord deployment that has a profile
store.

**Handing over the GitHub token.** Telegram uses a deep link into DM;
Discord has none, so `/settoken` opens a **modal** — a private form whose
value travels in the interaction payload and is never posted as a message,
to the channel or to a DM. Nothing lands in any chat history, so unlike
Telegram there is no pasted secret for the bot to delete. Deliberately
*not* a slash-command parameter (`/settoken token:…`), which would be typed
into the visible command bar and retained in the client's command history.
The token is validated against the bound repo before it is encrypted and
stored, and admin-ship is re-checked when the form is **submitted**, not
only when it was opened.

### What Discord does NOT do yet

These Telegram surfaces are **deferred / not yet built** on Discord, so an
operator should not expect full parity:

- **Repo commands** — `/issue` and `/repo` are Telegram-only (issue #42),
  so a stored token currently powers notifications but no interactive
  repo commands on Discord.
- **Scheduled wiki analyses** — the action scheduler (`/action`) does not
  run on Discord; only the subscription digest tick, the repo poller, and
  the capture reminder run in the background.
- **`/setconsent`, `/subscribable`, `/capture purge`** — no Discord
  equivalent yet.
- **DM `/log` + the chat picker** — the private `/log` flow and the
  "choose a shared group" picker depend on `deep_links` / `chat_picker`,
  which Discord lacks.

Everything above is intentionally absent, not broken — it lands when the
Discord admin surface grows the corresponding commands.

## Per-instance prerequisites

1. **Telegram bot** — create via @BotFather (`/newbot`). Confirm
   **Group Privacy is enabled** (`/mybots` → Bot Settings) — R1 depends
   on it. Recommended `/setcommands`:

   Each description is tagged with where the command is used —
   `[group]`, `[private]`, or `[both]`:

   ```
   log - [group] Reply to a message with this to publish it anonymously
   help - [both] How the bot works and which commands exist
   flush - [private] Discard your pseudonym and get a fresh, unlinkable one
   whoami - [private] Show which pseudonym you currently appear as
   privacy - [private] What the bot collects, publishes, and stores
   bug - [private] File an anonymous bug report with the maintainer
   issue - [both] Private: bug to maintainer; group: file in the bound repo
   repo - [group] Show the bound repository's open items
   setup - [group] (admins) how to configure the bot for this group
   setpage - [group] (admins) where /log publishes (under <path>/Telegram logs)
   setconsent - [group] (admins) who may /log whose messages
   setrepo - [group] (admins) bind a GitHub repository
   events - [group] (admins) turn rule-driven repo notifications on/off
   rule - [group] (admins) add/remove/clear composable event rules
   rules - [group] (admins) list this chat's event rules
   settings - [group] (admins) current group configuration
   revoke - [group] (admins) discard this group's stored token
   reset - [group] (admins) forget this group's configuration
   capture - [group] (admins) archive this chat's messages for analyses
   summarize - [group] (admins) publish a summary of recent messages
   talkingpoints - [group] (admins) publish recent talking points
   stats - [group] (admins) publish activity statistics
   run - [group] (admins) run any prompt template on the archive
   action - [group] (admins) schedule recurring analyses
   llm - [group] (admins) this chat's model, language, parameters
   ```

   Telegram also supports true per-scope command menus (`setMyCommands`
   with a scope) if you'd rather admins only see group commands in
   groups and DM commands in private; the tags above are the simpler
   single-list approach.

2. **Wiki** — target page(s) for `LOG_TARGET_PAGE` / `DM_TARGET_BASE`
   (may be the same page). The on-wiki account and BotPassword can be
   shared across instances; create them once at
   Special:BotPasswords with the *edit* grant.

3. **Config** — every key is documented in [.env.example](../.env.example)
   and spec §12. Secrets (`TELEGRAM_BOT_TOKEN`, `WIKI_BOTPASSWORD`,
   optional `GITHUB_TOKEN`) go straight into the env file over SSH —
   never into chats, commits, or issues.

4. **Telegram group rights** — add the bot to the group (it greets
   once). Promote it to admin with **Delete messages** if you want the
   `/log` command auto-deleted; everything else works without admin.

## Enabling self-service (v2)

With self-service on, any group's admins configure the bot from
Telegram: `/setup`, `/setpage`, `/setconsent`, `/setrepo` (+ the DM
token step), `/events`, `/rule`, `/rules`, `/settings`, `/revoke`,
`/reset`; members get
`/issue` and `/repo` once a repo is bound. **Forum groups:** run a
command inside a topic to configure that topic (its own page and/or
repo), or in the General area to set the group default that every other
topic inherits — consent stays group-wide. A self-service group must
run `/setpage` before `/log` works; an unconfigured scope is refused
rather than falling back to the operator default page. Two env keys
enable it:

```sh
become <tool>
# 1. Create the ToolsDB database (name = <cnf user>__blybot):
TOOL_DB="$(grep -oP "user\s*=\s*'?\K[^'\n]+" ~/replica.my.cnf)__blybot"
mariadb --defaults-file=$HOME/replica.my.cnf -h tools.db.svc.wikimedia.cloud \
    -e "CREATE DATABASE IF NOT EXISTS \`$TOOL_DB\`"
# 2. Generate the encryption key for group tokens, straight into the env file:
echo "PROFILE_ENCRYPTION_KEY=\"$($HOME/venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')\"" >> <name>.env
# 3. Choose the subpage leaf every /setpage base gets (safe, group-adaptable):
echo 'WIKI_PAGE_SUFFIX="Telegram logs"' >> <name>.env
chmod 600 <name>.env
~/blybot/deploy-instance.sh start <name>
```

The schema bootstraps itself at startup (additive, idempotent — safe to
redeploy over an older table). **What gets stored per group**: its chat
id, chosen page/repo, consent policy, event notification rules, per-
resource poll cursors, and any admin-supplied API token (Fernet-
encrypted; the key never leaves the env file). Never stored: user ids,
usernames, messages. Losing
`PROFILE_ENCRYPTION_KEY` invalidates stored tokens (groups re-bind);
back up the env file accordingly. Verify tokens are ciphertext with:
`SELECT chat_id, LEFT(token_ciphertext, 20) FROM profiles;`

## Enabling channel capture (v3)

Capture archives message content for scopes that opt in, powering the
v3 analyses. **This changes the bot's privacy posture — read this whole
section before enabling.**

1. Requires self-service (`PROFILE_ENCRYPTION_KEY`) plus
   `ARCHIVE_PSEUDONYM_KEY` (any long random string). Rotating the key
   re-keys the labels of everything archived *from then on* and
   unlinkably severs them from the old ones; already-stored rows keep
   their old labels (they cannot be recomputed — the user id behind
   them is never stored). To erase the old labels too, `/capture purge`.
2. **Flip privacy mode OFF at BotFather** (`/setprivacy` → Disable).
   From that moment the bot *receives* all group chatter everywhere it
   sits — the only thing between chatter and storage is the capture
   policy check, which stores nothing for scopes that didn't opt in.
   Announce the posture change to every community whose group has the
   bot before flipping (ready-to-post copy below). Strongly consider a
   **separate bot account** for capture deployments
   (`deploy-instance.sh` supports several instances) so privacy-first
   groups keep the structural guarantee.

   Post this (adapted) in every affected chat **before** the flip:

   > ⚙️ Heads-up from the operator of {bot}: on {date} this bot's
   > Telegram "privacy mode" will be switched off so that chats which
   > *explicitly opt in* can have their messages archived for public
   > on-wiki summaries and statistics. What changes for THIS chat:
   > nothing is collected or stored unless an admin runs /capture on
   > here (that decision is announced in the chat, permanently).
   > Technically the bot will now *receive* ordinary messages; its
   > policy check discards them for chats that have not opted in, and
   > that code is public: {repo_url}. Archived chats store authors only
   > as anonymous labels — never usernames or ids. Questions to
   > {maintainer}.

   And a matching note on the bot's wiki user page or the wiki forum
   your communities use:

   > {bot} (source: {repo_url}) now supports opt-in channel/group
   > archiving to power machine-generated summaries and statistics
   > published on wiki pages chosen by each chat's admins. Sections it
   > publishes are labeled machine-generated, name no users, and every
   > model output is sanitized so it cannot alter page structure. The
   > underlying models run on Wikimedia LiftWing. Operator contact:
   > {maintainer}.
3. Group admins enable per scope with `/capture on` (announced in-chat,
   permanently); channels enable by making the bot a channel admin (it
   posts an announcement first — if it cannot post, capture does not
   start) and disable by demoting or removing it. Re-promoting the bot
   re-announces: a fresh loud opt-in, never a silent resumption. A
   demotion during a ToolsDB outage is held fail-closed in memory and
   made durable within one maintenance tick (~60s) of storage
   recovering; the one accepted residual is a bot *restart during the
   outage or inside that first post-recovery tick*, which loses the
   pending revocation (it cannot be durably recorded while the durable
   store is down) — after any outage-plus-restart incident, check
   `capture_enabled` rows against current channel admin status.
   In groups `/capture off` stops; `/capture purge`
   erases (`/capture purge before:YYYY-MM-DD` trims only older rows).
   Set `CAPTURE_REANNOUNCE_DAYS` to re-post the announcement on a
   cadence. Note: the archive keeps the first version of each message —
   Telegram edits and deletions do not propagate to it (or to anything
   already published). A group→supergroup upgrade moves the archive to
   the new chat id, but author labels (HMAC-scoped to the id) restart
   from the migration on: stats windows spanning it count an author
   under two labels, once.
4. Watch the counters: `captures`, `captures_throttled`,
   `captures_failed`, and `injection_suspected` (injection-shaped
   phrases seen in analyzed transcripts — telemetry, not a gate).
   Archive growth is operator-owned — there is no automatic expiry; the
   heartbeat logs an `archive_size` line (total rows) every ~15 minutes,
   and per-scope size is
   `SELECT chat_id, COUNT(*) FROM messages GROUP BY chat_id;`

## Opt-in digest subscriptions (v3.1)

Rides along automatically on any capture-enabled deployment — no extra
env variable. Individuals subscribe in a DM to receive a group's digest
privately.

1. A group admin runs `/subscribable on` in the group (or topic). The bot
   mints a share link `https://t.me/<bot>?start=sub_<code>`; anyone with
   the link can subscribe. `/subscribable off` revokes it — the link dies
   and existing subscriptions stop delivering on the next tick.
2. A user opens the link and runs `/subscribe [schedule] [recipe]
   [lang:xx]` (defaults `daily@08:00`, `summarize`, the scope's language).
   `/mysubs` lists theirs; `/unsubscribe <id>` removes one.
3. Delivery re-checks the scope is still subscribable **and** capture is
   still on, stamps progress before sending (no double-DMs on a crash),
   and fans out under the same rate limiter as everything else, capped at
   `max_per_tick` (200) subscriptions per tick.
4. Counters: `subscription_digests` (delivered) and
   `subscription_ticks_failed` (a collection/delivery cycle that raised).
   The `subscriptions` table is the one place a subscriber's private chat
   id is stored — see "Privacy invariants" below.

## LiftWing LLM endpoints (v3 pre-flight findings)

The v3 analyses ([plan](PLAN-V3-CHANNEL-INTELLIGENCE.md)) call the Qwen
chat models hosted on Wikimedia LiftWing through its OpenAI-compatible
endpoint:

```
POST https://api.wikimedia.org/service/lw/inference/v1/models/llm-<model>/openai/v1/chat/completions
```

Verified live on 2026-07-25 (anonymous tier):

- `llm-qwen3-14b` (16K context) and `llm-qwen36-27b` (Qwen3.6 27B, 32K
  context) both answer; `llm-qwen3-27b` does **not** exist. Responses
  are OpenAI-shaped (`choices[].message.content`, `finish_reason`,
  `usage` with prompt/completion token counts).
- No API key. Anonymous callers share a ~100 req/h pool; calls from
  Toolforge are effectively unthrottled — measure Toolforge-tier latency
  and long-generation behavior before enabling scheduled analyses
  (remaining Phase 0 item). From a bastion, `become <tool>` and run
  `python3 ~/blybot/scripts/liftwing_baseline.py` (stdlib-only, no
  credentials); record the medians here and size
  `LIFTWING_TIMEOUT_SECONDS` above the observed maximum.
- Reference:
  [Machine_Learning/LiftWing/Large_Language_Models/Wikimania_2026](https://wikitech.wikimedia.org/wiki/Machine_Learning/LiftWing/Large_Language_Models/Wikimania_2026)
  on wikitech.

Operating the analyses: watch `prompts_run`, `prompt_tokens`,
`completion_tokens` (consumption), `prompt_failures` (transport),
`analyses_aborted` (the model violated the output contract — nothing was
published; occasional is normal, sustained means a template or model
regression), and `analyses_failed` (unexpected command failures). A
scope's model/language/parameters are admin-set via `/llm` in the chat;
`LLM_MAX_TOKENS_CEILING` and `LLM_MAX_CHUNKS_PER_RUN` are the operator
backstops.

## Updating (all instances at once)

```sh
~/blybot/deploy-instance.sh update
```

Pulls `main`, reinstalls the package into the shared venv (inside the
runtime container, via a one-off job), and restarts every job that has
a `~/<name>.env`.

## Monitoring

- `toolforge jobs list` — the job must be `Running`; Kubernetes
  restarts it automatically if it exits.
- Logs in `~/<name>.err` are **event-only by design** (spec §16): event
  names, outcomes, and counts — never message content or Telegram
  identifiers. A `heartbeat` line with counter totals appears roughly
  every 15 minutes. v3 counters to know: `captures[_throttled|_failed]`
  (archive ingest), `prompts_run`/`prompt_tokens`/`completion_tokens`/
  `prompt_failures` (LiftWing), `analyses_aborted` (output-contract
  violations — nothing published), `analyses_failed`, `actions_run`/
  `actions_empty`/`actions_failed`/`actions_configured` (scheduler).
- Capture + analyses raise the memory floor: run v3 instances with
  `--mem 768Mi` (archive window queries plus LLM payloads) instead of
  the v1/v2 512Mi.
- Useful greps:

  | Log line | Meaning |
  |---|---|
  | `event=startup outcome=ok` | clean boot |
  | `event=log_command outcome=ok` | a `/log` published |
  | `event=dm_flush outcome=ok lines=N` | a DM burst landed on the wiki |
  | `event=dm_flush outcome=error` | a burst was dropped after retries |
  | `event=command_cleanup outcome=ignored` | missing the *Delete messages* admin right |
  | `event=repo_poll outcome=ok events=N` | a group's `/rule` matches were delivered |
  | `event=repo_poll outcome=error` | a repo poll failed for one group (others unaffected) |
  | `event=token_bound outcome=ok` | a group admin bound a GitHub token |
  | `event=wiki_edit outcome=retry` | maxlag/transient API backoff in progress |
  | `event=wiki_login outcome=error` | BotPassword rejected — check credentials |

## Troubleshooting

- **Exit with `configuration error: missing required configuration
  keys`** — the env file is incomplete; the message names the keys
  (never the values).
- **`InvalidToken` at startup** — wrong `TELEGRAM_BOT_TOKEN`.
- **Publishes fail, `wiki_login` errors** — BotPassword revoked or
  mistyped; regenerate at Special:BotPasswords.
- **`/log` command not deleted** — grant the bot the *Delete messages*
  admin right in the group.
- **Newcomer prompt unwanted/missing** — `NEWCOMER_WELCOME=off|prompt`;
  detection also requires the bot to be a group admin.
- **A restart lost active DM sessions** — by design (spec §10):
  identities are memory-only. Already-published content is unaffected.

## Privacy invariants for operators

The bot stores no Telegram user identifier anywhere, with one documented
exception: an opt-in digest subscription durably records the subscriber's
private chat id (and nothing else about them) so the digest can reach them
— erased on their `/unsubscribe`, and present only on capture-enabled
deployments. Everything else holds the original line, and the *operator
environment* must too: keep env files at `0600` (run.sh refuses to start
otherwise), never copy logs elsewhere without checking them (they are
identifier-free, but belt and braces), and remember that everything
published on the wiki is permanent — takedowns are a wiki-side
(oversight) process, not a bot feature.
