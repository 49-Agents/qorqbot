"""Disposable process for crash/serialization tests at actual tmux boundaries."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from uuid import UUID

from qorqut_core.runner import RunBinding, TerminalRunner, Tmux
from support import wait_for

BODY = "worker payload λ\n"


def main():
    root, mode, identity, result_name = Path(sys.argv[1]), sys.argv[2], UUID(sys.argv[3]), sys.argv[4]
    binding = RunBinding.model_validate_json((root / "binding.json").read_text())
    database = root / "spool.sqlite3"
    stage = mode.removeprefix("crash:")

    def state():
        return json.loads((root / "state.json").read_text())

    class BoundaryTmux(Tmux):
        def load_buffer(self, *args):
            super().load_buffer(*args)
            if stage == "load":
                os._exit(91)

        def paste(self, *args, **kwargs):
            # This fresh connection must see durable exact intent before input.
            with sqlite3.connect(database) as db:
                row = db.execute("SELECT state,phase,payload_sha256,binding_digest FROM runner_attempts WHERE attempt_id=?",
                                 (str(identity),)).fetchone()
                assert row == ("dispatching", "paste", hashlib.sha256(BODY.encode()).hexdigest(), binding.digest), row
            if stage == "before_paste":
                os._exit(91)
            super().paste(*args, **kwargs)
            if stage == "paste":
                os._exit(91)
            if mode == "pause":
                (root / "paused").touch()
                wait_for(lambda: (root / "continue").exists(), timeout=10)

        def enter(self, *args, **kwargs):
            super().enter(*args, **kwargs)
            if stage == "enter":
                os._exit(91)

    class BoundaryRunner(TerminalRunner):
        @staticmethod
        def _update(db, attempt_id, status, phase, reason):
            if stage == "before_receipt" and status == "submitted":
                os._exit(91)
            TerminalRunner._update(db, attempt_id, status, phase, reason)
            if stage == "after_receipt" and status == "submitted":
                os._exit(91)

    def capture(_):
        if stage == "prepared":
            os._exit(91)
        return True

    runner = BoundaryRunner(database, tmux=BoundaryTmux(), capture_ready=capture,
        machine_hold=lambda _: False,
        classify_composer=lambda *_: "empty" if state()["current_bytes"] == 0 else "blocked",
        verify_paste=lambda b, s, digest: bool(wait_for(lambda: state()["current_sha256"] == digest
                                                      and state()["last_paste"] == digest)))
    (root / (result_name + ".entered")).touch()
    if mode == "control":
        receipt = runner.control(binding, identity, action="take", expected_epoch=0, viewer_id=UUID(int=99))
    elif mode == "inspect":
        receipt = runner.inspect_attempt(identity)
    else:
        receipt = runner.attempt(binding, identity, BODY, expected_epoch=0)
    (root / result_name).write_text(receipt.model_dump_json())


if __name__ == "__main__":
    main()
