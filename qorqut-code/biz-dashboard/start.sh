#!/usr/bin/env bash
# The one way to run biz-dashboard on a machine: start | stop | restart | status | logs.
#
# Why a script: the dashboard must start with a clean environment. Started by hand from inside a
# Claude Code session it can inherit CLAUDE_CODE_* markers. This script builds the environment from
# scratch (`env -i`), reads the machine's config from one file, detaches the process (setsid) and
# keeps a pidfile and a log under this app's ignored .state/ directory.
#
# Config: .env.dashboard next to this script (gitignored; copy .env.dashboard.example). Plain
# KEY=VALUE lines. HOST and PORT become --host/--port. INTERPRETER, WORKSPACE, INSTANCE_ROOT and
# COMPONENT_<NAME> are the launcher's explicit host selection (below); they become arguments and are never
# passed on as environment. Everything else is passed as environment (QORQUT_TERMINAL_WORKSPACE,
# BIZ_SERVICE_ORIGINS, DEEPSEEK_API_KEY, ...). The operational database must already exist. This script never
# migrates data and never searches a sibling checkout, a default interpreter or an old businesses root.
#
# Explicit host selection (every one required; absolute, normalized paths; nothing is discovered):
#   INTERPRETER (or BIZ_DASH_PYTHON)   the Python of the installed host, used with -I -m qorqut.service;
#                                      not needed when BIZ_DASH_LAUNCHER names the stable application launcher
#   WORKSPACE (or BIZ_DASH_WORKSPACE)  the protected configuration / package-source workspace
#   INSTANCE_ROOT                      the instance root holding operations/
#   COMPONENT_APP, COMPONENT_DASHBOARD   required component locations
#   COMPONENT_OPERATIONS_ENGINE, COMPONENT_CONTEXT_ENGINE, COMPONENT_ROLES   optional component locations
# `start.sh argv` prints the exact launch command, one element per line, without starting anything.
#
# `stop` ends the dashboard only. Agent shells live in tmux (`tmux -L qorqut`) and keep running;
# the next `start` re-attaches to them. Kill one on purpose with the Stop button or
# `tmux -L qorqut kill-session -t =<slug>__<agent>`.
set -euo pipefail
umask 077

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP_LAUNCHER=${BIZ_DASH_LAUNCHER:-}
CONF=${BIZ_DASH_CONF:-$HERE/.env.dashboard}
STATE=${BIZ_DASH_STATE:-$HERE/.state}
PIDFILE=$STATE/dashboard.pid
LOG=$STATE/dashboard.log

die() { echo "start.sh: $*" >&2; exit 1; }

COMPONENT_NAMES=(app operations_engine context_engine dashboard roles)
REQUIRED_COMPONENTS=(app dashboard)
declare -A COMPONENTS=()

component_known() {
  local name
  for name in "${COMPONENT_NAMES[@]}"; do [ "$name" = "$1" ] && return 0; done
  return 1
}

