#!/usr/bin/env bash
# Manage Blybot instances on Toolforge. Run on a bastion as the tool user.
#
#   ./deploy-instance.sh init <name>    create <name>.env from the template
#   ./deploy-instance.sh start <name>   (re)start every platform job for <name>
#   ./deploy-instance.sh update         pull + reinstall + restart every platform
#                                       of every instance
#
# An "instance" is one base config $HOME/<name>.env holding the shared
# settings plus whatever bot tokens you have. Each platform that has a token
# (TELEGRAM_BOT_TOKEN, DISCORD_BOT_TOKEN) runs as its OWN continuous job
# `<name>-<platform>` — isolated process, memory, and logs — while sharing
# this repo checkout, the venv, the wiki account, and the ToolsDB (whose rows
# are platform-tagged). Deploying an instance always (re)deploys every
# platform it has a token for; you never hand-create per-platform jobs, and
# removing a token retires that platform's job on the next deploy.
#
# See docs/OPERATIONS.md for the full runbook.

set -euo pipefail

TOOL_HOME="${HOME}"
REPO_DIR="${TOOL_HOME}/blybot"
VENV="${TOOL_HOME}/venv"
IMAGE="python3.13"
JOBS_DIR="${TOOL_HOME}/.blybot-jobs" # derived per-platform envs + wrappers

# Every platform the app supports; the deploy fans out one job per platform
# whose bot-token key below is set in the base env.
PLATFORMS="telegram discord"
token_key() {
    case "$1" in
    telegram) echo "TELEGRAM_BOT_TOKEN" ;;
    discord) echo "DISCORD_BOT_TOKEN" ;;
    *) return 1 ;;
    esac
}

die() {
    echo "deploy-instance: $*" >&2
    exit 1
}

command -v toolforge >/dev/null || die "run this on a Toolforge bastion as the tool user"
[ -d "${REPO_DIR}/.git" ] || die "repo missing: git clone https://github.com/schiste/blybot.git ${REPO_DIR}"

ensure_venv() {
    if [ ! -x "${VENV}/bin/python" ]; then
        echo "building the virtualenv inside the ${IMAGE} container..."
        toolforge jobs delete venv-build >/dev/null 2>&1 || true
        toolforge jobs run venv-build \
            --command "python3 -m venv ${VENV} && ${VENV}/bin/pip install --quiet --upgrade pip && ${VENV}/bin/pip install --quiet ${REPO_DIR}" \
            --image "${IMAGE}" --wait
        toolforge jobs delete venv-build >/dev/null 2>&1 || true
        rm -f "${TOOL_HOME}/venv-build.out" "${TOOL_HOME}/venv-build.err"
    fi
}

reinstall() {
    echo "reinstalling the package into the shared venv..."
    toolforge jobs delete venv-update >/dev/null 2>&1 || true
    # First pass installs any newly-added dependencies; second refreshes
    # the package itself (same version number, so pip must be forced).
    toolforge jobs run venv-update \
        --command "${VENV}/bin/pip install --quiet ${REPO_DIR} && ${VENV}/bin/pip install --quiet --force-reinstall --no-deps ${REPO_DIR}" \
        --image "${IMAGE}" --wait
    toolforge jobs delete venv-update >/dev/null 2>&1 || true
    rm -f "${TOOL_HOME}/venv-update.out" "${TOOL_HOME}/venv-update.err"
}

# (Re)deploy every platform job for one base instance. Idempotent: a platform
# with a token is (re)started; a platform without one has its job removed.
deploy_base() {
    local name="$1"
    local base_env="${TOOL_HOME}/${name}.env"
    [ -f "${base_env}" ] || die "${base_env} not found; run: $0 init ${name}"
    chmod 600 "${base_env}"

    local platform key job derived wrapper started=""
    mkdir -p "${JOBS_DIR}"
    chmod 700 "${JOBS_DIR}"

    # Retire the legacy single-name job from the pre-fan-out layout.
    toolforge jobs delete "${name}" >/dev/null 2>&1 || true

    for platform in ${PLATFORMS}; do
        key="$(token_key "${platform}")"
        job="${name}-${platform}"
        if ! grep -qE "^${key}=.+" "${base_env}"; then
            # No token for this platform: make sure any stale job is gone.
            toolforge jobs delete "${job}" >/dev/null 2>&1 || true
            continue
        fi
        ensure_venv
        derived="${JOBS_DIR}/${job}.env"
        wrapper="${JOBS_DIR}/run-${job}.sh"
        # Derived env = the base config with PLATFORM forced to this job's
        # platform (any PLATFORM line in the base is dropped first, so the
        # forced value always wins when run.sh sources the file).
        {
            grep -vE '^PLATFORM=' "${base_env}"
            echo "PLATFORM=${platform}"
        } >"${derived}"
        chmod 600 "${derived}"
        printf '#!/bin/bash\nexport BLYBOT_CONFIG=%s\nexec %s/run.sh\n' "${derived}" "${REPO_DIR}" >"${wrapper}"
        chmod +x "${wrapper}"
        toolforge jobs delete "${job}" >/dev/null 2>&1 || true
        toolforge jobs run "${job}" --command "${wrapper}" --image "${IMAGE}" --continuous --mem 768Mi
        echo "started job '${job}' (logs: ${TOOL_HOME}/${job}.out and .err)"
        started="${started} ${platform}"
    done

    [ -n "${started}" ] || die "${base_env} has no bot token yet (set TELEGRAM_BOT_TOKEN and/or DISCORD_BOT_TOKEN)"
}

case "${1:-}" in
init)
    name="${2:?usage: $0 init <name>}"
    env_file="${TOOL_HOME}/${name}.env"
    [ -f "${env_file}" ] && die "${env_file} already exists"
    cp "${REPO_DIR}/.env.example" "${env_file}"
    chmod 600 "${env_file}"
    echo "created ${env_file} — fill it in (nano ${env_file}), then: $0 start ${name}"
    ;;
start)
    deploy_base "${2:?usage: $0 start <name>}"
    ;;
update)
    git -C "${REPO_DIR}" pull --ff-only
    reinstall
    for env_file in "${TOOL_HOME}"/*.env; do
        [ -e "${env_file}" ] || continue
        name="$(basename "${env_file}" .env)"
        echo "redeploying instance '${name}' (every platform with a token)..."
        (deploy_base "${name}") || echo "skipped '${name}' (not startable yet)"
    done
    ;;
*)
    die "usage: $0 {init <name>|start <name>|update}"
    ;;
esac
