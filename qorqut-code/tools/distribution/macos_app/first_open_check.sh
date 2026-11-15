#!/bin/bash
# Release gate: on a Mac with none of the developer programs, the app from the disk image must set itself up and
# show the sign-in screen. Fails when it does not; screenshots, logs and summary.txt in the output folder show why.
#
#   first_open_check.sh DISK_IMAGE OUTPUT_FOLDER
#
# It takes Homebrew, Python, tmux, Node.js and Apple's developer tools away for the length of the check and puts
# them back afterwards (needs sudo without a password, as on GitHub's Macs). macOS's warning for a downloaded app
# is not part of this check: the app is opened as an approved app.
set -u

DMG="${1:?usage: first_open_check.sh DISK_IMAGE OUTPUT_FOLDER}"
OUT="${2:?usage: first_open_check.sh DISK_IMAGE OUTPUT_FOLDER}"
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/Qorqbot.app"
ROOT="$HOME/Qorqbot/qorqbot"
LAUNCHER_LOG="$HOME/Qorqbot/launcher.log"
INSPECTION_PORT=9222
READY_LIMIT=90
FAILED=0
mkdir -p "$OUT"

note() { printf '%s\n' "$*" | tee -a "$OUT/summary.txt"; }
section() { note ""; note "== $* =="; }
fail() { note "FAILED: $*"; FAILED=1; }
shot() { screencapture -x -t jpg "$OUT/$1.jpg" 2>>"$OUT/errors.txt" && note "screenshot: $1.jpg" || note "screenshot not taken: $1"; }
now() { /usr/bin/perl -MTime::HiRes=time -e 'printf "%.1f", time'; }
since() { /usr/bin/perl -e 'printf "%.1f", $ARGV[1] - $ARGV[0]' "$1" "$(now)"; }
# What the app itself would find: it reads PATH from the login shell.
login_which() {
  /bin/zsh -ilc "printf '__W__%s__W__\\n' \"\$(command -v $1)\"" 2>/dev/null \
    | sed -n 's/.*__W__\(.*\)__W__.*/\1/p' | tail -n 1
}

HIDDEN=()
hide() { if [ -e "$1" ] && sudo mv "$1" "$1.hidden"; then HIDDEN+=("$1"); fi; }
restore() {
  for (( i=${#HIDDEN[@]}-1; i>=0; i-- )); do sudo mv "${HIDDEN[$i]}.hidden" "${HIDDEN[$i]}"; done
  HIDDEN=()
}
trap restore EXIT

section "Machine"
note "macOS $(sw_vers -productVersion) ($(sw_vers -buildVersion)), $(uname -m)"
[ -e "$HOME/Qorqbot" ] && { fail "$HOME/Qorqbot already exists; this check needs a Mac that never ran Qorqbot"; exit 1; }

section "Taking the developer programs away"
hide /opt/homebrew
hide /usr/local/bin
hide /usr/local/Homebrew
hide /Library/Developer/CommandLineTools
for xcode in /Applications/Xcode*.app; do hide "$xcode"; done
for tool in python3.12 python3 tmux brew node git; do
  for _ in 1 2 3 4 5; do
    found="$(login_which "$tool")"
    case "$found" in ''|/usr/bin/*|/bin/*) break ;; esac
    [ -e "$found" ] || break
    hide "$found"
  done
done
for tool in brew python3.12 tmux node; do
  found="$(login_which "$tool")"
  note "  $tool: ${found:-not found}"
  [ -z "$found" ] || fail "$tool is still available at $found"
done
if developer="$(xcode-select -p 2>/dev/null)" && [ -e "$developer" ]; then
  fail "Apple's developer tools are still available at $developer"
else
  note "  Apple's developer tools (git, python3 in /usr/bin): not installed"
fi

section "Install: open the disk image, copy the app to Applications"
MOUNT="$(mktemp -d)"
if hdiutil attach -readonly -nobrowse -mountpoint "$MOUNT" "$DMG" >>"$OUT/errors.txt" 2>&1; then
  ditto "$MOUNT/Qorqbot.app" "$APP"
  hdiutil detach "$MOUNT" >>"$OUT/errors.txt" 2>&1
  xattr -dr com.apple.quarantine "$APP" 2>/dev/null
  note "copied $(basename "$DMG") ($(stat -f %z "$DMG") bytes) to $APP"
else
  fail "the disk image could not be opened"
  exit 1
fi

section "First open"
OPENED="$(now)"
open "$APP" --args "--remote-debugging-port=$INSPECTION_PORT" '--remote-allow-origins=*' >>"$OUT/errors.txt" 2>&1 &
sleep 2
shot 01-setting-up
READY=""
while [ "$(since "$OPENED" | cut -d . -f 1)" -lt "$READY_LIMIT" ]; do
  if grep -q "ready at" "$LAUNCHER_LOG" 2>/dev/null; then READY="$(since "$OPENED")"; break; fi
  sleep 0.5
done
if [ -n "$READY" ]; then
  note "ready $READY seconds after opening the app"
else
  fail "Qorqbot was not ready $READY_LIMIT seconds after opening the app"
fi

section "What the window shows"
PAGE="$(ELECTRON_RUN_AS_NODE=1 "$APP/Contents/MacOS/Qorqbot" "$HERE/first_open_page.cjs" "$INSPECTION_PORT" 60 2>>"$OUT/errors.txt")" \
  || fail "the window does not show the sign-in screen"
note "$PAGE"
note "sign-in screen $(since "$OPENED") seconds after opening the app"
shot 02-sign-in

section "Programs Qorqbot runs on"
SERVICE="$(ps -axo command | grep -m 1 '[q]orqut.service' | cut -d ' ' -f 1)"
note "  service runs on: ${SERVICE:-not running}"
case "$SERVICE" in "$ROOT"/runtime/venv/bin/*) ;; *) fail "the service does not run on the Python inside $ROOT" ;; esac
for tool in tmux git agent-browser; do
  program="$ROOT/runtime/native/bin/$tool"
  flag="--version"; [ "$tool" = tmux ] && flag="-V"
  if version="$("$program" "$flag" 2>&1 | head -n 1)"; then note "  $tool: $version"; else fail "$program does not run: $version"; fi
done
"$ROOT/runtime/native/bin/git" -C "$ROOT/instance/operations" log --oneline -1 >>"$OUT/errors.txt" 2>&1 \
  || fail "the records folder was not created with the shipped git"
# A terminal session on the shipped tmux, as agent terminals use it.
if TERM=xterm-256color "$ROOT/runtime/native/bin/tmux" -L first-open-check new-session -d 'sleep 30' 2>>"$OUT/errors.txt" \
    && "$ROOT/runtime/native/bin/tmux" -L first-open-check list-sessions >>"$OUT/errors.txt" 2>&1; then
  note "  a tmux session starts"
else
  fail "a tmux session does not start"
fi
"$ROOT/runtime/native/bin/tmux" -L first-open-check kill-server 2>/dev/null

section "Result"
cp "$LAUNCHER_LOG" "$OUT/launcher.log" 2>/dev/null
[ -f "$ROOT/instance/logs/application.log" ] && tail -n 200 "$ROOT/instance/logs/application.log" >"$OUT/application.log"
pkill -x Qorqbot 2>/dev/null
"$ROOT/qorqbot" stop >>"$OUT/errors.txt" 2>&1
restore
if [ "$FAILED" = 0 ]; then note "passed"; else note "failed"; fi
exit "$FAILED"
