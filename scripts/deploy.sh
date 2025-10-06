#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${ROOT_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${ROOT_DIR}/.env"
    if [[ -f "${ROOT_DIR}/.env.local" ]]; then
        # shellcheck disable=SC1091
        source "${ROOT_DIR}/.env.local"
    fi
    set +a
fi

COMPOSE_BIN=${COMPOSE_CMD:-"docker compose"}
DEFAULT_ENVIRONMENT="local"
COMPOSE_BASE_FILE="${ROOT_DIR}/docker/local.compose.yml"
COMPOSE_PROD_FILE="${ROOT_DIR}/docker/prod.compose.yml"
START_SEQUENCE=(
    redis
    mariadb
    ib-gateway
    account
    orders
    ibbot
    ib-command
    ib-feed
    connection_control
    market-data
    risk
    strategy
    notification
    queue-monitor
    optimizer
    optimizer-ui
    cloudflared
)
REFRESH_SERVICES=(
    redis
    mariadb
    account
    orders
    ibbot
    ib-command
    ib-feed
    connection_control
    market-data
    risk
    strategy
    notification
    queue-monitor
    optimizer
    optimizer-ui
    cloudflared
)
STARTED_SERVICES=()

usage() {
    cat <<'USAGE'
Usage: deploy.sh <command> [options]

Commands:
  up               Build and start the stack in dependency order
  refresh          Update code, rebuild and restart services (skip ib-gateway)
  down             Stop and remove the stack
  restart [svc]    Restart a specific service (defaults to full stack)
  status           Show service status via docker compose ps
  diagnose         Run scripts/diagnose_foundation.py against the active stack

Options:
  --env <name>     Environment to deploy (local|production). Default: local
  --no-diagnose    Skip diagnostics after bringing the stack up
  --build          Force rebuild of images when starting services
  --help           Show this message

Examples:
  ./scripts/deploy.sh up --env local
  ./scripts/deploy.sh up --env production --build
  ./scripts/deploy.sh restart ibbot
USAGE
}

log() {
    printf '[deploy] %s\n' "$*"
}

parse_args() {
    COMMAND=""
    ENVIRONMENT="${DEFAULT_ENVIRONMENT}"
    RUN_DIAGNOSTICS=1
    FORCE_BUILD=0
    POSITIONAL=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            up|refresh|down|restart|status|diagnose)
                COMMAND="$1"
                shift
                ;;
            --env)
                ENVIRONMENT="$2"
                shift 2
                ;;
            --no-diagnose)
                RUN_DIAGNOSTICS=0
                shift
                ;;
            --build)
                FORCE_BUILD=1
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            --)
                shift
                POSITIONAL+=("$@")
                break
                ;;
            *)
                if [[ -z "$COMMAND" ]]; then
                    POSITIONAL+=("$1")
                    shift
                else
                    POSITIONAL+=("$1")
                    shift
                fi
                ;;
        esac
    done

    if [[ -z "$COMMAND" ]]; then
        usage
        exit 1
    fi

    set -- "${POSITIONAL[@]}"
    REMAINING_ARGS=("$@")
}

compose_files_for_env() {
    local env_name="$1"
    case "$env_name" in
        local)
            printf '%s\n' "$COMPOSE_BASE_FILE"
            ;;
        production|prod)
            printf '%s\n%s\n' "$COMPOSE_BASE_FILE" "$COMPOSE_PROD_FILE"
            ;;
        *)
            log "Unsupported environment: $env_name"
            exit 1
            ;;
    esac
}

run_compose() {
    local env_name="$1"
    shift
    local files
    mapfile -t files < <(compose_files_for_env "$env_name")
    local cmd=("${COMPOSE_BIN}")
    for file in "${files[@]}"; do
        if [[ ! -f "$file" ]]; then
            log "Compose file not found: $file"
            exit 1
        fi
        cmd+=( -f "$file" )
    done
    cmd+=("$@")
    (cd "$ROOT_DIR" && "${cmd[@]}")
}

start_sequence() {
    local env_name="$1"
    local build_flag=()
    if [[ "$FORCE_BUILD" -eq 1 ]]; then
        build_flag+=(--build)
    fi

    STARTED_SERVICES=()

    local -a sequence=()
    mapfile -d '' -t sequence < <(filter_services "${START_SEQUENCE[@]}")

    for service in "${sequence[@]}"; do
        log "Starting ${service}"
        run_compose "$env_name" up -d "${build_flag[@]}" "$service"
        STARTED_SERVICES+=("$service")
    done
}

