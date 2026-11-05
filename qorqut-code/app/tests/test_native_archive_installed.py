"""Installed-wheel check: ``bazarlyk.native_archive`` imports and captures from outside the source tree.

The check needs an interpreter whose environment holds the built ``bazarlyk-core`` wheel (and nothing
else is required: the archive is standard-library only). It is skipped unless
``NATIVE_ARCHIVE_INSTALLED_PYTHON`` names that interpreter, so the ordinary focused run does not build
anything. The exact build/install commands are in ``docs/v1.2/NATIVE_ARCHIVE_CHECKPOINT.md``.

The child runs in isolated mode (``-I``: no ``PYTHON*`` variables, no user site, no working-directory or
script-directory path entry) from an unrelated temporary directory, proves the package it imported lives
in the installed environment rather than the source tree, and performs one synthetic Claude capture
sequence (partial tail, completed record, final-tail evidence) with readback. Everything it touches is
generated under ``tmp_path``.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest

INTERPRETER = os.environ.get("NATIVE_ARCHIVE_INSTALLED_PYTHON")
SOURCE_TREE = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(not INTERPRETER, reason="NATIVE_ARCHIVE_INSTALLED_PYTHON names no installed-wheel interpreter")

CHILD = r'''
import json, sys
from datetime import datetime, timezone
from pathlib import Path

source_tree, scratch = Path(sys.argv[1]).resolve(), Path(sys.argv[2])
assert sys.flags.isolated and sys.flags.no_user_site
assert not any(Path(entry or ".").resolve() == source_tree for entry in sys.path)

import bazarlyk.native_archive as package
from bazarlyk.native_archive import CaptureBinding, FinalCaptureEvidence, TranscriptArchive

location = Path(package.__file__).resolve()
assert source_tree not in location.parents, location
assert "site-packages" in location.parts, location

conversation = "22222222-2222-4222-8222-222222222222"
workspace, home = scratch / "workspace", scratch / "provider-home"
workspace.mkdir(); home.mkdir(); (scratch / "agent").mkdir()      # the archive creates only the root leaf
source = home / "session.jsonl"


def line(text, uuid):
    return json.dumps({"type": "user", "sessionId": conversation, "cwd": str(workspace), "uuid": uuid,
                       "message": {"role": "user", "content": text}}).encode() + b"\n"


first, second = line("one", "m1"), line("two", "m2")
source.write_bytes(first + second[:20])
binding = CaptureBinding("installed-agent", "claude", conversation, 1, str(workspace), str(source))
archive = TranscriptArchive(scratch / "agent" / "transcripts")

partial = archive.capture(binding)
source.write_bytes(first + second)
whole = archive.capture(binding)
exited = datetime(2026, 10, 11, 12, 5, tzinfo=timezone.utc)
done = archive.capture(binding, final_evidence=FinalCaptureEvidence(
    "installed-agent", "claude", conversation, 1, "pid-1:start-1", exited))

raw = archive.read_raw(done, offset=0, limit=1 << 20)
records = [(record.offset, record.length, record.kind) for record in archive.iter_records(done)]
print(json.dumps({
    "module": str(location),
    "partial": [partial.sequence, partial.length, partial.complete, list(partial.incomplete_reasons),
                partial.record_boundary, partial.partial_tail_bytes],
    "whole": [whole.sequence, whole.length, whole.appended, whole.complete, whole.partial_tail_bytes],
    "done": [done.sequence, done.appended, done.complete, done.final is not None],
    "raw_matches": raw.data == first + second and raw.next_offset is None,
    "records": records, "first": len(first), "second": len(second),
    "chain": [archive.load_receipt(f"claude:{conversation}:{n}").predecessor_id for n in (1, 2, 3)],
    "latest_is_final": archive.latest(binding) == done,
}))
'''


def test_installed_wheel_imports_and_captures_from_outside_the_source_tree(tmp_path):
    scratch, elsewhere = tmp_path / "scratch", tmp_path / "unrelated-cwd"
    scratch.mkdir()
    elsewhere.mkdir()
    environment = {"HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
    done = subprocess.run([INTERPRETER, "-I", "-c", CHILD, str(SOURCE_TREE), str(scratch)], cwd=elsewhere,
                          env=environment, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)

    first, second = result["first"], result["second"]
    assert "site-packages" in Path(result["module"]).parts and str(SOURCE_TREE) not in result["module"]
    assert result["partial"] == [1, first + 20, False, ["partial_tail"], first, 20]
    assert result["whole"] == [2, first + second, second - 20, True, 0]
    assert result["done"] == [3, 0, True, True]
    assert result["raw_matches"] is True and result["latest_is_final"] is True
    assert result["records"] == [[0, first, "json"], [first, second, "json"]]
    prefix = "claude:22222222-2222-4222-8222-222222222222"
    assert result["chain"] == [None, f"{prefix}:1", f"{prefix}:2"]
    assert (scratch / "agent" / "transcripts" / "claude").is_dir()
