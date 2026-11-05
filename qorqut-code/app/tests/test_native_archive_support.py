"""Synthetic fixtures for the native archive tests: every byte, path and identity is generated here.

No provider home, registered agent, business, database, process or network is touched; each
workbench lives in a fresh temporary directory. The module defines no tests (the name only follows
the archive checkpoint's exclusive test-file pattern).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

from bazarlyk.native_archive import CaptureBinding, FinalCaptureEvidence, TranscriptArchive

CONVERSATION = "11111111-1111-4111-8111-111111111111"
AGENT = "5f2a9c1e-3b7d-4e0a-9c55-0d2f6a1b7e44"
EPOCH = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


def jsonl(record: dict) -> bytes:
    return json.dumps(record).encode() + b"\n"


class SimulatedCrash(BaseException):
    """Raised from the archive's crash seam; BaseException so no handler in the writer swallows it."""


class HookedArchive(TranscriptArchive):
    """An archive whose crash seam calls ``hook(stage_name)`` (it may raise, or mutate the source)."""

    def __init__(self, root, hook, **options):
        super().__init__(root, **options)
        self._hook = hook

    def _crash_point(self, name: str) -> None:
        self._hook(name)


def crash_at(stage: str, *, occurrence: int = 1):
    """A hook that raises :class:`SimulatedCrash` on the ``occurrence``-th time ``stage`` is reached."""
    seen = {"count": 0}

    def hook(name: str) -> None:
        if name == stage:
            seen["count"] += 1
            if seen["count"] == occurrence:
                raise SimulatedCrash(stage)

    return hook


class Workbench:
    """A temporary agent transcripts root, workspace and provider source for one lineage."""

    def __init__(self, base: Path, *, provider: str = "claude", conversation: str = CONVERSATION, agent: str = AGENT):
        self.base = base
        self.provider, self.conversation, self.agent = provider, conversation, agent
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.sources = base / "provider-home"
        self.sources.mkdir()
        self.parent = base / "agent"
        self.parent.mkdir()
        self.root = self.parent / "transcripts"
        self.source = self.sources / "session.jsonl"
        self.archive = self.new_archive()

    # ---- construction helpers

    def new_archive(self, hook=None, **options) -> TranscriptArchive:
        options.setdefault("clock", lambda: EPOCH)
        return HookedArchive(self.root, hook, **options) if hook else TranscriptArchive(self.root, **options)

    def binding(self, generation: int = 1, **overrides) -> CaptureBinding:
        values = {"agent_id": self.agent, "provider": self.provider, "conversation_id": self.conversation,
                  "generation": generation, "workspace": str(self.workspace), "source_path": str(self.source)}
        return CaptureBinding(**{**values, **overrides})

    def final(self, generation: int = 1, token: str = "pid-4242:start-777", minutes: int = 5, **overrides):
        values = {"agent_id": self.agent, "provider": self.provider, "conversation_id": self.conversation,
                  "generation": generation, "process_token": token, "exited_at": EPOCH + timedelta(minutes=minutes)}
        return FinalCaptureEvidence(**{**values, **overrides})

    # ---- provider records

    def claude(self, text: str = "hello", *, uuid: str = "m1", role: str = "user", **extra) -> bytes:
        record = {"type": role, "sessionId": self.conversation, "cwd": str(self.workspace), "uuid": uuid,
                  "message": {"role": role, "content": text}, **extra}
        return jsonl(record)

    def codex_meta(self, **payload) -> bytes:
        body = {"id": self.conversation, "cwd": str(self.workspace), **payload}
        return jsonl({"type": "session_meta", "payload": body})

    def codex_message(self, text: str = "hello", role: str = "user") -> bytes:
        return jsonl({"type": "response_item", "payload": {"type": "message", "role": role,
                                                           "content": [{"type": "input_text", "text": text}]}})

    # ---- source file

    def write(self, data: bytes) -> None:
        self.source.write_bytes(data)

    def append(self, data: bytes) -> None:
        with self.source.open("ab") as stream:
            stream.write(data)

    def source_bytes(self) -> bytes:
        return self.source.read_bytes()

    # ---- archive inspection

    @property
    def lineage(self) -> Path:
        from bazarlyk.native_archive._types import encode_component
        return self.root / self.provider / encode_component(self.conversation)

    def payload(self) -> bytes:
        return (self.lineage / "payload.raw").read_bytes()

    def receipt_files(self) -> list[Path]:
        return sorted((self.lineage / "receipts").glob("*.rcpt"))

    def snapshot(self) -> dict[str, tuple[int, int]]:
        return tree_snapshot(self.root)


def tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Relative path -> (size, mtime_ns) of every file and directory below ``root`` (for 'nothing changed' checks)."""
    result = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        result[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns)
    return result


def tree_storage(root: Path) -> dict:
    """Every regular file under ``root``: apparent bytes, allocated bytes and the file list."""
    apparent = allocated = 0
    files = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        if path.is_symlink() or not path.is_file():
            continue
        files[str(path.relative_to(root))] = info.st_size
        apparent += info.st_size
        allocated += info.st_blocks * 512
    return {"apparent": apparent, "allocated": allocated, "files": files}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def temporaries(workbench: Workbench) -> list[str]:
    """Leftover staging or temporary files in the lineage (a clean archive has none)."""
    found = [str(path.relative_to(workbench.lineage)) for path in workbench.lineage.glob(".tmp-*")]
    staging = workbench.lineage / "staging"
    if staging.exists():
        found.extend(str(path.relative_to(workbench.lineage)) for path in staging.iterdir())
    return found


def flip_byte(path: Path, offset: int) -> None:
    with path.open("r+b") as stream:
        stream.seek(offset)
        value = stream.read(1)
        stream.seek(offset)
        stream.write(bytes([value[0] ^ 0xFF]))
