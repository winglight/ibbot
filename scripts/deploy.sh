#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-${ROOT_DIR}/docker}"
COMPOSE_CMD=${COMPOSE_CMD:-"docker compose"}
IFS=' ' read -r -a COMPOSE_PARTS <<< "${COMPOSE_CMD}"

SERVICES=("ib-gateway" "ibbot" "optimizer" "cloudflared")
declare -A SERVICE_DEPENDENCIES=(
    ["ib-gateway"]=""
    ["ibbot"]="ib-gateway"
    ["optimizer"]="ibbot"
    ["cloudflared"]="ibbot"
)

function usage() {
    cat <<EOF
Usage: $(basename "$0") <command> [service]

Commands:
  up                Start all services in dependency order (docker compose up -d)
  restart <service> Restart the specified service and its dependencies
  deploy            Alias for 'up'
EOF
}

function run_compose() {
    (cd "${COMPOSE_DIR}" && "${COMPOSE_PARTS[@]}" "$@")
}

function resolve_plan() {
    local target="$1"
    local -n _result=$2
    local -A visited=()

    function _visit() {
        local service="$1"
        if [[ -n "${visited[$service]:-}" ]]; then
            return
        fi
        visited[$service]=1
        local deps="${SERVICE_DEPENDENCIES[$service]:-}"
        for dep in $deps; do
            if [[ -z "${SERVICE_DEPENDENCIES[$dep]+x}" ]]; then
                echo "Unknown dependency '${dep}' for service '${service}'" >&2
                exit 1
            fi
            _visit "$dep"
        done
        _result+=("$service")
    }

    if [[ -z "${SERVICE_DEPENDENCIES[$target]+x}" ]]; then
        echo "Unknown service: ${target}" >&2
        exit 1
    fi
    _visit "$target"
}

function restart_service() {
    local service="$1"
    local plan=()
    resolve_plan "$service" plan
    for svc in "${plan[@]}"; do
        echo "Restarting ${svc}..."
        if ! run_compose restart "${svc}"; then
            echo "Restart failed, starting ${svc} with up -d" >&2
            run_compose up -d "${svc}"
        fi
    done
}

function start_all() {
    for svc in "${SERVICES[@]}"; do
        echo "Starting ${svc}..."
        run_compose up -d "${svc}"
    done
}

command="${1:-}"
case "$command" in
    up|deploy)
        start_all
        ;;
    restart)
        if [[ $# -lt 2 ]]; then
            usage
            exit 1
        fi
        restart_service "$2"
        ;;
    help|-h|--help|"")
        usage
        ;;
    *)
        echo "Unknown command: ${command}" >&2
        usage
        exit 1
        ;;
esac
