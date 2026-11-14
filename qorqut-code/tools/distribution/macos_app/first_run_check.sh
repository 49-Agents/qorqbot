#!/bin/bash
# First-run check: record what a person sees who downloads the published
# Qorqbot DMG onto a Mac and opens it. It observes and never fails the job;
# the screenshots, logs and summary.txt in the output folder are the result.
#
#   first_run_check.sh OUTPUT_FOLDER
#
# Needs GH_TOKEN and GITHUB_REPOSITORY (set by GitHub Actions) for the download.
set -u

OUT="${1:?usage: first_run_check.sh OUTPUT_FOLDER}"
WORK="$(mktemp -d)"
APP="/Applications/Qorqbot.app"
LAUNCHER_LOG="$HOME/Qorqbot/launcher.log"
mkdir -p "$OUT"

note() { printf '%s\n' "$*" | tee -a "$OUT/summary.txt"; }
section() { note ""; note "== $* =="; }
shot() {
  if screencapture -x -t jpg "$OUT/$1.jpg" 2>>"$OUT/errors.txt"; then
    note "screenshot: $1.jpg"
  else
    note "screenshot FAILED: $1"
  fi
}
# What the app itself would find: it reads PATH from the login shell.
login_which() {
  /bin/zsh -ilc "printf '__W__%s__W__\\n' \"\$(command -v $1)\"" 2>/dev/null \
    | sed -n 's/.*__W__\(.*\)__W__.*/\1/p' | tail -n 1
}
tools_report() {
  for tool in brew python3.12 git tmux; do
    found="$(login_which "$tool")"
    note "  $tool: ${found:-not found}"
  done
}
save_launcher_log() { [ -f "$LAUNCHER_LOG" ] && cp "$LAUNCHER_LOG" "$OUT/$1"; }

section "Machine"
note "macOS $(sw_vers -productVersion) ($(sw_vers -buildVersion)), $(uname -m)"
note "Programs as this machine comes:"
tools_report

section "Download"
if gh release download --repo "$GITHUB_REPOSITORY" --pattern '*.dmg' --dir "$WORK" 2>>"$OUT/errors.txt"; then
  DMG="$(ls "$WORK"/*.dmg | head -n 1)"
  note "downloaded $(basename "$DMG") ($(stat -f %z "$DMG") bytes)"
else
  note "download FAILED"
  exit 0
fi
# A browser marks every download as quarantined; do the same.
xattr -w com.apple.quarantine "0083;$(printf '%x' "$(date +%s)");Safari;$(uuidgen)" "$DMG"

section "Install: open the DMG, drag the app to Applications"
MOUNT="$WORK/mounted"
mkdir "$MOUNT"
if hdiutil attach -readonly -nobrowse -mountpoint "$MOUNT" "$DMG" >>"$OUT/errors.txt" 2>&1; then
  ditto "$MOUNT/Qorqbot.app" "$APP"
  hdiutil detach "$MOUNT" >>"$OUT/errors.txt" 2>&1
else
  note "mounting the DMG FAILED"
  exit 0
fi
if xattr -p com.apple.quarantine "$APP" >/dev/null 2>&1; then
  note "the installed app carries the download quarantine mark"
else
  note "the copy lost the quarantine mark; setting it as Finder would have kept it"
  xattr -r -w com.apple.quarantine "0083;$(printf '%x' "$(date +%s)");Safari;$(uuidgen)" "$APP"
fi
note "Gatekeeper verdict: $(spctl --assess --type execute -vv "$APP" 2>&1 | tr '\n' ' ')"
note "app architecture: $(lipo -archs "$APP/Contents/MacOS/Qorqbot" 2>&1)"

section "First open, straight after download"
note "open: $(open "$APP" 2>&1 || true)"
sleep 12
shot 01-first-open
note "app running: $(pgrep -x Qorqbot >/dev/null && echo yes || echo no)"
open "x-apple.systempreferences:com.apple.preference.security?General" 2>>"$OUT/errors.txt"
sleep 10
shot 02-privacy-and-security
killall "System Settings" 2>/dev/null
killall CoreServicesUIAgent 2>/dev/null
pkill -x Qorqbot 2>/dev/null
sleep 2

if [ "$(uname -m)" != "arm64" ]; then
  note ""
  note "This is not an Apple silicon Mac; the published app is built for Apple silicon only. Stopping here."
  exit 0
fi

section "After approving the app (stands in for Open Anyway)"
xattr -dr com.apple.quarantine "$APP"
# Take the developer programs away, as on a Mac that never had them.
# /usr/bin/git cannot be removed here; on a new Mac it is a stub that asks to
# install Apple's command line tools the first time it runs.
HIDDEN=()
hide() {
  if [ -e "$1" ] && sudo mv "$1" "$1.hidden"; then HIDDEN+=("$1"); fi
}
hide /opt/homebrew
hide /usr/local/bin
for tool in python3.12 tmux brew; do
  for _ in 1 2 3 4 5; do
    found="$(login_which "$tool")"
    [ -n "$found" ] && [ -e "$found" ] || break
    hide "$found"
  done
done
note "Programs with the developer tools taken away:"
tools_report
open "$APP" 2>>"$OUT/errors.txt"
sleep 30
shot 03-open-without-developer-programs
save_launcher_log 03-launcher.log

section "Doing what the screen asks: install the missing programs"
for (( i=${#HIDDEN[@]}-1; i>=0; i-- )); do
  sudo mv "${HIDDEN[$i]}.hidden" "${HIDDEN[$i]}"
done
MISSING=()
[ -n "$(login_which python3.12)" ] || MISSING+=(python@3.12)
[ -n "$(login_which tmux)" ] || MISSING+=(tmux)
if [ "${#MISSING[@]}" -gt 0 ]; then
  START="$(date +%s)"
  note "running: brew install ${MISSING[*]}"
  brew install "${MISSING[@]}" >>"$OUT/brew-install.log" 2>&1
  note "brew install exit $? after $(( $(date +%s) - START )) seconds"
else
  note "nothing left to install once the developer tools are back"
fi
note "Programs now:"
tools_report

section "Waiting for the app to continue by itself"
READY=no
for n in 01 02 03 04 05 06 07 08 09 10 11 12; do
  sleep 20
  shot "04-setup-$n"
  if grep -q "ready at" "$LAUNCHER_LOG" 2>/dev/null; then READY=yes; break; fi
done
note "server ready: $READY"
if [ "$READY" = yes ]; then
  sleep 20
  shot 05-dashboard
  URL="$(grep -o 'ready at http[^ ]*' "$LAUNCHER_LOG" | tail -n 1 | cut -d ' ' -f 3)"
  note "dashboard address answers: $(curl -s -o /dev/null -w '%{http_code}' "$URL" 2>&1)"
fi
save_launcher_log 05-launcher.log
APPLICATION_LOG="$HOME/Qorqbot/qorqbot/instance/logs/application.log"
[ -f "$APPLICATION_LOG" ] && tail -n 200 "$APPLICATION_LOG" >"$OUT/05-application.log"
exit 0
