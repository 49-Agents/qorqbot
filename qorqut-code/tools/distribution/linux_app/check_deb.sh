#!/bin/bash
# Release gate for the .deb: install it on a new Ubuntu 24.04 (a Docker container) with `apt install`, check what it
# installs, open it as a new person would, remove it and check nothing is left. Fails when anything differs;
# summary.txt and the logs in the output folder show why.
#
#   check_deb.sh [--no-launch] [--system-tools] DEB OUTPUT_FOLDER
#
# --no-launch     check only the files and the package, do not open the app
# --system-tools  the archive inside is not self-contained: also install the Python 3.12, git and tmux it relies on
#
# Needs Docker and a network connection (the image is ubuntu:24.04; its packages come from Ubuntu's archive, or from
# the mirror named by QORQBOT_APT_MIRROR, e.g. http://azure.archive.ubuntu.com/ubuntu; apt still verifies them). The
# container is not given more privileges than Docker's default, which blocks the user namespaces Chromium's sandbox
# uses, so the app opens with --no-sandbox here; the AppArmor profile and the sandbox helper's permissions are checked
# as files, not exercised. The container's AppArmor is not enabled, so the profile is parsed but not loaded.
set -u

LAUNCH=1
SYSTEM_TOOLS=0
while [ $# -gt 2 ]; do
  case "$1" in
    --no-launch) LAUNCH=0 ;;
    --system-tools) SYSTEM_TOOLS=1 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done
DEB="${1:?usage: check_deb.sh [--no-launch] [--system-tools] DEB OUTPUT_FOLDER}"
OUT="${2:?usage: check_deb.sh [--no-launch] [--system-tools] DEB OUTPUT_FOLDER}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEB="$(readlink -f "$DEB")"
mkdir -p "$OUT" || exit 1
OUT="$(cd "$OUT" && pwd)"
chmod 777 "$OUT"
rm -f "$OUT/summary.txt"
[ -f "$DEB" ] || { echo "FAILED: $DEB is not a file" | tee -a "$OUT/summary.txt"; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "FAILED: docker is not installed" | tee -a "$OUT/summary.txt"; exit 1; }

docker run -i --rm \
  --mount "type=bind,src=$DEB,dst=/in/$(basename "$DEB"),readonly" \
  --mount "type=bind,src=$HERE,dst=/check,readonly" \
  --mount "type=bind,src=$OUT,dst=/out" \
  -e LAUNCH="$LAUNCH" -e SYSTEM_TOOLS="$SYSTEM_TOOLS" -e DEB_NAME="$(basename "$DEB")" -e APT_MIRROR="${QORQBOT_APT_MIRROR:-}" \
  ubuntu:24.04 bash -s <<'CONTAINER'
set -u
OUT=/out
FAILED=0
note() { printf '%s\n' "$*" | tee -a "$OUT/summary.txt"; }
section() { note ""; note "== $* =="; }
fail() { note "FAILED: $*"; FAILED=1; }
expect_file() { [ -e "$1" ] || [ -L "$1" ] && note "  present: $1" || fail "missing: $1"; }

export DEBIAN_FRONTEND=noninteractive
section "A new Ubuntu"
note "$(. /etc/os-release; echo "$PRETTY_NAME"), $(uname -m)"
for program in python3.12 git tmux node; do
  command -v "$program" >/dev/null 2>&1 && note "  $program is installed in this image" || note "  $program is not installed (as on a new computer)"
done

section "apt install"
EXTRA=""
[ "$SYSTEM_TOOLS" = 1 ] && EXTRA="python3.12 python3.12-venv git tmux"
cp "/in/$DEB_NAME" "/tmp/$DEB_NAME"
if [ -n "$APT_MIRROR" ]; then
  mirror="${APT_MIRROR%/}"
  sed -i "s|http://archive.ubuntu.com/ubuntu/|$mirror/|;s|http://security.ubuntu.com/ubuntu/|$mirror/|" /etc/apt/sources.list.d/ubuntu.sources
  note "packages come from $mirror"
