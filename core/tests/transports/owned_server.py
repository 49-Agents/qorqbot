"""Test-only commit barrier and optional crash/lease controls inside the real production server composition."""

import argparse
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from qorqut_core.app import Application
from qorqut_core.storage.jobs import Jobs
from qorqut_core.transports.configuration import read_configuration
from qorqut_core.transports.server import serve


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", type=Path)
    parser.add_argument("--gate", type=Path)
    parser.add_argument("--ui-root", type=Path)
    parser.add_argument("--crash-point")
    parser.add_argument("--fault-record", type=Path)
    parser.add_argument("--fault-exit-code", type=int)
    parser.add_argument("--claim-lease-seconds", type=int)
    args = parser.parse_args()
    settings = read_configuration(args.installation)
    opened = Application.open
    run_next = Jobs.run_next

    def barrier(point):
        if args.gate is None or point != "operation_after_result" or (args.gate / "released").exists():
            return
        (args.gate / "entered").touch()
        deadline = time.monotonic() + 10
        while not (args.gate / "released").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Test barrier timed out")
            time.sleep(0.01)

    def fault(point):
        barrier(point)
        if point == args.crash_point:
            # Record the intended boundary, then exit only this child process.
            args.fault_record.write_text(json.dumps({"point": point, "pid": os.getpid()}), encoding="utf-8")
            os._exit(args.fault_exit_code)

    def short_claim(self, *, worker_id, lease_seconds=30):
        return run_next(self, worker_id=worker_id, lease_seconds=args.claim_lease_seconds)

    with ExitStack() as stack:
        stack.enter_context(patch.object(Application, "open", side_effect=lambda s: opened(s, fault=fault)))
        if args.claim_lease_seconds is not None:
            stack.enter_context(patch.object(Jobs, "run_next", short_claim))
        serve(settings, ui_root=args.ui_root)
