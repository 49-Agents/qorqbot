#!/bin/bash
# Release gate: on a Linux desktop with a new home folder, the app must set itself up and show the sign-in screen,
# and quitting it must stop the Qorqbot server. Fails when it does not; screenshots, logs and summary.txt in the
# output folder show why.
#
#   first_open_check.sh APPIMAGE OUTPUT_FOLDER             the downloaded AppImage
#   first_open_check.sh --installed PROGRAM OUTPUT_FOLDER  the installed .deb (PROGRAM is /usr/bin/qorqbot-desktop)
#
# It needs an X display: run it under `xvfb-run -a` (it starts xvfb-run itself when DISPLAY is not set). It makes a
# new HOME, answers the AppImage's "add to applications menu" question with "decline" (QORQBOT_DESKTOP_INTEGRATION),
# opens the app, waits for the dashboard's sign-in screen through the app's inspection port, quits the app with
# SIGTERM and checks that the server stopped, then opens it a second time to check it starts again on the same port.
# For a self-contained archive it also hides the computer's Python, git, tmux and Node.js from the app (a stub in
# front of each on PATH, also in the login shell it reads PATH from) and checks the app runs on the Python and git it
# ships, and needs neither tmux nor Node.js.
#
# An AppImage is mounted with FUSE when this computer allows it, and unpacked and run from there otherwise
# (APPIMAGE_EXTRACT_AND_RUN=1). The Chromium sandbox follows the app's own rules: the AppImage's launcher turns it off
# where unprivileged user namespaces are blocked. In a container pass QORQBOT_CHECK_NO_SANDBOX=1 for --installed.
set -u

ORIGINAL_ARGS=("$@")
MODE=appimage
if [ "${1:-}" = --installed ]; then MODE=installed; shift; fi
TARGET="${1:?usage: first_open_check.sh [--installed] APPIMAGE_OR_PROGRAM OUTPUT_FOLDER}"
OUT="${2:?usage: first_open_check.sh [--installed] APPIMAGE_OR_PROGRAM OUTPUT_FOLDER}"
mkdir -p "$OUT" || exit 1
OUT="$(cd "$OUT" && pwd)"
TARGET="$(readlink -f "$TARGET")"
if [ -z "${DISPLAY:-}" ]; then
  command -v xvfb-run >/dev/null 2>&1 || { echo "FAILED: no display and no xvfb-run (sudo apt install xvfb)" | tee -a "$OUT/summary.txt"; exit 1; }
  exec xvfb-run -a -s "-screen 0 1280x900x24" "$0" "${ORIGINAL_ARGS[@]}"
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
INSPECTION_PORT="${QORQBOT_CHECK_INSPECTION_PORT:-9222}"
READY_LIMIT=120
FAILED=0
START_PATH="$PATH"

# A new home folder with a short path (Qorqbot's folder inside it must stay under 50 bytes).
CHECK_HOME="$(mktemp -d /tmp/qfo.XXXXXX)"
WORK="$CHECK_HOME/work"
mkdir -p "$WORK"
export HOME="$CHECK_HOME"
unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_STATE_HOME APPDIR APPIMAGE ARGV0 OWD
export SHELL=/bin/bash
ROOT="$HOME/Qorqbot/qorqbot"
LAUNCHER_LOG="$HOME/Qorqbot/launcher.log"

note() { printf '%s\n' "$*" | tee -a "$OUT/summary.txt"; }
section() { note ""; note "== $* =="; }
fail() { note "FAILED: $*"; FAILED=1; }
ready_count() { local count; count="$(grep -c "ready at" "$LAUNCHER_LOG" 2>/dev/null)"; echo "${count:-0}"; }
now() { date +%s.%N; }
since() { awk -v a="$1" -v b="$(now)" 'BEGIN { printf "%.1f", b - a }'; }
page() { ELECTRON_RUN_AS_NODE=1 "$ELECTRON" "$HERE/first_open_page.cjs" "$@"; }
port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