fi
apt-get update -qq >"$OUT/apt-update.log" 2>&1 || fail "apt-get update failed (see apt-update.log)"
# The tools of the check itself, then the package exactly as `sudo apt install ./file.deb` does (with its Recommends).
apt-get install -y -qq desktop-file-utils apparmor procps xvfb xauth x11-utils ca-certificates $EXTRA >"$OUT/apt-tools.log" 2>&1 \
  || fail "the check's own tools did not install (see apt-tools.log)"
if apt-get install -y "/tmp/$DEB_NAME" >"$OUT/apt-install.log" 2>&1; then
  note "installed $DEB_NAME"
else
  fail "apt install ./$DEB_NAME failed (see apt-install.log)"
  tail -n 20 "$OUT/apt-install.log"
  exit 1
fi
grep -E "^Setting up|libappindicator" "$OUT/apt-install.log" | sed 's/^/  /' | head -n 12

section "The package"
dpkg -s qorqbot | grep -E "^(Package|Status|Version|Architecture|Maintainer|Depends|Recommends|Section|Homepage|License)" | sed 's/^/  /' | tee -a "$OUT/summary.txt" >/dev/null
dpkg -s qorqbot | grep -q "^Status: install ok installed" || fail "dpkg does not list qorqbot as installed"
VERSION="$(dpkg-query -W -f '${Version}' qorqbot)"
case "$VERSION" in
  *-*) fail "the package version $VERSION has a revision part; pre-releases must use ~ so they sort before the release" ;;
esac
note "  package version: $VERSION"

section "Files"
expect_file /opt/Qorqbot/qorqbot-desktop
expect_file /opt/Qorqbot/resources/app.asar
expect_file /opt/Qorqbot/resources/THIRD-PARTY-NOTICES.md
expect_file /opt/Qorqbot/LICENSE.electron.txt
expect_file /opt/Qorqbot/LICENSES.chromium.html
expect_file /usr/share/doc/qorqbot/copyright
ARCHIVE="$(ls /opt/Qorqbot/resources/qorqbot-payload/qorqbot-*-ubuntu24-x86_64.tar.gz 2>/dev/null | head -n 1)"
[ -n "$ARCHIVE" ] && note "  present: $ARCHIVE" || fail "the Qorqbot archive is missing from /opt/Qorqbot/resources/qorqbot-payload"
for name in ARCHIVE_NAME ARCHIVE_SHA256 VERSION; do expect_file "/opt/Qorqbot/resources/qorqbot-payload/$name"; done
if [ -n "$ARCHIVE" ]; then
  [ "$(sha256sum "$ARCHIVE" | cut -d ' ' -f 1)" = "$(tr -d '\n' </opt/Qorqbot/resources/qorqbot-payload/ARCHIVE_SHA256)" ] \
    && note "  the archive matches its recorded SHA-256" || fail "the archive does not match ARCHIVE_SHA256"
  [ "$(basename "$ARCHIVE")" = "$(tr -d '\n' </opt/Qorqbot/resources/qorqbot-payload/ARCHIVE_NAME)" ] || fail "ARCHIVE_NAME does not name the archive"
fi
[ -L /usr/bin/qorqbot-desktop ] && note "  /usr/bin/qorqbot-desktop -> $(readlink -f /usr/bin/qorqbot-desktop)" || fail "/usr/bin/qorqbot-desktop is not a link"
[ "$(readlink -f /usr/bin/qorqbot-desktop)" = /opt/Qorqbot/qorqbot-desktop ] || fail "/usr/bin/qorqbot-desktop does not lead to /opt/Qorqbot/qorqbot-desktop"
for size in 16 24 32 48 64 128 256 512; do expect_file "/usr/share/icons/hicolor/${size}x${size}/apps/qorqbot.png"; done
MODE="$(stat -c %a /opt/Qorqbot/chrome-sandbox)"
note "  chrome-sandbox mode $MODE ($( [ "$MODE" = 4755 ] && echo "setuid, used where user namespaces are unavailable" || echo "not setuid; the AppArmor profile lets Chromium use user namespaces"))"

