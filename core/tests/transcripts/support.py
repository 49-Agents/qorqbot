"""Synthetic bindings and process helpers used only by the focused capture suite."""

from pathlib import Path
import os
import sqlite3
import sys
from uuid import uuid4

from qorqut_core.transcripts.spool import CaptureSpool, RunBinding, Source


def new_id():
    return str(uuid4())


def binding():
    return RunBinding(new_id(), new_id(), new_id(), 1, new_id(), new_id(), 1)


def registered(path, *, kind="native"):
    run = binding()
    source = Source(new_id(), run.installation_id, run.agent_id, kind,
                    native_session_id="synthetic-session" if kind == "native" else None)
    epoch = new_id()
    spool = CaptureSpool(path)
    spool.register_run(run)
    spool.register_source(source, epoch, run.run_id, capability_evidence="synthetic fixture")
    return spool, source, epoch, run


def raw(path, source, epoch):
    with sqlite3.connect(path) as connection:
        return b"".join(row[0] for row in connection.execute(
            "SELECT payload FROM capture_chunks WHERE source_id=? AND source_epoch=? ORDER BY offset",
            (source, epoch),
        ) if row[0] is not None)


def child_env():
    # Isolated deterministic Python programs only; no native/provider CLI is invoked.
    return {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1"}


def cli_args(path, run, source_id, epoch, child):
    return [sys.executable, "-m", "qorqut_core.transcripts.recorder",
            "--spool", str(path), "--installation-id", run.installation_id,
            "--agent-id", run.agent_id, "--recovery-epoch", run.recovery_epoch,
            "--environment-generation", str(run.environment_generation), "--boot-id", run.boot_id,
            "--run-id", run.run_id, "--run-generation", str(run.run_generation),
            "--source-id", source_id, "--source-epoch", epoch, "--", *child]