APP_PID=""
cleanup() {
  [ -n "$APP_PID" ] && kill -KILL "$APP_PID" 2>/dev/null
  [ -x "$ROOT/qorqbot" ] && "$ROOT/qorqbot" stop >>"$OUT/errors.txt" 2>&1
  pkill -KILL -f "$CHECK_HOME" 2>/dev/null
  rm -rf "$CHECK_HOME"
}
trap cleanup EXIT

section "Machine"
note "$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-Linux}"), $(uname -m), kernel $(uname -r)"
note "display $DISPLAY; check mode: $MODE; $(basename "$TARGET")"
if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null)" = 1 ]; then
  note "unprivileged user namespaces are restricted by AppArmor on this computer"
fi
if unshare -Ur true 2>/dev/null; then note "user namespaces usable: yes"; else note "user namespaces usable: no"; fi

# ---------------------------------------------------------------- what to run

if [ "$MODE" = appimage ]; then
  [ -f "$TARGET" ] && [ -x "$TARGET" ] || { fail "$TARGET is not an executable file"; exit 1; }
  section "Unpacking the AppImage"
  # Only to run its Electron in Node mode and read its files; the app itself starts from the AppImage below.
  if ( cd "$WORK" && "$TARGET" --appimage-extract >/dev/null 2>>"$OUT/errors.txt" ); then
    APP_DIR="$WORK/squashfs-root"
    ELECTRON="$APP_DIR/qorqbot-desktop"
    DESKTOP_FILE="$APP_DIR/qorqbot.desktop"
    note "unpacked $(du -sm "$APP_DIR" | cut -f 1) MB"
  else
    fail "the AppImage does not unpack (--appimage-extract)"
    exit 1
  fi
  if [ -c /dev/fuse ] && [ -w /dev/fuse ] && { command -v fusermount3 || command -v fusermount; } >/dev/null 2>&1 && [ "${QORQBOT_CHECK_FUSE:-1}" = 1 ]; then
    note "AppImage opened through FUSE ($(command -v fusermount3 || command -v fusermount))"
    LAUNCH=("$TARGET")
  else
    note "AppImage opened with APPIMAGE_EXTRACT_AND_RUN=1 (FUSE is not usable here, or QORQBOT_CHECK_FUSE=0)"
    export APPIMAGE_EXTRACT_AND_RUN=1
    LAUNCH=("$TARGET")
  fi
else
  ELECTRON="$(readlink -f "$TARGET")"
  APP_DIR="$(dirname "$ELECTRON")"
  DESKTOP_FILE=/usr/share/applications/qorqbot.desktop
  [ -x "$ELECTRON" ] || { fail "$TARGET is not an executable program"; exit 1; }
  LAUNCH=("$TARGET")
  [ "${QORQBOT_CHECK_NO_SANDBOX:-0}" = 1 ] && LAUNCH+=(--no-sandbox)
fi
RESOURCES="$APP_DIR/resources"
ARCHIVE="$(ls "$RESOURCES"/qorqbot-payload/qorqbot-*-ubuntu24-x86_64.tar.gz 2>/dev/null | head -n 1)"
[ -n "$ARCHIVE" ] || { fail "the app carries no Qorqbot archive in $RESOURCES/qorqbot-payload"; exit 1; }

# ---------------------------------------------------------------- the programs the app may use

SELF_CONTAINED=0
tar -xzOf "$ARCHIVE" qorqbot/runtime/PAYLOAD.json 2>/dev/null | grep -q '"bundled"' && SELF_CONTAINED=1
APP_PATH="$START_PATH"
if [ "$SELF_CONTAINED" = 1 ]; then
  section "Hiding the computer's developer programs"
  BLOCKED="$WORK/hidden-programs"
  mkdir -p "$BLOCKED"
  for program in python python3 python3.12 pip pip3 tmux git node npm; do
    printf '#!/bin/sh\necho "%s is hidden during the first-open check" >&2\nexit 127\n' "$program" >"$BLOCKED/$program"
    chmod +x "$BLOCKED/$program"
  done
  # The app also reads PATH from the login shell, which starts over from the system's list; put the stubs first there too.
  printf 'export PATH="%s:$PATH"\n' "$BLOCKED" >"$HOME/.profile"
  APP_PATH="$BLOCKED:$START_PATH"
  for program in python3.12 tmux git node; do
    note "  $program: $(PATH="$APP_PATH" command -v "$program") (a stub; the computer has $(PATH="$START_PATH" command -v "$program" || echo "none"))"
  done