wait_for_health() {
    local env_name="$1"
    if [[ ${#STARTED_SERVICES[@]} -eq 0 ]]; then
        return
    fi
    local deadline=$((SECONDS + 180))
    while (( SECONDS < deadline )); do
        local output
        if ! output=$(run_compose "$env_name" ps --format json 2>/dev/null); then
            sleep 3
            continue
        fi
        if python - "$output" "${STARTED_SERVICES[@]}" <<'PYTHON'
import json
import sys

raw = sys.argv[1]
services = set(sys.argv[2:])
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    sys.exit(1)

states = {item.get("Service"): item for item in data if item.get("Service") in services}
if services.difference(states):
    sys.exit(1)

for svc, payload in states.items():
    state = (payload.get("State") or "").lower()
    health = (payload.get("Health") or "").lower()
    if state not in {"running", ""}:
        sys.exit(1)
    if health and health not in {"healthy", "starting"}:
        sys.exit(1)
    if state != "running":
        sys.exit(1)
    if health == "starting":
        sys.exit(1)
sys.exit(0)
PYTHON
        then
            sleep 5
            continue
        fi
        log "All services report healthy state"
        return
    done
    log "Timed out waiting for services to become healthy"
}

restart_service() {
    local env_name="$1"
    local service="${2:-}"
    if [[ -z "$service" ]]; then
        log "Restarting full stack"
        start_sequence "$env_name"
        return
    fi
    log "Restarting $service"
    if ! run_compose "$env_name" restart "$service"; then
        log "Restart command failed, attempting to start $service"
        run_compose "$env_name" up -d "$service"
    fi
}

run_diagnostics() {
    local env_name="$1"
    if [[ "$RUN_DIAGNOSTICS" -eq 0 ]]; then
        return
    fi
    log "Service status"
    run_compose "$env_name" ps
    log "Running foundation diagnostics"
    (cd "$ROOT_DIR" && python scripts/diagnose_foundation.py)
}

filter_services() {
    if [[ $# -eq 0 ]]; then
        return
    fi

    local service
    for service in "$@"; do
        if [[ "$service" == "cloudflared" && -z "${CF_TUNNEL_TOKEN:-}" ]]; then
            continue
        fi
        printf '%s\0' "$service"
    done
}

update_repository() {
    if [[ ! -d "${ROOT_DIR}/.git" ]]; then
        log "Skipping git update (no .git directory detected)"
        return
    fi

    if ! command -v git >/dev/null 2>&1; then
        log "Skipping git update (git command not available)"
        return
    fi

    if ! (
        set +e
        cd "$ROOT_DIR" || exit 1

        if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            exit 0
        fi

        if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
            log "Working tree has local changes; skip git pull to avoid overwriting"
            exit 0
        fi

        log "Pulling latest code (git pull --ff-only)"
        git pull --ff-only
    ); then
        log "git pull failed; continuing without updating repository"
    fi
}

refresh_stack() {
    local env_name="$1"

    update_repository

    local -a services=()
    mapfile -d '' -t services < <(filter_services "${REFRESH_SERVICES[@]}")

    if [[ ${#services[@]} -eq 0 ]]; then
        log "No services selected for refresh"
        return
    fi

    STARTED_SERVICES=("${services[@]}")

    log "Rebuilding and starting services (excluding ib-gateway): ${services[*]}"
    run_compose "$env_name" up -d --build "${services[@]}"
}

main() {
    parse_args "$@"

    case "$COMMAND" in
        up)
            start_sequence "$ENVIRONMENT"
            wait_for_health "$ENVIRONMENT"
            run_diagnostics "$ENVIRONMENT"
            ;;
        refresh)
            refresh_stack "$ENVIRONMENT"
            wait_for_health "$ENVIRONMENT"
            run_diagnostics "$ENVIRONMENT"
            ;;
        down)
            log "Stopping stack"
            run_compose "$ENVIRONMENT" down --remove-orphans
            ;;
        restart)
            restart_service "$ENVIRONMENT" "${REMAINING_ARGS[0]:-}"
            if [[ -z "${REMAINING_ARGS[0]:-}" ]]; then
                run_diagnostics "$ENVIRONMENT"
            fi
            ;;
        status)
            run_compose "$ENVIRONMENT" ps
            ;;
        diagnose)
            run_diagnostics "$ENVIRONMENT"
            ;;
        *)
            usage
            exit 1
            ;;
    esac
}

main "$@"