require_absolute() {  # LABEL VALUE: an absolute, normalized path; nothing is resolved or searched
  local label=$1 value=$2
  case $value in /*) ;; *) die "$label must be an absolute path (got '$value')" ;; esac
  [ "$value" != / ] || die "$label must not be the filesystem root"
  case "$value/" in */../*|*/./*|*//*) die "$label must be a normalized absolute path (got '$value')" ;; esac
}

read_conf() {
  [ -f "$CONF" ] || die "no config at $CONF (copy .env.dashboard.example and fill it in)"
  HOST=127.0.0.1; PORT=1049; ENV_ARGS=()
  CONF_INTERPRETER=""; CONF_WORKSPACE=""; INSTANCE_ROOT=""; COMPONENTS=()
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%%#*}; line=${line#"${line%%[![:space:]]*}"}; line=${line%"${line##*[![:space:]]}"}
    [ -z "$line" ] && continue
    line=${line#export }
    key=${line%%=*}; val=${line#*=}
    [[ $key =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "bad key in $CONF: $key"
    val=${val#\"}; val=${val%\"}; val=${val#\'}; val=${val%\'}
    case $key in
      HOST) HOST=$val ;;
      PORT) PORT=$val ;;
      INTERPRETER) CONF_INTERPRETER=$val ;;
      WORKSPACE) CONF_WORKSPACE=$val ;;
      INSTANCE_ROOT) INSTANCE_ROOT=$val ;;
      COMPONENT_*)
        name=$(printf '%s' "${key#COMPONENT_}" | tr '[:upper:]' '[:lower:]')
        component_known "$name" || die "unknown component in $CONF: $key (known: ${COMPONENT_NAMES[*]})"
        [ -z "${COMPONENTS[$name]:-}" ] || die "component $name is selected more than once in $CONF"
        COMPONENTS[$name]=$val ;;
      BIZ_ROOT|BIZ_DIRS) die "$key is retired and no longer selects anything; set INSTANCE_ROOT in $CONF" ;;
      *) ENV_ARGS+=("$key=$val") ;;
    esac
  done < "$CONF"
}

# Build COMMAND from the explicit selection only. Refuses a missing or malformed selection; never defaults,
# derives from this script's location or looks for a sibling checkout.
resolve_launch() {
  local name interpreter workspace
  interpreter=${BIZ_DASH_PYTHON:-$CONF_INTERPRETER}
  workspace=${BIZ_DASH_WORKSPACE:-$CONF_WORKSPACE}
  [ -n "$workspace" ] || die "no workspace selected (set WORKSPACE in $CONF or BIZ_DASH_WORKSPACE)"
  require_absolute WORKSPACE "$workspace"
  [ -n "$INSTANCE_ROOT" ] || die "no instance root selected (set INSTANCE_ROOT in $CONF)"
  require_absolute INSTANCE_ROOT "$INSTANCE_ROOT"
  for name in "${REQUIRED_COMPONENTS[@]}"; do
    [ -n "${COMPONENTS[$name]:-}" ] || die "no $name component selected (set COMPONENT_${name^^} in $CONF)"
  done
  [[ $PORT =~ ^[0-9]{1,5}$ ]] || die "PORT must be a number (got '$PORT')"
  [ -n "$HOST" ] || die "HOST must not be empty"
  LAUNCH_ARGS=(--workspace "$workspace" --instance-root "$INSTANCE_ROOT")
  for name in "${COMPONENT_NAMES[@]}"; do
    [ -n "${COMPONENTS[$name]:-}" ] || continue
    require_absolute "COMPONENT_${name^^}" "${COMPONENTS[$name]}"
    LAUNCH_ARGS+=(--component "$name=${COMPONENTS[$name]}")
  done
  LAUNCH_ARGS+=(--host "$HOST" --port "$PORT")
  if [ -n "$APP_LAUNCHER" ]; then
    require_absolute BIZ_DASH_LAUNCHER "$APP_LAUNCHER"
    COMMAND=("$APP_LAUNCHER" "${LAUNCH_ARGS[@]}")
  else
    [ -n "$interpreter" ] || die "no host interpreter selected (set INTERPRETER in $CONF or BIZ_DASH_PYTHON); no sibling checkout is searched"
    require_absolute INTERPRETER "$interpreter"
    COMMAND=("$interpreter" -I -u -m qorqut.service "${LAUNCH_ARGS[@]}")
  fi
}

running_pid() {
  local pid
  if [ -f "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "$pid"; return 0; fi
  fi
  return 1
}

cmd_argv() {
  read_conf
  resolve_launch
  printf '%s\n' "${COMMAND[@]}"
}

cmd_start() {
  read_conf
  resolve_launch
  if pid=$(running_pid); then echo "already running (pid $pid) on $HOST:$PORT"; return 0; fi
  [ -x "${COMMAND[0]}" ] || die "the selected host runtime is not executable: ${COMMAND[0]}"
  mkdir -p "$STATE"
  # A fresh environment: nothing from the caller but the basics. No CLAUDE_* can get through.
  env -i \
    HOME="$HOME" USER="${USER:-$(id -un)}" LOGNAME="${LOGNAME:-$(id -un)}" PATH="$PATH" \
    SHELL="${SHELL:-/bin/bash}" LANG="${LANG:-C.UTF-8}" TERM=xterm-256color \
    "${ENV_ARGS[@]}" \
    setsid nohup "${COMMAND[@]}" \
      >> "$LOG" 2>&1 < /dev/null &
  echo $! > "$PIDFILE"
  sleep 1.5
  if pid=$(running_pid); then
    echo "biz-dashboard started (pid $pid) on http://$HOST:$PORT  log: $LOG"
  else
    echo "failed to start; last log lines:" >&2; tail -n 5 "$LOG" >&2; rm -f "$PIDFILE"; exit 1
  fi
}

cmd_stop() {
  if ! pid=$(running_pid); then echo "not running"; rm -f "$PIDFILE"; return 0; fi
  kill "$pid"
  for _ in {1..40}; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  if kill -0 "$pid" 2>/dev/null; then die "graceful shutdown timed out (pid $pid); inspect before recovery"; fi
  rm -f "$PIDFILE"
  echo "stopped (pid $pid). Agent shells in tmux keep running; 'start' re-attaches."
}

cmd_foreground() {
  read_conf
  resolve_launch
  mkdir -p "$STATE"
  if [ -n "$APP_LAUNCHER" ]; then
    [ -x "$APP_LAUNCHER" ] || die "stable application launcher unavailable"
  else
    [ -x "${COMMAND[0]}" ] || die "configured interpreter unavailable"
  fi
  exec env -i \
    HOME="$HOME" USER="${USER:-$(id -un)}" LOGNAME="${LOGNAME:-$(id -un)}" PATH="$PATH" \
    SHELL="${SHELL:-/bin/bash}" LANG="${LANG:-C.UTF-8}" TERM=xterm-256color \
    "${ENV_ARGS[@]}" BIZ_DASH_STATE="$STATE" "${COMMAND[@]}"
}

cmd_status() {
  if pid=$(running_pid); then echo "running (pid $pid)"; else echo "not running"; fi
  if command -v tmux >/dev/null; then
    echo "agent shells (tmux -L qorqut):"
    tmux -L qorqut list-sessions -F '  #{session_name}  #{pane_current_command}  #{pane_current_path}' 2>/dev/null || echo "  none"
  fi
}

case ${1:-} in
  argv) cmd_argv ;;
  start) cmd_start ;;
  foreground) cmd_foreground ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  logs) mkdir -p "$STATE"; touch "$LOG"; tail -n "${2:-50}" -f "$LOG" ;;
  *) echo "usage: $0 argv|foreground|start|stop|restart|status|logs [n]"; exit 2 ;;
esac
