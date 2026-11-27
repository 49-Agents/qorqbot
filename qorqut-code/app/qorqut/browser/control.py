"""Process identity checks and the DevTools port's answers, shared by the agents' managed browsers. Standard library and
the system layer only: a browser's supervisor imports this, and must not need the application for it."""

import json
from pathlib import Path
import sys
from urllib.request import ProxyHandler, build_opener

from qorqut_system import processes as _processes


def devtools_json(port, path):
    """What the Chrome on this loopback DevTools port answers at ``path`` (``/json/list``, ``/json/version``)."""
    with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return json.load(response)


def process_identity(pid):
    """Boot identity and process start time prevent mistaking a reused PID for Chrome."""
    if sys.platform == 'darwin':
        from qorqut.process_info import identity
        return identity(pid)
    stat = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + stat[19]


def owns_listener(pid, port, address=None):
    """The process holds a listening TCP socket on ``port`` (optionally only on ``address``)."""
    return _processes.owns_listener(pid, port, address)
