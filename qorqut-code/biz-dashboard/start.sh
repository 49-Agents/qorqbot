#!/usr/bin/env bash
# The one way to run biz-dashboard on a machine: start | stop | restart | status | logs.
#
# Why a script: the dashboard must start with a clean environment. Started by hand from inside a
# Claude Code session it can inherit CLAUDE_CODE_* markers. This script builds the environment from
# scratch (`env -i`), reads the machine's config from one file, detaches the process (setsid) and
# keeps a pidfile and a log under this app's ignored .state/ directory.
#
# Config: .env.dashboard next to this script (gitignored; copy .env.dashboard.example). Plain
# KEY=VALUE lines. HOST and PORT become --host/--port; everything else is passed as environment
# (BIZ_ROOT, BIZ_SERVICE_ORIGINS, DEEPSEEK_API_KEY, BIZ_DASH_TOKEN, ...).
# The registered database and authority marker must already exist. This script never migrates data.
#
# `stop` ends the dashboard only. Agent shells live in tmux (`tmux -L bazarlyk`) and keep running;
# the next `start` re-attaches to them. Kill one on purpose with the Stop button or
# `tmux -L bazarlyk kill-session -t =<slug>__<agent>`.
set -euo pipefail
umask 077

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PYTHON=$HERE/../biz-core/.venv/bin/python
CONF=${BIZ_DASH_CONF:-$HERE/.env.dashboard}
STATE=${BIZ_DASH_STATE:-$HERE/.state}
PIDFILE=$STATE/dashboard.pid
LOG=$STATE/dashboard.log

die() { echo "start.sh: $*" >&2; exit 1; }

read_conf() {
  [ -f "$CONF" ] || die "no config at $CONF (copy .env.dashboard.example and fill it in)"
  HOST=127.0.0.1; PORT=1049; ENV_ARGS=()
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
      *) ENV_ARGS+=("$key=$val") ;;
    esac
  done < "$CONF"
}

running_pid() {
  local pid
  if [ -f "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo "$pid"; return 0; fi
  fi
  return 1
}

cmd_start() {
  read_conf
  if pid=$(running_pid); then echo "already running (pid $pid) on $HOST:$PORT"; return 0; fi
  [ -x "$PYTHON" ] || die "project runtime missing; install biz-core into biz-core/.venv first"
  mkdir -p "$STATE"
  # A fresh environment: nothing from the caller but the basics. No CLAUDE_* can get through.
  env -i \
    HOME="$HOME" USER="${USER:-$(id -un)}" LOGNAME="${LOGNAME:-$(id -un)}" PATH="$PATH" \
    SHELL="${SHELL:-/bin/bash}" LANG="${LANG:-C.UTF-8}" TERM=xterm-256color \
    "${ENV_ARGS[@]}" \
    setsid nohup "$PYTHON" -I -u -m bazarlyk.service --workspace "$HERE/.." --host "$HOST" --port "$PORT" \
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
  kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
  rm -f "$PIDFILE"
  echo "stopped (pid $pid). Agent shells in tmux keep running; 'start' re-attaches."
}

cmd_status() {
  if pid=$(running_pid); then echo "running (pid $pid)"; else echo "not running"; fi
  if command -v tmux >/dev/null; then
    echo "agent shells (tmux -L bazarlyk):"
    tmux -L bazarlyk list-sessions -F '  #{session_name}  #{pane_current_command}  #{pane_current_path}' 2>/dev/null || echo "  none"
  fi
}

case ${1:-} in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  logs) mkdir -p "$STATE"; touch "$LOG"; tail -n "${2:-50}" -f "$LOG" ;;
  *) echo "usage: $0 start|stop|restart|status|logs [n]"; exit 2 ;;
esac