section "Menu entry"
ENTRY=/usr/share/applications/qorqbot.desktop
expect_file "$ENTRY"
if desktop-file-validate "$ENTRY" 2>&1 | tee "$OUT/desktop-file-validate.log" | grep -q .; then fail "desktop-file-validate: $(cat "$OUT/desktop-file-validate.log")"; else note "  desktop-file-validate: no complaints"; fi
for expected in "Name=Qorqbot" "Exec=/opt/Qorqbot/qorqbot-desktop %U" "Icon=qorqbot" "StartupWMClass=Qorqbot" "Categories=Office;" "Type=Application"; do
  grep -qxF "$expected" "$ENTRY" && note "  $expected" || fail "the menu entry lacks $expected"
done

section "AppArmor profile (Ubuntu 24.04 restricts unprivileged user namespaces)"
PROFILE=/opt/Qorqbot/resources/apparmor-profile
expect_file "$PROFILE"
grep -q "^profile \"qorqbot-desktop\" \"/opt/Qorqbot/qorqbot-desktop\"" "$PROFILE" && note "  the profile covers /opt/Qorqbot/qorqbot-desktop" || fail "the profile does not name /opt/Qorqbot/qorqbot-desktop"
grep -q "^  userns," "$PROFILE" && note "  it allows user namespaces (userns,)" || fail "the profile does not allow userns"
if apparmor_parser --skip-kernel-load --debug "$PROFILE" >"$OUT/apparmor-parse.log" 2>&1; then
  note "  apparmor_parser accepts it (AppArmor $(apparmor_parser --version | head -n 1 | sed 's/.*version //'))"
else
  fail "apparmor_parser rejects the profile (see apparmor-parse.log)"
fi
grep -q "apparmor_parser --replace" /var/lib/dpkg/info/qorqbot.postinst && note "  the install script copies it to /etc/apparmor.d/qorqbot-desktop and loads it where AppArmor is running" || fail "the install script does not load the profile"
grep -q "apparmor_parser --remove" /var/lib/dpkg/info/qorqbot.postrm && note "  the removal script unloads it" || fail "the removal script does not unload the profile"
# AppArmor is not running in a container, so the script skips the profile; with it running the file would be here.
[ -e /etc/apparmor.d/qorqbot-desktop ] && note "  /etc/apparmor.d/qorqbot-desktop is installed" || note "  /etc/apparmor.d/qorqbot-desktop is not installed (AppArmor is not enabled in this container)"

section "Libraries"
MISSING="$(ldd /opt/Qorqbot/qorqbot-desktop /opt/Qorqbot/chrome_crashpad_handler 2>&1 | grep "not found" | sort -u)"
if [ -z "$MISSING" ]; then note "  every library the program needs is installed"; else fail "missing libraries:"$'\n'"$MISSING"; fi

if [ "$LAUNCH" = 1 ]; then
  section "Open it as a new person would"
  # A normal user, as on a desktop. The container has no user namespaces, so Chromium's sandbox is off for this run.
  useradd -m -s /bin/bash tester
  mkdir -p "$OUT/first-open"
  chmod 777 "$OUT/first-open"
  runuser -u tester -- env QORQBOT_CHECK_NO_SANDBOX=1 bash /check/first_open_check.sh --installed /usr/bin/qorqbot-desktop "$OUT/first-open" >"$OUT/first-open.log" 2>&1
  CODE=$?
  sed -n '/^== Machine/,$p' "$OUT/first-open/summary.txt" 2>/dev/null | sed 's/^/  /' | head -n 80
  [ "$CODE" = 0 ] && note "the first-open check passed" || fail "the first-open check failed (see first-open/)"
fi

section "apt remove"
apt-get remove -y qorqbot >"$OUT/apt-remove.log" 2>&1 || fail "apt remove failed (see apt-remove.log)"
for gone in /opt/Qorqbot /usr/bin/qorqbot-desktop /usr/share/applications/qorqbot.desktop /etc/apparmor.d/qorqbot-desktop; do
  [ -e "$gone" ] || [ -L "$gone" ] && fail "still present after removal: $gone" || note "  gone: $gone"
done
for size in 16 512; do [ -e "/usr/share/icons/hicolor/${size}x${size}/apps/qorqbot.png" ] && fail "an icon is still present after removal"; done

section "Result"
if [ "$FAILED" = 0 ]; then note "passed"; else note "failed"; fi
exit "$FAILED"
CONTAINER
exit $?
