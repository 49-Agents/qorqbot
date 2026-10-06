"""Test-only commit barrier inside the real production server composition."""

import argparse
import time
from pathlib import Path
from unittest.mock import patch

from qorqut_core.app import Application
from qorqut_core.transports.configuration import read_configuration
from qorqut_core.transports.server import serve


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", type=Path)
    parser.add_argument("--transport-config", type=Path)
    parser.add_argument("--gate", type=Path)
    parser.add_argument("--ui-root", type=Path)
    parser.add_argument("--session-seconds", type=float, default=28800)
    args = parser.parse_args()
    settings, credentials = read_configuration(args.installation, args.transport_config)
    opened = Application.open

    def barrier(point):
        if args.gate is None or point != "operation_after_result" or (args.gate / "released").exists():
            return
        (args.gate / "entered").touch()
        deadline = time.monotonic() + 10
        while not (args.gate / "released").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Test barrier timed out")
            time.sleep(0.01)

    with patch.object(Application, "open", side_effect=lambda s: opened(s, fault=barrier)), \
         patch("qorqut_core.transports.sessions.SESSION_SECONDS", args.session_seconds):
        serve(settings, credentials, ui_root=args.ui_root)