else
  note "the archive is not self-contained: the computer's Python 3.12 and git stay available to the app"
fi

# ---------------------------------------------------------------- first open

launch() {
  ( cd "$HOME" && exec env PATH="$APP_PATH" QORQBOT_DESKTOP_INTEGRATION=decline "${LAUNCH[@]}" \
      "--remote-debugging-port=$INSPECTION_PORT" '--remote-allow-origins=*' ) >>"$OUT/app-output.log" 2>&1 &
  APP_PID=$!
}

wait_ready() {  # wait_ready READY_COUNT_BEFORE SECONDS_LIMIT
  local started; started="$(now)"
  while [ "$(since "$started" | cut -d . -f 1)" -lt "$2" ]; do
    [ "$(ready_count)" -gt "$1" ] && { since "$started"; return 0; }
    kill -0 "$APP_PID" 2>/dev/null || return 1
    sleep 0.5
  done
  return 1
}

section "First open"
OPENED="$(now)"
launch
sleep 4
page shots "$INSPECTION_PORT" "$OUT/01-setting-up" >>"$OUT/errors.txt" 2>&1 && note "screenshots: 01-setting-up-window-*.png"
if READY="$(wait_ready 0 "$READY_LIMIT")"; then
  note "ready $(since "$OPENED") seconds after the first open"
  note "app log: $(grep -m 1 'Qorqbot app' "$LAUNCHER_LOG" | cut -d ' ' -f 3-)"
else
  fail "Qorqbot was not ready $READY_LIMIT seconds after the first open"
fi

section "What the window shows"
if SIGN_IN="$(page sign-in "$INSPECTION_PORT" 60 "$OUT/02" 2>>"$OUT/errors.txt")"; then
  note "$SIGN_IN"
  note "screenshot: 02-sign-in.png"
else
  fail "the window does not show the sign-in screen"
  note "$SIGN_IN"
  page shots "$INSPECTION_PORT" "$OUT/02-failure" >>"$OUT/errors.txt" 2>&1
fi
if command -v scrot >/dev/null 2>&1; then scrot -z "$OUT/02-screen.png" 2>>"$OUT/errors.txt" && note "screenshot: 02-screen.png"; fi

section "Desktop entry"
EXPECTED_CLASS="$(sed -n 's/^StartupWMClass=//p' "$DESKTOP_FILE" 2>/dev/null | head -n 1)"
note "StartupWMClass in $(basename "$DESKTOP_FILE"): ${EXPECTED_CLASS:-missing}"
if command -v xwininfo >/dev/null 2>&1; then
  CLASSES="$(xwininfo -root -tree 2>/dev/null | sed -n 's/.*: (\("[^"]*" "[^"]*"\)).*/\1/p' | sort -u | tr '\n' ' ')"
  note "window classes on the display: ${CLASSES:-none}"
  case "$CLASSES" in
    *"\"$EXPECTED_CLASS\""*) note "a window has the class $EXPECTED_CLASS, so the desktop can match it to the menu entry" ;;
    *) fail "no window has the class $EXPECTED_CLASS (the desktop entry's StartupWMClass)" ;;
  esac
else
  note "xwininfo is not installed (sudo apt install x11-utils): window class not checked"
fi

