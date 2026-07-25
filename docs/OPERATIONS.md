# Operations runbook

How to run one or many Blybot instances on Wikimedia Toolforge. An
**instance** = one Telegram bot identity + one config file + one
continuous job, publishing to its own wiki pages. All instances on a
tool share the repository checkout, the virtualenv, and the on-wiki
account.

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
   bot before flipping. Strongly consider a **separate bot account**
   for capture deployments (`deploy-instance.sh` supports several
   instances) so privacy-first groups keep the structural guarantee.
3. Group admins enable per scope with `/capture on` (announced in-chat,
   permanently); channels enable by making the bot a channel admin (it
   posts an announcement). `/capture off` stops; `/capture purge`
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
  (remaining Phase 0 item).
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

The bot never stores identifiers, but the *operator environment* must
hold the same line: keep env files at `0600` (run.sh refuses to start
otherwise), never copy logs elsewhere without checking them (they are
identifier-free, but belt and braces), and remember that everything
published on the wiki is permanent — takedowns are a wiki-side
(oversight) process, not a bot feature.
