"""Durable, bounded reporting from prepared agent hooks.

This module intentionally has no service or application imports.  Hooks can
record a small observation when the service is down. The host supervisor replays
it later using the same scoped credential. Reporting failures are swallowed by the
caller and never recurse into another observation.

``python technical_reporting.py --registration .qorqut/registration.json``
prints a bounded, read-only summary of the local outbox (counts, capacity and
active markers only; never a record body or credential).
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
import re
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError


MAX_RECORD_BYTES = 4096
MAX_OUTBOX_BYTES = 4 * 1024 * 1024
MAX_REPLAY = 8
REPLAY_SECONDS = 2.0
OUTBOX_NAME = "pending.jsonl"
REJECTED_NAME = "rejected.jsonl"
MAX_REJECTED_BYTES = 4 * 1024 * 1024
MAX_REJECTED_ENTRY_BYTES = 8192
MAX_MARKERS = 100
_EXPECTED = frozenset({
    "validation_error", "forbidden", "permission_denied", "approval_pending",
    "revision_conflict", "login_required", "account_missing", "setup_incomplete",
    "pending", "hold", "not_found", "policy_denied", "invalid_credential",
    "missing_credential", "inactive_agent", "inbox_session_superseded",
    "inbox_consumer_busy", "context_refresh_required", "stale_proposal",
    "idempotency_conflict", "configuration_missing", "operation_unavailable",
    "scope_required", "invalid_transition", "maintenance", "cancelled",
    "service_stopping", "management_unavailable", "unsupported_runtime", "busy",
})
_COMPONENTS = frozenset({'bootstrap', 'inbox', 'activity_hook'})
_OPERATIONS = frozenset({'prepare', 'receipt', 'review', 'watch', 'queue', 'record', 'checkpoint', 'release'})
_SAFE_SYMBOL = re.compile(r'^[a-z][a-z0-9_.-]{0,79}$')
_CALL_SITES = frozenset({'bootstrap.prepare', 'bootstrap.handle_hook', 'bootstrap.receipt', 'bootstrap.review', 'inbox.watcher.watch',
    'inbox.watcher.queue', 'inbox.watcher.release', 'activity_hook.record',
    'activity_hook.checkpoint'})
_DEFAULT_CALL_SITE = {
    ('bootstrap', 'prepare'): 'bootstrap.prepare', ('bootstrap', 'receipt'): 'bootstrap.receipt',
    ('bootstrap', 'review'): 'bootstrap.review',
    ('inbox', 'watch'): 'inbox.watcher.watch', ('inbox', 'queue'): 'inbox.watcher.queue',
    ('inbox', 'release'): 'inbox.watcher.release', ('activity_hook', 'record'): 'activity_hook.record',
    ('activity_hook', 'checkpoint'): 'activity_hook.checkpoint'}
_LOCAL_ONLY = frozenset({'registration_id', 'recovery_for'})
# Exact service reasons for a forbidden report whose own binding can never be
# accepted. A forbidden without one of these is endpoint-wide and stays pending.
_PERMANENT_FORBIDDEN = frozenset({'technical_registration_mismatch', 'technical_session_mismatch'})
_SAFE_REASON = re.compile(r'^[a-z][a-z0-9_]{0,63}$')


class _RemoteError(Exception):
    def __init__(self, code, details=None):
        self.code = code
        self.details = details
        super().__init__(code)


def _safe(path: Path) -> bool:
    return not path.is_symlink() and not any(parent.is_symlink() for parent in path.parents)


def _path(registration: dict) -> Path:
    workspace = Path(registration["workspace"])
    path = workspace / ".qorqut/technical-reports" / OUTBOX_NAME
    if not workspace.is_absolute() or not _safe(path):
        raise ValueError("unsafe technical report path")
    return path


@contextmanager
def _lock(path: Path):
    lock = path.with_suffix(path.suffix + ".lock")
    if not _safe(lock):
        raise ValueError("unsafe technical report lock")
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _fsync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _episode(value) -> tuple | None:
    """The exact failure episode a recovery record closes; None when unknown."""
    if not isinstance(value, dict) or value.get('recovered') is not True:
        return None
    target = value.get('recovery_for')
    fields = tuple(value.get(key) for key in ('registration_id', 'session_id', 'component', 'operation'))
    if not isinstance(target, str) or not target or any(not isinstance(item, str) or not item for item in fields):
        return None
    return (*fields, target)


def _recovery_id(value: dict) -> str:
    """One stable identity per failure episode, so a retried recovery is the same receipt."""
    identity = json.dumps(_episode(value), separators=(",", ":"))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "qorqut/technical-recovery/" + identity))


def _has_episode(path: Path, key: tuple) -> bool:
    needle = key[-1].encode()
    for line in path.read_bytes().splitlines():
        if needle not in line or len(line) > MAX_RECORD_BYTES:
            continue
        try:
            value = json.loads(line)
            # Only a valid row counts: replay discards an invalid one, which
            # must not stand in for the recovery being queued.
            if _valid(value) and _episode(value) == key:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _newest_failure_locked(path: Path, value: dict) -> dict | None:
    """The newest valid queued failure of this exact registration/session/component/operation.

    Scans the whole bounded outbox in file order; the caller holds the outbox lock.
    MAX_REPLAY only limits an HTTP batch, never which episode is identified.
    """
    if not path.exists() or not _safe(path) or path.stat().st_size > MAX_OUTBOX_BYTES:
        return None
    newest = None
    for line in path.read_bytes().splitlines():
        if len(line) > MAX_RECORD_BYTES:
            continue
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if (_valid(item) and not item["recovered"] and all(
                item[key] == value[key] for key in ("registration_id", "session_id", "component", "operation"))):
            newest = item
    return newest


def _newest_failure(path: Path, value: dict) -> str | None:
    """The id of that failure, read under the existing outbox lock; None when there is none."""
    try:
        if not path.exists() or not _safe(path) or path.stat().st_size > MAX_OUTBOX_BYTES:
            return None
        with _lock(path):
            newest = _newest_failure_locked(path, value)
        return newest["observation_id"] if newest else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


_SAVED, _DUPLICATE, _FULL, _FAILED = "saved", "duplicate", "full", "failed"


def _append(path: Path, value: dict) -> str:
    """Append under the outbox lock; one recovery per failure episode is ever queued.

    A failure's marker is installed in the same lock acquisition as its append, so an
    installed marker always names the newest appended failure of that identity. If
    marker storage is down the report stays queued and the previous marker is kept.
    """
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_RECORD_BYTES:
        return _FAILED
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    if not _safe(path):
        return _FAILED
    with _lock(path):
        try:
            current = path.stat().st_size if path.exists() else 0
            key = _episode(value)
            if key is not None and 0 < current <= MAX_OUTBOX_BYTES and _has_episode(path, key):
                return _DUPLICATE
            if current + len(encoded) + 1 > MAX_OUTBOX_BYTES:
                return _FULL
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            if not value.get("recovered"):
                try:
                    _mark_active_locked(path, value)
                except (OSError, ValueError):
                    # The report stays queued and replayable; active() and the replay
                    # repair find its episode from the outbox while marker storage is down.
                    pass
            return _SAVED
        except OSError:
            return _FAILED


def _health(path: Path, field: str, amount=1, *, value=None) -> None:
    """Best-effort local health; failures here are never reported."""
    health = path.with_name("health.json")
    if not _safe(health):
        return
    try:
        prior = json.loads(health.read_text()) if health.exists() and health.stat().st_size < 1024 else {}
        current = {key: int(item) for key, item in prior.items() if isinstance(item, int)}
        if value is not None and current.get(field, 0) == value:
            return  # an unchanged state is not rewritten on every replay pass
        current[field] = value if value is not None else current.get(field, 0) + amount
        descriptor, temporary = tempfile.mkstemp(prefix=".health-", dir=health.parent)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(current, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, health)
        health.chmod(0o600)
    except (OSError, ValueError, TypeError):
        try:
            if "temporary" in locals():
                os.unlink(temporary)
        except OSError:
            pass


def _marker(path: Path, value: dict) -> Path:
    identity = ":".join(str(value.get(key, "")) for key in
        ("registration_id", "session_id", "component", "operation"))
    return path.parent / "active" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")


def _mark_active_locked(path: Path, value: dict) -> None:
    """Install the marker naming ``value``; the caller holds the outbox lock.

    The outbox lock is the one serialization boundary for every marker write and
    delete (failure append, replay repair, retirement). Helpers named ``_locked``
    never take it themselves: flock on a second descriptor would deadlock.
    """
    target = _marker(path, value)
    if not _safe(target):
        raise ValueError("unsafe technical report marker")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".active-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump({key: value[key] for key in ("registration_id", "session_id", "component", "operation", "observation_id")},
                      stream, separators=(",", ":"))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
        _fsync_dir(target.parent)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _clear_active_locked(path: Path, value: dict) -> None:
    """Retire the marker only while it still names the episode ``value`` closes.

    The caller holds the outbox lock, so the version check and the unlink are one
    step: a newer failure cannot install its marker between them. The containing
    directory is fsynced after the delete so the retirement is durable.
    """
    target = _marker(path, value)
    if not _safe(target):
        return
    closes = value.get('recovery_for') if value.get('recovered') else value.get('observation_id')
    if not closes or not target.exists():
        return
    current = json.loads(target.read_text())
    if isinstance(current, dict) and current.get('observation_id') == closes:
        target.unlink(missing_ok=True)
        _fsync_dir(target.parent)


def _clear_active(path: Path, value: dict) -> None:
    try:
        with _lock(path):
            _clear_active_locked(path, value)
    except (OSError, ValueError, TypeError):
        pass


def _repair_marker(path: Path, value: dict) -> bool:
    """Keep an accepted replayed failure's episode marked; False if that repair failed.

    Rechecked under the outbox lock because the replay snapshot is stale by the time
    the service answered: a valid marker (possibly a newer failure's) is never
    overwritten, and a missing one is rebuilt only from the newest failure still
    queued, so a report another process already retired is not resurrected.
    """
    try:
        with _lock(path):
            if _marker_active(path, value):
                return True
            newest = _newest_failure_locked(path, value)
            if newest is None:
                return True
            _mark_active_locked(path, newest)
            return _marker_active(path, newest)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _readable_marker(content) -> dict | None:
    """The marker object when it names a failure (a nonempty string id), else None.

    One rule for every reader: anything else (not JSON, not an object, a missing,
    empty or non-string id) is an unreadable marker.
    """
    if isinstance(content, dict) and isinstance(content.get('observation_id'), str) and content['observation_id']:
        return content
    return None


def _read_marker(path: Path, value: dict) -> dict | None:
    """This identity's readable marker, or None (absent, unsafe or unreadable)."""
    try:
        target = _marker(path, value)
        if not target.exists() or not _safe(target):
            return None
        return _readable_marker(json.loads(target.read_text()))
    except (OSError, ValueError, TypeError):
        return None


def _marker_active(path: Path, value: dict) -> bool:
    """Check only the durable marker, never the pending line itself."""
    current = _read_marker(path, value)
    return (current is not None
            and current.get('registration_id') == value.get('registration_id')
            and current.get('session_id') == value.get('session_id')
            and current.get('component') == value.get('component')
            and current.get('operation') == value.get('operation'))


def _replace_lines(path: Path, lines: list[bytes]) -> None:
    """Atomically and durably replace the outbox; the caller holds its lock."""
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if lines:
                stream.write(b"\n".join(lines) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _ack(path: Path, digests: set[str]) -> None:
    if not digests or not path.exists():
        return
    with _lock(path):
        try:
            lines = path.read_bytes().splitlines()
            _replace_lines(path, [line for line in lines if hashlib.sha256(line).hexdigest() not in digests])
        except (OSError, ValueError):
            pass


def _compact(path: Path) -> int:
    """Collapse legacy duplicate recoveries of one exact failure episode.

    Only valid recovery rows with identical registration/session/component/
    operation and a nonempty ``recovery_for`` collapse, keeping the earliest
    original record. Failures, unknown targets, newer episodes and other
    sessions are never touched, and retained rows keep their relative order.
    """
    try:
        if not path.exists() or not _safe(path) or path.stat().st_size > MAX_OUTBOX_BYTES:
            return 0
        with _lock(path):
            seen, kept, removed = set(), [], 0
            for line in path.read_bytes().splitlines():
                key = None
                if len(line) <= MAX_RECORD_BYTES:
                    try:
                        value = json.loads(line)
                        key = _episode(value) if _valid(value) else None
                    except (TypeError, ValueError):
                        key = None
                if key is not None:
                    if key in seen:
                        removed += 1
                        continue
                    seen.add(key)
                kept.append(line)
            if removed:
                _replace_lines(path, kept)
        if removed:
            _health(path, "coalesced", removed)
        return removed
    except (OSError, ValueError):
        return 0


def _permanent_rejection(error) -> str | None:
    """The reason a service answer rejects only this record, else None.

    Schema validation and the two exact binding reasons can never succeed for
    this record. Anything else (generic forbidden, credentials, network,
    maintenance, unavailable) is endpoint-wide or transient and must stay pending.
    """
    code = getattr(error, 'code', '')
    if code == 'validation_error':
        return 'validation_error'
    details = getattr(error, 'details', None)
    reason = details.get('reason') if hasattr(details, 'get') else None
    if code == 'forbidden' and isinstance(reason, str) and reason in _PERMANENT_FORBIDDEN:
        return reason
    return None


def _quarantine(path: Path, digest: str, value: dict, code: str, reason: str) -> str:
    """Durably keep a rejected record (never overwritten or dropped) before it leaves pending.

    Returns ``new``, ``existing`` (a crash retry of the same record) or ``failed``.
    """
    rejected = path.with_name(REJECTED_NAME)
    encoded = json.dumps({"schema": 1, "rejected_at": _now(), "code": str(code)[:80], "reason": str(reason)[:80],
                          "record_sha256": digest, "record": value}, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_REJECTED_ENTRY_BYTES:
        return "failed"
    try:
        if not _safe(rejected):
            return "failed"
        with _lock(rejected):
            size = rejected.stat().st_size if rejected.exists() else 0
            if size > MAX_REJECTED_BYTES:
                return "failed"
            if size and ('"record_sha256":"' + digest + '"').encode() in rejected.read_bytes():
                return "existing"
            if size + len(encoded) + 1 > MAX_REJECTED_BYTES:
                return "failed"
            descriptor = os.open(rejected, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "ab") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            _fsync_dir(rejected.parent)
            return "new"
    except (OSError, ValueError):
        return "failed"


def _snapshot(path: Path, limit: int) -> tuple[list[tuple[str, dict]], set[str], bool]:
    """Up to ``limit`` valid pending records, malformed digests, and whether that was every line."""
    if not path.exists():
        return [], set(), True
    if not _safe(path) or path.stat().st_size > MAX_OUTBOX_BYTES:
        raise ValueError("unsafe technical report outbox")
    result, malformed = [], set()
    with _lock(path):
        lines = path.read_bytes().splitlines()
        window = max(1, min(limit, MAX_REPLAY))
        for line in lines[:window]:
            digest = hashlib.sha256(line).hexdigest()
            if len(line) > MAX_RECORD_BYTES:
                malformed.add(digest)
                continue
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                malformed.add(digest)
                continue
            if not _valid(value):
                malformed.add(digest)
                continue
            result.append((digest, value))
    return result, malformed, len(lines) <= window


def _base(registration: dict, session_id: str, component: str, operation: str,
          error_code: str, *, call_site=None, retryable=False, terminal=False,
          attempts=1, recovered=False, observed_at=None) -> dict:
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 200:
        raise ValueError("invalid session")
    values = {
        "observation_id": str(uuid.uuid4()),
        "observed_at": observed_at or _now(),
        "component": str(component)[:80], "operation": str(operation)[:120],
        "error_code": str(error_code)[:80],
        "call_site": str(call_site or _DEFAULT_CALL_SITE.get((component, operation), ''))[:160],
        "retryable": bool(retryable), "terminal": bool(terminal),
        "attempts": max(0, min(int(attempts), 999)), "recovered": bool(recovered),
        "registration_id": str(registration["registration_id"]), "session_id": session_id,
        **({"recovery_for": ""} if not recovered else {}),
    }
    if (values["component"] not in _COMPONENTS or values["operation"] not in _OPERATIONS
            or values["call_site"] not in _CALL_SITES
            or not _SAFE_SYMBOL.fullmatch(values["error_code"])):
        raise ValueError("invalid technical report")
    return values


def _valid(value: dict) -> bool:
    if not isinstance(value, dict) or set(value) - {
            'observation_id', 'observed_at', 'component', 'operation', 'error_code',
            'call_site', 'retryable', 'terminal', 'attempts', 'recovered',
            'registration_id', 'session_id', 'recovery_for'}:
        return False
    try:
        uuid.UUID(value['observation_id'])
        if (not isinstance(value['registration_id'], str) or not value['registration_id']
                or len(value['registration_id']) > 200 or not isinstance(value['session_id'], str)): return False
        if not 1 <= len(value['session_id']) <= 200 or any(ord(c) < 33 or ord(c) == 127 for c in value['session_id']): return False
        if not isinstance(value['observed_at'], str) or datetime.fromisoformat(value['observed_at']).tzinfo is None: return False
        if value['component'] not in _COMPONENTS or value['operation'] not in _OPERATIONS: return False
        if not isinstance(value['error_code'], str) or not _SAFE_SYMBOL.fullmatch(value['error_code']): return False
        if not isinstance(value['call_site'], str) or value['call_site'] not in _CALL_SITES: return False
        if type(value['retryable']) is not bool or type(value['terminal']) is not bool or type(value['recovered']) is not bool: return False
        if type(value['attempts']) is not int or not 0 <= value['attempts'] <= 999: return False
        if value.get('recovery_for') and not isinstance(value['recovery_for'], str): return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


def replay(client, registration: dict, *, limit=MAX_REPLAY) -> int:
    """Try a bounded batch; only accepted/ignored/quarantined records leave the outbox.

    A record the service rejects for itself alone (schema or an exact binding
    reason) is quarantined with its reason and the batch continues. Any other
    failure keeps the record pending and ends this attempt.
    """
    try:
        path = _path(registration)
        _compact(path)
        records, malformed, complete = _snapshot(path, limit)
    except (OSError, ValueError, TypeError, KeyError):
        return 0
    acknowledged = set(malformed)
    if malformed:
        _health(path, "malformed", len(malformed))
    recovered, quarantined = [], []
    quarantine_failed = broke = False
    started = time.monotonic()
    prior_timeout = getattr(client, "timeout", None)
    if isinstance(prior_timeout, (int, float)):
        client.timeout = min(prior_timeout, 1)
    for digest, value in records:
        if time.monotonic() - started >= REPLAY_SECONDS:
            broke = True
            break
        try:
            observation = {key: item for key, item in value.items() if key not in _LOCAL_ONLY}
            wire = {"registration_id": value["registration_id"],
                    "session_id": value["session_id"], "observation": observation}
            result = client.post("/api/agent/technical-observation", wire)
            if (isinstance(result, dict) and result.get("status") == "accepted"
                    and result.get("observation_id") == value["observation_id"]):
                acknowledged.add(digest)
                if value.get("recovered"):
                    recovered.append(value)
                elif not _repair_marker(path, value):
                    # A marker write may have failed before the service came back. The
                    # check and any repair happen under the outbox lock; while a needed
                    # repair cannot be made the accepted failure stays queued so its
                    # episode is still found.
                    acknowledged.discard(digest)
            else:
                broke = True
                break
        except Exception as exc:
            # Includes malformed service responses and network failures.  The
            # original record stays durable for the next background pass.
            reason = _permanent_rejection(exc)
            if reason is None:
                broke = True
                break
            outcome = _quarantine(path, digest, value, getattr(exc, 'code', ''), reason)
            if outcome == "failed":
                # Keep the original pending and surface the degraded local state;
                # a later healthy record in this batch may still progress.
                quarantine_failed = True
                _health(path, "quarantine_failed", 1)
                _health(path, "quarantine_blocked", value=1)
                continue
            acknowledged.add(digest)
            quarantined.append(value)
            if outcome == "new":
                _health(path, "rejected", 1)
    if isinstance(prior_timeout, (int, float)):
        client.timeout = prior_timeout
    if complete and not broke and not quarantine_failed:
        # Every pending record was examined and none failed to quarantine, so nothing is
        # blocked any more (even if the stuck record left pending another way).
        _health(path, "quarantine_blocked", value=0)
    try:
        _ack(path, acknowledged)
        for value in recovered:
            _clear_active(path, value)
        # Durable quarantine retires this undeliverable local report attempt and its
        # version-matched marker. It does not mean the service accepted or recovered
        # anything; the retained evidence still needs reconciliation.
        for value in quarantined:
            _clear_active(path, value)
    except (OSError, ValueError):
        pass
    return len(acknowledged)


def replay_http(registration: dict, *, limit=MAX_REPLAY) -> int:
    """Stdlib-only replay port used by the host supervisor."""
    try:
        from urllib.request import HTTPRedirectHandler, Request, build_opener
        from urllib.parse import urlsplit
        endpoint = urlsplit(registration["service_url"])
        credential = Path(registration["workspace"]) / ".qorqut/credential"
        if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or endpoint.path.rstrip("/") not in {"", "/mcp"}
                or not _safe(credential) or not credential.is_file()
                or credential.stat().st_mode & 0o077):
            return 0
        token = credential.read_text().strip()
        if not 32 <= len(token) <= 512 or any(char.isspace() or ord(char) < 33 for char in token):
            return 0
        class NoRedirects(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        class Client:
            def post(self, route, value):
                request = Request(f"{endpoint.scheme}://{endpoint.netloc}{route}",
                    data=json.dumps(value, separators=(",", ":")).encode(),
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
                try:
                    with build_opener(NoRedirects()).open(request, timeout=1) as response:
                        raw = response.read(16385)
                except HTTPError as exc:
                    try:
                        raw = exc.read(16385)
                        payload = json.loads(raw)
                        error = payload.get('error', {}) if isinstance(payload, dict) else {}
                        code = error.get('code') if isinstance(error, dict) else None
                        details = error.get('details') if isinstance(error, dict) else None
                        reason = details.get('reason') if isinstance(details, dict) else None
                    except (OSError, ValueError, TypeError):
                        code = reason = None
                    if code in {'validation_error', 'forbidden'}:
                        # Only the exact safe reason crosses; replay decides if it is per-record.
                        raise _RemoteError(code, {'reason': reason} if isinstance(reason, str)
                                           and _SAFE_REASON.fullmatch(reason) else None) from None
                    raise
                if len(raw) > 16384:
                    raise OSError('oversized response')
                payload = json.loads(raw)
                return payload.get("data", payload) if isinstance(payload, dict) else {}
        return replay(Client(), registration, limit=limit)
    except Exception:
        return 0


def record(registration: dict, session_id: str, component: str, operation: str,
           error_code: str, *, call_site=None, retryable=False, terminal=False,
           attempts=1, recovered=False, observed_at=None, client=None) -> bool:
    """Append one bounded report, then make one best-effort replay attempt."""
    if not recovered and str(error_code).lower() in _EXPECTED:
        return False
    try:
        value = _base(registration, session_id, component, operation, error_code,
                      call_site=call_site, retryable=retryable, terminal=terminal,
                      attempts=attempts, recovered=recovered, observed_at=observed_at)
        path = _path(registration)
        if recovered:
            marker = _read_marker(path, value)
            target = marker['observation_id'] if marker else None
            if not target:
                # Marker missing or unreadable: bind to the newest valid queued failure of
                # this exact identity so the episode still coalesces. No match means an
                # unknown target, which is never invented.
                target = _newest_failure(path, value)
            if target:
                value['recovery_for'] = target
                value['observation_id'] = _recovery_id(value)
        outcome = _append(path, value)
        drained = False
        if outcome == _FULL:
            # A full outbox must not stall: compact/drain first, then retry the
            # exact same observation (identity and timestamp) once.
            drained = True
            if client is not None:
                replay(client, registration)
            else:
                _compact(path)
            outcome = _append(path, value)
        saved = outcome in (_SAVED, _DUPLICATE)
        if client is not None and not drained:
            # Attempt the bounded replay even if this append was refused.
            replay(client, registration)
        if not saved:
            _health(path, "dropped", 1)
            if outcome == _FULL:
                _health(path, "queue_full", 1)
        return saved
    except (OSError, ValueError, TypeError, KeyError):
        return False


def pending(registration: dict) -> int:
    try:
        path = _path(registration)
        records, _, _ = _snapshot(path, MAX_REPLAY)
        return len(records)
    except (OSError, ValueError, TypeError, KeyError):
        return 0


def active(registration: dict, component: str, operation: str, session_id: str | None = None) -> bool:
    try:
        path = _path(registration)
        value = {"registration_id": registration["registration_id"], "component": component,
                 "operation": operation, "session_id": session_id or ""}
        target = _marker(path, value)
        if target.exists() and _safe(target):
            return True
        return _newest_failure(path, value) is not None
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _read_bounded(path: Path, limit: int) -> bytes | None:
    """Read a regular, non-symlinked file without creating or changing anything."""
    try:
        if not _safe(path) or not path.is_file():
            return None
        with open(path, "rb") as stream:
            return stream.read(limit + 1)
    except OSError:
        return None


def diagnostics(registration: dict) -> dict:
    """Bounded read-only health of this folder's outbox: counts and capacity only.

    Never returns a record body, credential or service-supplied text, takes no
    lock and creates nothing. ``status`` reflects the present condition;
    ``history`` holds the cumulative local counters.
    """
    path = _path(registration)
    raw = _read_bounded(path, MAX_OUTBOX_BYTES)
    lines = raw.splitlines() if raw else []
    episodes, duplicates, unknown, failures, recoveries, invalid = set(), 0, 0, 0, 0, 0
    for line in lines:
        try:
            value = json.loads(line) if len(line) <= MAX_RECORD_BYTES else None
        except (TypeError, ValueError):
            value = None
        if not _valid(value):
            invalid += 1
        elif not value["recovered"]:
            failures += 1
        else:
            recoveries += 1
            key = _episode(value)
            if key is None:
                unknown += 1
            elif key in episodes:
                duplicates += 1
            else:
                episodes.add(key)
    try:
        pending_bytes = path.stat().st_size if _safe(path) and path.is_file() else 0
    except OSError:
        pending_bytes = 0
    rejected_path = path.with_name(REJECTED_NAME)
    rejected_raw = _read_bounded(rejected_path, MAX_REJECTED_BYTES)
    try:
        rejected_bytes = rejected_path.stat().st_size if _safe(rejected_path) and rejected_path.is_file() else 0
    except OSError:
        rejected_bytes = 0
    health_raw = _read_bounded(path.with_name("health.json"), 1024)
    try:
        counters = json.loads(health_raw) if health_raw and len(health_raw) <= 1024 else {}
    except (TypeError, ValueError):
        counters = {}
    history = {key: item for key, item in counters.items() if type(item) is int} if isinstance(counters, dict) else {}
    markers, unreadable = [], 0
    directory = path.parent / "active"
    try:
        names = sorted(name for name in os.listdir(directory) if name.endswith(".json"))[:MAX_MARKERS] \
            if _safe(directory) and directory.is_dir() else []
    except OSError:
        names = []
    for name in names:
        content = _read_bounded(directory / name, 1024)
        try:
            marker = json.loads(content) if content and len(content) <= 1024 else None
        except (TypeError, ValueError):
            marker = None
        if _readable_marker(marker) is not None:
            markers.append({key: str(marker.get(key, ""))[:200]
                            for key in ("component", "operation", "session_id", "observation_id")})
        else:
            unreadable += 1
    queue_full = pending_bytes + MAX_RECORD_BYTES + 1 > MAX_OUTBOX_BYTES
    rejected_full = rejected_bytes + MAX_REJECTED_ENTRY_BYTES + 1 > MAX_REJECTED_BYTES
    blocked = bool(history.get("quarantine_blocked")) and bool(lines)
    rejected_count = len(rejected_raw.splitlines()) if rejected_raw else 0
    # Retained rejected evidence is unresolved until reconciled, even when pending is
    # empty and rejected storage is not full; a cumulative count alone would hide it.
    degraded = [reason for reason, present in (
        ("queue_full", queue_full), ("rejected_full", rejected_full), ("quarantine_failed", blocked),
        ("rejected_pending_review", rejected_count > 0), ("pending_unreadable", invalid > 0),
        ("active_marker_unreadable", unreadable > 0)) if present]
    return {
        "schema": 1,
        "status": "degraded" if degraded else "ok",
        "degraded": degraded,
        "pending": {"count": len(lines), "bytes": pending_bytes, "max_bytes": MAX_OUTBOX_BYTES,
                    "failures": failures, "recoveries": recoveries, "recovery_episodes": len(episodes),
                    "duplicate_recoveries": duplicates, "unknown_target_recoveries": unknown,
                    "invalid": invalid, "full": queue_full},
        "rejected": {"count": rejected_count, "pending_review": rejected_count > 0,
                     "bytes": rejected_bytes, "max_bytes": MAX_REJECTED_BYTES, "full": rejected_full},
        "active_markers": markers, "unreadable_markers": unreadable,
        "history": history,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only summary of this agent folder's technical-report outbox.")
    parser.add_argument("--registration", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw = _read_bounded(args.registration, 1024 * 1024)
        if raw is None or len(raw) > 1024 * 1024:
            raise ValueError("unreadable registration")
        print(json.dumps(diagnostics(json.loads(raw)), indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print("Cannot read the prepared registration or its technical-report outbox.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
