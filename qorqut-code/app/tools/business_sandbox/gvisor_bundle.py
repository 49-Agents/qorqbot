#!/usr/bin/env python3
"""Generate the two fixed, disposable gVisor OCI smoke configurations.

This is deliberately a fixture tool, not a production container builder.  It
only edits a config produced by ``runsc spec`` and accepts a profile name; the
workload command, environment, mounts, identity and resource limits are fixed
below so a probe cannot accidentally become an arbitrary command runner.
"""

import argparse
import json
from pathlib import Path


PROFILES = {
    "workstation": {
        "memory": 256 * 1024 * 1024,
        "pids": 128,
        "args": [
            "/bin/sh", "-lc",
            "set -eu; id; claude --version; codex --version; node --version; "
            "/opt/qorqut/venv/bin/python --version; "
            "/opt/qorqut/venv/bin/python -I -c 'import bazarlyk'; "
            "git --version; tmux -V; tmux new-session -d -s gvisor-smoke 'sleep 2'; "
            "tmux has-session -t gvisor-smoke; tmux kill-session -t gvisor-smoke; "
            "echo QORQUT_GVISOR_WORKSTATION_OK",
        ],
        "env": [
            "HOME=/home/agent", "PATH=/usr/local/bin:/usr/bin:/bin", "TMPDIR=/tmp",
        ],
    },
    "browser": {
        "memory": 512 * 1024 * 1024,
        "pids": 256,
        "args": [
            "/bin/sh", "-lc",
            "set -eu; id; /usr/bin/google-chrome --version; "
            "timeout 25 /usr/bin/google-chrome --headless=new --disable-gpu "
            "--no-first-run --disable-crash-reporter --user-data-dir=/tmp/chrome-profile "
            "--dump-dom about:blank > /tmp/chrome-dom; head -c 256 /tmp/chrome-dom; "
            "echo; echo QORQUT_GVISOR_BROWSER_OK",
        ],
        "env": [
            "HOME=/home/agent", "PATH=/usr/local/bin:/usr/bin:/bin", "TMPDIR=/tmp",
            "CHROME_DEVEL_SANDBOX=/opt/google/chrome/chrome-sandbox",
        ],
    },
    "connector": {
        "memory": 128 * 1024 * 1024,
        "pids": 64,
        "args": [
            "/bin/sh", "-lc",
            "set -eu; id; /opt/qorqut/venv/bin/python -c 'from http.server import BaseHTTPRequestHandler,HTTPServer; "
            "H=type(\"H\",(BaseHTTPRequestHandler,),{\"do_GET\":lambda self:(self.send_response(200),self.end_headers(),self.wfile.write(b\"gvisor-loopback\")),\"log_message\":lambda *args:None}); "
            "s=HTTPServer((\"127.0.0.1\",9222),H); print(\"QORQUT_GVISOR_CONNECTOR_SERVER_READY\",flush=True); s.handle_request(); s.handle_request()'",
        ],
        "env": [
            "HOME=/home/agent", "PATH=/usr/local/bin:/usr/bin:/bin", "TMPDIR=/tmp",
        ],
    },
}


def _tmpfs(destination, *, size, mode, owner=False):
    ownership = ["uid=1000", "gid=1000"] if owner else []
    return {
        "destination": destination,
        "type": "tmpfs",
        "source": "tmpfs",
        "options": ["rw", "nosuid", "nodev", f"mode={mode}", f"size={size}", *ownership],
    }


def generate(config_path: Path, profile: str) -> None:
    selected = PROFILES[profile]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if config.get("root", {}).get("path") not in (None, "rootfs"):
        raise SystemExit("runsc spec must use the bundle's rootfs directory")
    namespaces = config.get("linux", {}).get("namespaces", [])
    required = {"pid", "network", "ipc", "uts", "mount"}
    if {item.get("type") for item in namespaces} != required:
        raise SystemExit("runsc spec must provide only the standard private namespaces")
    if any("path" in item for item in namespaces):
        raise SystemExit("namespace paths are not allowed")

    mounts = []
    for mount in config.get("mounts", []):
        if mount.get("type") == "bind" or str(mount.get("source", "")).startswith("/"):
            raise SystemExit("host bind mounts are not allowed")
        if mount.get("destination") in {"/tmp", "/home/agent", "/dev/shm"}:
            continue
        if mount.get("type") == "tmpfs":
            options = list(mount.get("options", []))
            if not any(option.startswith("size=") for option in options):
                options.append("size=64m")
            mount = {**mount, "options": options}
        mounts.append(mount)
    mounts.extend([
        _tmpfs("/tmp", size="128m", mode="1777", owner=False),
        _tmpfs("/home/agent", size="64m", mode="700", owner=True),
    ])
    if profile == "browser":
        mounts.append(_tmpfs("/dev/shm", size="64m", mode="1777"))

    process = config.setdefault("process", {})
    process.update({
        "args": selected["args"],
        "env": selected["env"],
        "cwd": "/",
        "user": {"uid": 1000, "gid": 1000, "additionalGids": []},
        "noNewPrivileges": True,
        "capabilities": {
            name: [] for name in ("ambient", "bounding", "effective", "inheritable", "permitted")
        },
    })
    config["root"] = {"path": "rootfs", "readonly": True}
    config["mounts"] = mounts
    config.setdefault("linux", {})["resources"] = {
        "memory": {"limit": selected["memory"]},
        "pids": {"limit": selected["pids"]},
    }
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    args = parser.parse_args()
    generate(args.config, args.profile)


if __name__ == "__main__":
    main()
