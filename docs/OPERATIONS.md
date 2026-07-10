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

   ```
   log - Reply to a message with this to publish it anonymously
   help - How the bot works and which commands exist
   flush - Discard your pseudonym and get a fresh, unlinkable one
   whoami - Show which pseudonym you currently appear as
   privacy - What the bot collects, publishes, and stores
   bug - File an anonymous bug report with the maintainer
   ```

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
Telegram: `/setup`, `/setpage`, `/setconsent`, `/settings`, `/reset`.
Two env keys enable it:

```sh
become <tool>
# 1. Create the ToolsDB database (name = <cnf user>__blybot):
TOOL_DB="$(grep -oP "user\s*=\s*'?\K[^'\n]+" ~/replica.my.cnf)__blybot"
mariadb --defaults-file=$HOME/replica.my.cnf -h tools.db.svc.wikimedia.cloud \
    -e "CREATE DATABASE IF NOT EXISTS \`$TOOL_DB\`"
# 2. Generate the encryption key for group tokens, straight into the env file:
echo "PROFILE_ENCRYPTION_KEY=\"$($HOME/venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')\"" >> <name>.env
# 3. Choose the page prefix group admins may target:
echo 'WIKI_PAGE_PREFIX="Telegram logs/"' >> <name>.env
chmod 600 <name>.env
~/blybot/deploy-instance.sh start <name>
```

The schema bootstraps itself at startup. **What gets stored per group**:
its chat id, chosen page/repo, consent policy, event settings, and any
admin-supplied API token (Fernet-encrypted; the key never leaves the
env file). Never stored: user ids, usernames, messages. Losing
`PROFILE_ENCRYPTION_KEY` invalidates stored tokens (groups re-bind);
back up the env file accordingly. Verify tokens are ciphertext with:
`SELECT chat_id, LEFT(token_ciphertext, 20) FROM profiles;`

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
  every 15 minutes.
- Useful greps:

  | Log line | Meaning |
  |---|---|
  | `event=startup outcome=ok` | clean boot |
  | `event=log_command outcome=ok` | a `/log` published |
  | `event=dm_flush outcome=ok lines=N` | a DM burst landed on the wiki |
  | `event=dm_flush outcome=error` | a burst was dropped after retries |
  | `event=command_cleanup outcome=ignored` | missing the *Delete messages* admin right |
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
