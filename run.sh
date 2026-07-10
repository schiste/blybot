#!/usr/bin/env bash
# Toolforge continuous-job entry point (spec section 14).
#
#   toolforge jobs run blybot --command ./run.sh --image python3.13 --continuous --mem 512Mi
#
# Expects:
#   - a virtualenv at $HOME/venv (created once from a bastion:
#       python3 -m venv $HOME/venv && $HOME/venv/bin/pip install /path/to/blybot)
#   - configuration in $HOME/blybot.env with 0600 permissions (see .env.example)

set -euo pipefail

CONFIG_FILE="${BLYBOT_CONFIG:-$HOME/blybot.env}"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "blybot: config file not found: $CONFIG_FILE" >&2
    exit 2
fi

perms="$(stat -c '%a' "$CONFIG_FILE" 2>/dev/null || stat -f '%Lp' "$CONFIG_FILE")"
if [[ "$perms" != "600" ]]; then
    echo "blybot: refusing to start: $CONFIG_FILE must be chmod 600 (is $perms)" >&2
    exit 2
fi

set -a
# shellcheck disable=SC1090
source "$CONFIG_FILE"
set +a

exec "$HOME/venv/bin/python" -m blybot
