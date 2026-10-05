"""Discover raw sources and attachment references, without projecting conversation.

P06/P11 format knowledge: pinned Qorqut terminal_chat/transcript_snapshot/terminal.
No cwd ownership check, newest-file choice, snapshot copying, or archive state.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import PurePosixPath
import re
from uuid import UUID

from .capabilities import require_profile
from .managed_io import ManagedTree
from .types import (AdapterError, AttachmentReference, DiscoveryGap, DiscoveryResult,
    ManagedRoots, NativeBinding, NativeSource, Provider, SourceEvidence)

UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
ROLLOUT = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(" + UUID_PATTERN
    + r")(?:_" + UUID_PATTERN + r")?\.jsonl\Z")
# A bound raw source remains eligible even when records exceed this metadata scan
# limit. The recorder, not this module, streams all bytes without a file ceiling.
METADATA_RECORD_LIMIT = 1024 * 1024


def _records(stream):
    offset = 0
    while True:
        line = stream.readline(METADATA_RECORD_LIMIT + 1)
        if not line:
            return
        start = offset
        offset += len(line)
        if len(line) > METADATA_RECORD_LIMIT:
            while not line.endswith(b"\n"):
                line = stream.readline(METADATA_RECORD_LIMIT + 1)
                offset += len(line)
                if not line:
                    break
            yield start, None, "metadata_record_too_large"
            continue
        if not line.endswith(b"\n"):
            yield start, None, "incomplete_metadata_record"
            return
        yield start, line, None


def _evidence(kind, offset, raw):
    return SourceEvidence(kind=kind, offset=offset, length=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def _attachments(provider, record):
    """Only the image_url reference form evidenced by pinned Codex fixtures.

    Yield references, never image contents or tool/user/assistant text. Claude's
    queued_command attachment is an inline native record, not a file attachment.
    Other layouts stay explicit gaps; no invented path-field semantics.
    """
    if provider == "codex":
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        if record.get("type") == "event_msg" and payload.get("type") == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "UserMessage":
                content = item.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "image":
                            yield block.get("image_url")
    elif record.get("type") == "attachment":
        attachment = record.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
            yield None


def discover(provider: Provider, version: str, roots: ManagedRoots, binding: NativeBinding) -> DiscoveryResult:
    require_profile(provider, version)
    bases = (".claude/projects",) if provider == "claude" else (".codex/sessions", ".codex/archived_sessions")
    sources, attachments = [], []
    gaps = [DiscoveryGap(code="child_association_contract_unverified"),
        DiscoveryGap(code="attachment_coverage_partial")]
    expected = str(binding.native_session_id)
    with ManagedTree(roots.home) as home:
        for base in bases:
            try:
                for relative in home.files(base):
                    path = PurePosixPath(relative)
                    if path.suffix != ".jsonl":
                        continue
                    # A child-looking file cannot become a parent source based on cwd,
                    # filename proximity or a parent session ID inside its messages.
                    if "subagents" in path.parts or path.name.startswith("agent-"):
                        gaps.append(DiscoveryGap(code="unsupported_child_association", relative_path=relative))
                        continue
                    if provider == "claude":
                        matches = path.name == expected + ".jsonl"
                    else:
                        match = ROLLOUT.fullmatch(path.name)
                        matches = bool(match and UUID(match.group(1)) == binding.native_session_id)
                    if not matches:
                        if expected in path.name:
                            gaps.append(DiscoveryGap(code="unsupported_source_filename", relative_path=relative))
                        continue
                    evidence, refs, local_gaps = [], [], []
                    conflict = False
                    with home.open_read(relative) as stream:
                        info = os.fstat(stream.fileno())
                        for offset, raw, gap in _records(stream):
                            if gap:
                                local_gaps.append(DiscoveryGap(code=gap, relative_path=relative))
                                continue
                            try:
                                record = json.loads(raw)
                            except (ValueError, UnicodeError, RecursionError):
                                local_gaps.append(DiscoveryGap(code="invalid_metadata_record", relative_path=relative))
                                continue
                            if not isinstance(record, dict):
                                continue
                            native = None
                            if provider == "claude":
                                native = record.get("sessionId")
                                if record.get("isSidechain") is True or record.get("agentId"):
                                    conflict = True
                                    local_gaps.append(DiscoveryGap(code="unsupported_child_association", relative_path=relative))
                                    break
                            elif record.get("type") == "session_meta":
                                payload = record.get("payload")
                                if isinstance(payload, dict):
                                    native = payload.get("id")
                                    # No reliable child/fork lineage contract in the pinned source.
                                    if any(payload.get(key) for key in ("forked_from_id", "parent_thread_id", "parent_session_id")) or isinstance(payload.get("source"), dict):
                                        conflict = True
                                        local_gaps.append(DiscoveryGap(code="unsupported_child_association", relative_path=relative))
                                        break
                            if native is not None:
                                if native != expected:
                                    conflict = True
                                    break
                                if not evidence:
                                    evidence.append(_evidence("native_session_id", offset, raw))
                            for reference in _attachments(provider, record):
                                if not isinstance(reference, str) or not reference:
                                    local_gaps.append(DiscoveryGap(code="unsupported_attachment_reference", relative_path=relative))
                                else:
                                    embedded = reference.startswith("data:")
                                    refs.append(AttachmentReference(source_path=roots.home / relative,
                                        record=_evidence("attachment_reference", offset, raw),
                                        kind="embedded" if embedded else "external",
                                        location=None if embedded else reference))
                                    if not embedded:
                                        local_gaps.append(DiscoveryGap(code="external_attachment_not_captured", relative_path=relative))
                        after = os.fstat(stream.fileno())
                    gaps.extend(local_gaps)
                    if after.st_size < info.st_size or (after.st_size == info.st_size and after.st_mtime_ns != info.st_mtime_ns):
                        gaps.append(DiscoveryGap(code="source_changed_during_discovery", relative_path=relative))
                        continue
                    if conflict or not evidence:
                        gaps.append(DiscoveryGap(code="native_identity_conflict" if conflict else "native_identity_unverified", relative_path=relative))
                        continue
                    sources.append(NativeSource(provider=provider, binding=binding,
                        path=roots.home / relative, relative_path=relative,
                        device=info.st_dev, inode=info.st_ino, observed_size=after.st_size,
                        ownership=tuple(evidence), format_hint=f"{provider}-jsonl-p06-p11"))
                    attachments.extend(refs)
            except (OSError, AdapterError) as exc:
                gaps.append(DiscoveryGap(code="managed_source_unavailable:" + type(exc).__name__, relative_path=base))
    if not sources:
        gaps.append(DiscoveryGap(code="bound_native_source_not_found"))
    return DiscoveryResult(sources=tuple(sources), attachments=tuple(attachments), gaps=tuple(gaps))