if [ "$SELF_CONTAINED" = 1 ]; then
  section "Programs Qorqbot runs on"
  SERVICE="$(ps -axo command | grep -F "$ROOT" | grep -m 1 'qorqut.service' | cut -d ' ' -f 1)"
  note "  service runs on: ${SERVICE:-not running}"
  case "$SERVICE" in "$ROOT"/runtime/venv/bin/*) ;; *) fail "the service does not run on the Python inside $ROOT" ;; esac
  for tool in git agent-browser; do
    program="$ROOT/runtime/native/bin/$tool"
    if version="$("$program" --version 2>&1 | head -n 1)"; then note "  $tool: $version"; else fail "$program does not run: $version"; fi
  done
  "$ROOT/runtime/native/bin/git" -C "$ROOT/instance/operations" log --oneline -1 >>"$OUT/errors.txt" 2>&1 \
    || fail "the records folder was not created with the shipped git"
fi

# ---------------------------------------------------------------- quit

quit_app() {  # quit_app LABEL: ask the main process to quit and wait for it and the server to go
  local label="$1" pid="" port="" waited
  pid="$(page pid "$INSPECTION_PORT" 2>>"$OUT/errors.txt")"
  port="$(tr -dc 0-9 <"$HOME/Qorqbot/server-port" 2>/dev/null)"
  if [ -z "$pid" ]; then fail "$label: the app's main process was not found"; return 1; fi
  note "$label: the app's main process is $pid, the server's port is ${port:-unknown}"
  kill -TERM "$pid"
  waited=0
  while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 60 ]; do sleep 1; waited=$((waited + 1)); done
  if kill -0 "$pid" 2>/dev/null; then
    fail "$label: the app was still running $waited seconds after SIGTERM"
    kill -KILL "$pid" 2>/dev/null
  else
    note "$label: the app quit after $waited seconds"
  fi
  wait "$APP_PID" 2>/dev/null
  APP_PID=""
  status="$("$ROOT/qorqbot" status 2>&1)"; code=$?
  note "$label: ./qorqbot status says: $status"
  if [ "$code" = 0 ]; then fail "$label: the server is still running after the app quit"; fi
  if [ -n "$port" ] && port_open "$port"; then fail "$label: port $port still answers after the app quit"; fi
  # (only this check's own folder: other Qorqbot installations may be running on the computer)
  if ps -axo command | grep -F "$ROOT" | grep -q 'qorqut.service'; then fail "$label: a Qorqbot service process is still running"; fi
}

section "Quit"
FIRST_PORT="$(sed -n 's/.*ready at http:\/\/127.0.0.1:\([0-9]*\).*/\1/p' "$LAUNCHER_LOG" | tail -n 1)"
quit_app "first open"

section "Second open"
BEFORE="$(ready_count)"
REOPENED="$(now)"
launch
if wait_ready "$BEFORE" "$READY_LIMIT" >/dev/null; then
  note "ready $(since "$REOPENED") seconds after the second open"
else
  fail "Qorqbot was not ready after the second open"
fi
SECOND_PORT="$(sed -n 's/.*ready at http:\/\/127.0.0.1:\([0-9]*\).*/\1/p' "$LAUNCHER_LOG" | tail -n 1)"
if [ -n "$FIRST_PORT" ] && [ "$FIRST_PORT" = "$SECOND_PORT" ]; then note "the server came back on port $SECOND_PORT"; else fail "the server moved from port ${FIRST_PORT:-?} to ${SECOND_PORT:-?}"; fi
if page sign-in "$INSPECTION_PORT" 60 "$OUT/03" >>"$OUT/summary.txt" 2>>"$OUT/errors.txt"; then note "screenshot: 03-sign-in.png"; else fail "the second window does not show the sign-in screen"; fi
quit_app "second open"

section "What the app left in the home folder"
(cd "$HOME" && find . -maxdepth 2 -not -path './work*' -not -path './Qorqbot/qorqbot/*' -not -path './.config/*/*' | sort | sed 's/^\.\//  /' | tail -n +2) | tee -a "$OUT/summary.txt"

section "Result"
cp "$LAUNCHER_LOG" "$OUT/launcher.log" 2>/dev/null
[ -f "$ROOT/instance/logs/application.log" ] && tail -n 200 "$ROOT/instance/logs/application.log" >"$OUT/application.log"
if [ "$FAILED" = 0 ]; then note "passed"; else note "failed"; fi
exit "$FAILED"
