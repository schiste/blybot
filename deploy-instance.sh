#!/usr/bin/env bash
# Manage Blybot instances on Toolforge. Run on a bastion as the tool user.
#
#   ./deploy-instance.sh init <name>    create <name>.env from the template
#   ./deploy-instance.sh start <name>   (re)start the continuous job for <name>
#   ./deploy-instance.sh update         pull + reinstall + restart every instance
#
# An "instance" is a config file $HOME/<name>.env plus a continuous job
# named <name>: one Telegram bot identity publishing to its own wiki
# pages. All instances share this repository checkout and one virtualenv,
# so `update` upgrades every instance at once.
#
# See docs/OPERATIONS.md for the full runbook.

set -euo pipefail

TOOL_HOME="${HOME}"
REPO_DIR="${TOOL_HOME}/blybot"
VENV="${TOOL_HOME}/venv"
IMAGE="python3.13"

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
    toolforge jobs run venv-update \
        --command "${VENV}/bin/pip install --quiet --force-reinstall --no-deps ${REPO_DIR}" \
        --image "${IMAGE}" --wait
    toolforge jobs delete venv-update >/dev/null 2>&1 || true
    rm -f "${TOOL_HOME}/venv-update.out" "${TOOL_HOME}/venv-update.err"
}

start_instance() {
    local name="$1"
    local env_file="${TOOL_HOME}/${name}.env"
    local wrapper="${TOOL_HOME}/run-${name}.sh"
    [ -f "${env_file}" ] || die "${env_file} not found; run: $0 init ${name}"
    grep -qE '^TELEGRAM_BOT_TOKEN=.+' "${env_file}" || die "${env_file} has no TELEGRAM_BOT_TOKEN yet"
    chmod 600 "${env_file}"
    printf '#!/bin/bash\nexport BLYBOT_CONFIG=%s\nexec %s/run.sh\n' "${env_file}" "${REPO_DIR}" >"${wrapper}"
    chmod +x "${wrapper}"
    ensure_venv
    toolforge jobs delete "${name}" >/dev/null 2>&1 || true
    toolforge jobs run "${name}" --command "${wrapper}" --image "${IMAGE}" --continuous --mem 512Mi
    echo "started job '${name}' (logs: ${TOOL_HOME}/${name}.out and .err)"
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
    start_instance "${2:?usage: $0 start <name>}"
    ;;
update)
    git -C "${REPO_DIR}" pull --ff-only
    reinstall
    for env_file in "${TOOL_HOME}"/*.env; do
        [ -e "${env_file}" ] || continue
        name="$(basename "${env_file}" .env)"
        echo "restarting instance '${name}'..."
        (start_instance "${name}") || echo "skipped '${name}' (not startable yet)"
    done
    ;;
*)
    die "usage: $0 {init <name>|start <name>|update}"
    ;;
esac
