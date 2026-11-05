#!/bin/sh
# Run one pre-built, bounded OCI bundle with a pinned gVisor runtime.
# The caller must create the bundle and provide a private writable runsc state
# directory. This wrapper has no image, network, mount, or process arguments.
set -eu

: "${RUNSC:?set RUNSC to the verified runsc binary}"
: "${BUNDLE:?set BUNDLE to the OCI bundle directory}"
: "${RUNSC_ROOT:?set RUNSC_ROOT to a private writable runsc state directory}"
: "${RUNSC_PROFILE:?set RUNSC_PROFILE to workstation or browser}"

case "$RUNSC" in /*) ;; *) echo 'RUNSC must be an absolute path' >&2; exit 64 ;; esac
case "$BUNDLE" in /*) ;; *) echo 'BUNDLE must be an absolute path' >&2; exit 64 ;; esac
case "$RUNSC_ROOT" in /*) ;; *) echo 'RUNSC_ROOT must be an absolute path' >&2; exit 64 ;; esac
[ -x "$RUNSC" ] || { echo 'RUNSC is not executable' >&2; exit 64; }
[ -f "$BUNDLE/config.json" ] || { echo 'BUNDLE has no config.json' >&2; exit 64; }
case "$RUNSC_PROFILE" in workstation|browser|connector) ;; *) echo 'RUNSC_PROFILE is invalid' >&2; exit 64 ;; esac

# Refuse an accidentally permissive disposable bundle before invoking runsc.
python3 -I - "$BUNDLE/config.json" <<'PY'
import json
import sys

config = json.load(open(sys.argv[1], encoding="utf-8"))
if config.get("root", {}).get("readonly") is not True:
    raise SystemExit("bundle root must be read-only")
user = config.get("process", {}).get("user", {})
if user.get("uid") != 1000 or user.get("gid") != 1000:
    raise SystemExit("bundle process must run as uid/gid 1000")
if user.get("additionalGids"):
    raise SystemExit("bundle must not grant supplementary groups")
if config.get("process", {}).get("noNewPrivileges") is not True:
    raise SystemExit("bundle must set noNewPrivileges")
caps = config.get("process", {}).get("capabilities", {})
required_caps = {"ambient", "bounding", "effective", "inheritable", "permitted"}
if set(caps) != required_caps or any(caps.get(name) for name in required_caps):
    raise SystemExit("bundle must drop all capabilities")
resources = config.get("linux", {}).get("resources", {})
if not isinstance(resources.get("memory", {}).get("limit"), int) or resources["memory"]["limit"] <= 0:
    raise SystemExit("bundle must set a positive memory limit")
if not isinstance(resources.get("pids", {}).get("limit"), int) or resources["pids"]["limit"] <= 0:
    raise SystemExit("bundle must set a positive pids limit")
namespaces = config.get("linux", {}).get("namespaces", [])
if any("path" in ns for ns in namespaces):
    raise SystemExit("bundle must not join a host namespace")
if "network" not in {ns.get("type") for ns in namespaces}:
    raise SystemExit("bundle must isolate its network namespace")
for mount in config.get("mounts", []):
    if mount.get("type") == "bind" or str(mount.get("source", "")).startswith("/"):
        raise SystemExit("bundle must not use host bind mounts")
    if mount.get("type") == "tmpfs":
        sizes = [option[5:] for option in mount.get("options", []) if option.startswith("size=")]
        if len(sizes) != 1 or not sizes[0]:
            raise SystemExit("every tmpfs must have one bounded size")
        import re
        match = re.fullmatch(r"([1-9][0-9]*)([kmgt]?)", sizes[0].lower())
        if not match:
            raise SystemExit("tmpfs size must be a positive numeric value")
        units = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
        size = int(match.group(1)) * units[match.group(2)]
        if size > 256 * 1024 ** 2:
            raise SystemExit("tmpfs size exceeds the fixture bound")
PY

probe_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -I - "$BUNDLE/config.json" "$RUNSC_PROFILE" "$probe_dir/gvisor_bundle.py" <<'PY'
import importlib.util
import json
from pathlib import Path
import sys
config = json.load(open(sys.argv[1], encoding="utf-8"))
spec = importlib.util.spec_from_file_location("qorqut_gvisor_bundle", sys.argv[3])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
selected = module.PROFILES[sys.argv[2]]
process = config.get("process", {})
if process.get("args") != selected["args"] or process.get("env") != selected["env"]:
    raise SystemExit("bundle command or environment does not match its fixed profile")
if process.get("cwd") != "/":
    raise SystemExit("bundle cwd does not match its fixed profile")
PY

run_id=${RUNSC_ID:-qorqut-gvisor-probe}
case "$run_id" in
  ''|*[!A-Za-z0-9_.-]*) echo 'RUNSC_ID contains invalid characters' >&2; exit 64 ;;
esac
seconds=${RUNSC_TIMEOUT_SECONDS:-120}
case "$seconds" in
  ''|*[!0-9]*) echo 'RUNSC_TIMEOUT_SECONDS must be an integer' >&2; exit 64 ;;
esac
[ "$seconds" -ge 1 ] && [ "$seconds" -le 600 ] || {
  echo 'RUNSC_TIMEOUT_SECONDS must be between 1 and 600' >&2
  exit 64
}

version=$("$RUNSC" --version)
case "$version" in
  *'runsc version release-20260907.0'*) ;;
  *) echo 'unexpected runsc version; pin and verify the release first' >&2; exit 65 ;;
esac

exec /usr/bin/timeout --foreground --signal=TERM --kill-after=5s "${seconds}s" \
  "$RUNSC" --root="$RUNSC_ROOT" --network=none run --bundle="$BUNDLE" "$run_id"
