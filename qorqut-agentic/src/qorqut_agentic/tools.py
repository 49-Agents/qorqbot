"""Custom tool invocation, receipts and fixture tests (``layer.tools``).

Honesty rules implemented here:

* Arguments are validated before dispatch; output is validated before success.
* A receipt is written (durably, ``in_flight``) *before* the runtime is called, so
  a retry with the same invocation ID can never run the effect a second time.
* A call interrupted by a crash is reported ``indeterminate`` until the caller
  reconciles it; exactly-once external execution is never claimed.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

from .catalog import Catalog, Entry, Selection, bundle_digest, load_catalog
from .changes import normalize_actor
from .core import LayerContext
from .errors import (
    InvalidRequestError,
    NotFoundError,
    OperationConflictError,
    RevisionConflictError,
    RuntimeUnavailableError,
    StateConflictError,
    UnavailableError,
    ValidationFailedError,
)
from .files import DiskSource
from .runtime import ExecutionRequest, ExecutionResult
from .schemas import TOOL_DEFINITION_SCHEMA, validate_instance
from .util import canonical_json, check_operation_id, dumps, is_ref, json_digest, loads, sha256_hex

DIAGNOSTIC_STDERR_BYTES = 8192
DIAGNOSTIC_STDOUT_EXCERPT = 1024
RECONCILE_RESOLUTIONS = ("effect_confirmed", "no_effect_confirmed")


def _process_token() -> str:
    """Identify this process: host, pid and start time (guards against pid reuse)."""
    if sys.platform == "darwin":
        import psutil
        start = str(int(psutil.Process().create_time() * 1_000_000))
        return f"{socket.gethostname()}:{os.getpid()}:{start}"
    start = ""
    try:
        with open("/proc/self/stat", "rb") as handle:
            start = handle.read().rsplit(b")", 1)[1].split()[19].decode()
    except (OSError, IndexError):
        pass
    return f"{socket.gethostname()}:{os.getpid()}:{start}"


def _runner_alive(token: str | None) -> bool:
    """False only when the runner is known to be gone (same host, no such process)."""
    if not token or (sys.platform != "darwin" and not os.path.isdir("/proc/self")):
        return True
    try:
        host, pid_text, start = token.rsplit(":", 2)
        pid = int(pid_text)
    except ValueError:
        return True
    if host != socket.gethostname():
        return True
    if sys.platform == "darwin":
        import psutil
        try:
            current = str(int(psutil.Process(pid).create_time() * 1_000_000))
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True
        return not start or current == start
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            current = handle.read().rsplit(b")", 1)[1].split()[19].decode()
    except (OSError, IndexError):
        return False
    return not start or current == start


def _bounded_text(data: bytes, limit: int) -> str:
    return data[:limit].decode("utf-8", "replace")


class Tools:
    """Public tool operations."""

    def __init__(self, ctx: LayerContext) -> None:
        self.ctx = ctx

    # ---------------------------------------------------------------- lookup
    def locate(self, catalog: Catalog, ref: str, selection: Selection) -> Entry:
        if not is_ref(ref):
            raise InvalidRequestError("ref must be package-id/entry-id", details={"field": "ref"})
        entry = catalog.get(ref)
        if entry is None:
            raise NotFoundError(f"no valid catalog entry {ref!r}", details={"ref": ref,
                                "issues": [i.to_dict() for i in catalog.issues if i.entry_ref == ref][:20]})
        if entry.kind != "tool":
            raise InvalidRequestError(f"{ref} is a {entry.kind}, not a tool", details={"ref": ref})
        available, diagnostics = catalog.availability(entry, selection)
        if not available:
            raise UnavailableError(f"tool {ref} is unavailable: {'; '.join(diagnostics)}",
                                   details={"ref": ref, "diagnostics": diagnostics})
        return entry

    # ---------------------------------------------------------------- invoke
    def invoke(
        self,
        ref: str,
        arguments: Any,
        invocation_id: str,
        actor: dict[str, str],
        selection: dict[str, Any] | None = None,
        expected_definition_revision: str | None = None,
        expected_code_revision: str | None = None,
    ) -> dict[str, Any]:
        """Run a tool once. The same ``invocation_id`` always returns the same receipt.

        Argument or schema problems raise ``validation_failed`` (nothing ran); a
        missing runtime raises ``runtime_unavailable`` (nothing ran, no receipt).
        Once dispatched, the outcome, including failure, is the receipt's status.
        """
        return {"receipt": self._invoke(ref, arguments, invocation_id, actor, selection,
                                        expected_definition_revision, expected_code_revision, "invoke")}

    def _invoke(self, ref, arguments, invocation_id, actor, selection, expected_definition, expected_code,
                purpose) -> dict[str, Any]:
        ctx = self.ctx
        check_operation_id(invocation_id, "invocation_id")
        actor = normalize_actor(actor)
        chosen = Selection.from_value(selection)
        try:
            fingerprint = json_digest({"ref": ref, "arguments": arguments, "actor": actor,
                                       "expected_definition_revision": expected_definition,
                                       "expected_code_revision": expected_code})
            stdin = canonical_json(arguments).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise InvalidRequestError(f"arguments must be a JSON value: {exc}", details={"field": "arguments"}) from exc
        existing = self._fetch(invocation_id, actor=actor, selection=chosen)
        if existing is not None:
            return self._replay(existing, fingerprint)
        scratch = self._scratch_dir(invocation_id)
        try:
            with ctx.store.lock(exclusive=False):
                ctx.require_no_recovery()
                catalog = load_catalog(DiskSource(ctx.tree))
                entry = self.locate(catalog, ref, chosen)
                definition = entry.definition
                assert definition is not None
                runtime = ctx.runtimes.get(definition["runtime"])
                if runtime is None:
                    raise RuntimeUnavailableError(
                        f"no runtime is registered for alias {definition['runtime']!r}",
                        details={"ref": ref, "runtime": definition["runtime"], "registered": sorted(ctx.runtimes)})
                problems = validate_instance(arguments, definition["input_schema"])
                if problems:
                    raise ValidationFailedError(f"arguments do not match the input schema of {ref}",
                                                details={"issues": problems, "ref": ref})
                package_dir, work_dir, code_path, code_revision = self._stage(catalog, entry, scratch)
            if expected_definition is not None and expected_definition != entry.revision:
                raise RevisionConflictError(
                    f"the definition of {ref} changed", details={"ref": ref, "kind": "definition",
                    "expected": expected_definition, "actual": entry.revision})
            if expected_code is not None and expected_code != code_revision:
                raise RevisionConflictError(
                    f"the code of {ref} changed", details={"ref": ref, "kind": "code",
                    "expected": expected_code, "actual": code_revision})
            limits = definition["limits"]
            request = ExecutionRequest(
                invocation_id=invocation_id, runtime_alias=definition["runtime"], code_path=code_path,
                package_dir=package_dir, work_dir=work_dir, stdin=stdin,
                timeout_seconds=float(limits["timeout_seconds"]), max_output_bytes=int(limits["max_output_bytes"]),
                max_stderr_bytes=int(limits["max_stderr_bytes"]),
            )
            started = ctx.now()
            try:
                with ctx.store.transaction() as conn:
                    conn.execute(
                        "INSERT INTO invocations(invocation_id, tool_ref, purpose, actor, request_sha256,"
                        " definition_revision, code_revision, status, effect_state, started_at, runner, limits)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (invocation_id, ref, purpose, dumps(actor), fingerprint, entry.revision, code_revision,
                         "in_flight", "possible", started, _process_token(), dumps(dict(limits))),
                    )
            except sqlite3.IntegrityError:
                row = self._fetch(invocation_id, actor=actor, selection=chosen)
                assert row is not None
                return self._replay(row, fingerprint)
            ctx.hook("after_receipt_recorded", invocation_id=invocation_id)
            try:
                result = runtime.execute(request)
            except Exception as exc:  # a misbehaving runtime must not leave the receipt open
                result = ExecutionResult("runtime_error", message=f"{type(exc).__name__}: {exc}")
            self._finish(invocation_id, result, definition)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        row = self._fetch(invocation_id, actor=actor, selection=chosen)
        assert row is not None
        return self._receipt(row)

    def _scratch_dir(self, invocation_id: str) -> Path:
        base = self.ctx.store.scratch_dir / "invocations"
        base.mkdir(parents=True, exist_ok=True)
        # Racing retries may both stage before one wins the receipt INSERT.
        # They must never remove or rewrite each other's staged code.
        return base / (sha256_hex(invocation_id.encode())[:24] + "-" + uuid.uuid4().hex)

    def _stage(self, catalog: Catalog, entry: Entry, scratch: Path) -> tuple[str, str, str, str]:
        """Copy the code and declared resources into a fresh scratch directory.

        The bytes copied here are the bytes hashed into the recorded code
        revision and the bytes that run; later edits cannot change them.
        """
        package_dir, work_dir = scratch / "package", scratch / "work"
        package_dir.mkdir(parents=True, mode=0o700)
        work_dir.mkdir(mode=0o700)
        package = catalog.packages[entry.package_id]
        digests = []
        for resource in entry.resources:
            data = self.ctx.tree.read(resource.root_path)
            if data is None or sha256_hex(data) != resource.sha256:
                raise StateConflictError(f"tool file changed while staging: {resource.root_path}",
                                         details={"path": resource.root_path})
            target = package_dir / resource.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            digests.append((resource.root_path, resource.sha256))
        assert entry.definition is not None
        code_path = package_dir / entry.definition["code"]
        assert package.dir
        return str(package_dir), str(work_dir), str(code_path), bundle_digest(digests)

    # -------------------------------------------------------------- results
    def _finish(self, invocation_id: str, result: ExecutionResult, definition: dict[str, Any]) -> None:
        if not isinstance(result, ExecutionResult):
            result = ExecutionResult("runtime_error", message="runtime returned an invalid result")
        if (not isinstance(result.outcome, str)
                or result.outcome not in {"exited", "spawn_failed", "runtime_error", "timeout", "output_overflow"}
                or not isinstance(result.stdout, bytes) or not isinstance(result.stderr, bytes)
                or not isinstance(result.stderr_truncated, bool)
                or not isinstance(result.limits, dict)
                or (result.outcome == "exited" and type(result.exit_code) is not int)):
            result = ExecutionResult("runtime_error", message="runtime returned an invalid outcome or stream")
        try:
            dumps(result.limits).encode("utf-8")
        except (TypeError, ValueError, RecursionError, OverflowError):
            result = ExecutionResult("runtime_error", message="runtime returned invalid limits")
        limits = definition["limits"]
        if (len(result.stdout) > limits["max_output_bytes"]
                or len(result.stderr) > limits["max_stderr_bytes"]):
            result.outcome = "output_overflow"
            result.stderr_truncated |= len(result.stderr) > limits["max_stderr_bytes"]
            result.stdout = result.stdout[:limits["max_output_bytes"]]
            result.stderr = result.stderr[:limits["max_stderr_bytes"]]
        status, failure_code, effect = "failed", None, "possible"
        output_json: str | None = None
        diagnostics: dict[str, Any] = {
            "stderr": _bounded_text(result.stderr, DIAGNOSTIC_STDERR_BYTES),
            "stderr_truncated": result.stderr_truncated or len(result.stderr) > DIAGNOSTIC_STDERR_BYTES,
            "stdout_excerpt": None,
            "message": str(result.message)[:4096] if result.message is not None else None,
            "issues": [],
        }
        if result.outcome == "spawn_failed":
            failure_code, effect = "spawn_failed", "not_started"
        elif result.outcome == "runtime_error":
            failure_code = "runtime_error"
        elif result.outcome == "timeout":
            failure_code = "timeout"
            diagnostics["message"] = diagnostics["message"] or "the tool exceeded its time limit and was terminated"
        elif result.outcome == "output_overflow":
            failure_code = "output_overflow"
            diagnostics["stdout_excerpt"] = _bounded_text(result.stdout, DIAGNOSTIC_STDOUT_EXCERPT)
            diagnostics["message"] = diagnostics["message"] or "the tool wrote more output than its limit and was terminated"
        elif result.exit_code != 0:
            failure_code = "nonzero_exit"
            diagnostics["stdout_excerpt"] = _bounded_text(result.stdout, DIAGNOSTIC_STDOUT_EXCERPT)
            diagnostics["message"] = diagnostics["message"] or f"the tool exited with status {result.exit_code}"
        else:
            try:
                parsed, problem = self._parse_output(result.stdout)
                if problem:
                    failure_code = "invalid_output"
                    diagnostics["stdout_excerpt"] = _bounded_text(result.stdout, DIAGNOSTIC_STDOUT_EXCERPT)
                    diagnostics["message"] = problem
                else:
                    issues = validate_instance(parsed, definition["output_schema"])
                    if issues:
                        failure_code = "output_schema_violation"
                        diagnostics["stdout_excerpt"] = _bounded_text(result.stdout, DIAGNOSTIC_STDOUT_EXCERPT)
                        diagnostics["message"] = "the output does not match the output schema"
                        diagnostics["issues"] = [
                            _bounded_text(issue.encode("utf-8", "replace"), 1024) for issue in issues[:20]]
                    else:
                        output_json = dumps(parsed)
                        output_json.encode("utf-8")  # SQLite TEXT must be representable, too.
                        status, effect = "succeeded", "completed"
            except Exception as exc:
                # Untrusted output may exceed the decoder/validator/encoder's
                # recursion or representation limits. The process already ran;
                # finish its receipt. Process-crash BaseExceptions still escape.
                status, effect, failure_code, output_json = "failed", "possible", "invalid_output", None
                diagnostics["stdout_excerpt"] = _bounded_text(result.stdout, DIAGNOSTIC_STDOUT_EXCERPT)
                diagnostics["message"] = (
                    f"tool output could not be processed ({type(exc).__name__}): "
                    + _bounded_text(str(exc).encode("utf-8", "replace"), 1024))
                diagnostics["issues"] = []
        if diagnostics["message"] is not None:
            diagnostics["message"] = _bounded_text(diagnostics["message"].encode("utf-8", "replace"), 4096)
        with self.ctx.store.transaction() as conn:
            conn.execute(
                "UPDATE invocations SET status = ?, failure_code = ?, effect_state = ?, ended_at = ?, exit_code = ?,"
                " output = ?, diagnostics = ?, limits = COALESCE(?, limits)"
                " WHERE invocation_id = ? AND status = 'in_flight'",
                (status, failure_code, effect, self.ctx.now(), result.exit_code, output_json, dumps(diagnostics),
                 dumps(result.limits) if result.limits else None, invocation_id),
            )

    @staticmethod
    def _parse_output(stdout: bytes) -> tuple[Any, str | None]:
        if not stdout.strip():
            return None, "the tool wrote no output; exactly one JSON value is required"
        try:
            text = stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None, "the output is not valid UTF-8"

        def reject(constant: str) -> Any:
            raise ValueError(f"{constant} is not valid JSON")

        try:
            return json.loads(text, parse_constant=reject), None
        except ValueError as exc:
            return None, f"the output is not exactly one JSON value: {exc}"

    # -------------------------------------------------------------- receipts
    @staticmethod
    def _access(row, actor, selection):
        if actor is not None and normalize_actor(actor) != loads(row["actor"]):
            raise UnavailableError("invocation is owned by another actor")
        chosen = Selection.from_value(selection)
        package_id = row["tool_ref"].split("/", 1)[0]
        if chosen.package_ids is not None and package_id not in chosen.package_ids:
            raise UnavailableError("invocation package is not allowed by this selection")

    def _fetch(self, invocation_id: str, *, actor=None, selection=None) -> sqlite3.Row | None:
        with self.ctx.store.reader() as conn:
            row = conn.execute("SELECT * FROM invocations WHERE invocation_id = ?", (invocation_id,)).fetchone()
        if row is not None:
            self._access(row, actor, selection)
        if row is not None and row["status"] == "in_flight" and not _runner_alive(row["runner"]):
            with self.ctx.store.transaction() as conn:
                conn.execute(
                    "UPDATE invocations SET status = 'indeterminate', effect_state = 'unknown', ended_at = ?"
                    " WHERE invocation_id = ? AND status = 'in_flight'",
                    (self.ctx.now(), invocation_id),
                )
            with self.ctx.store.reader() as conn:
                row = conn.execute("SELECT * FROM invocations WHERE invocation_id = ?", (invocation_id,)).fetchone()
        return row

    def _replay(self, row: sqlite3.Row, fingerprint: str) -> dict[str, Any]:
        if row["request_sha256"] != fingerprint:
            raise OperationConflictError(
                f"invocation ID {row['invocation_id']!r} was already used for a different request",
                details={"invocation_id": row["invocation_id"], "tool_ref": row["tool_ref"]})
        return self._receipt(row, replayed=True)

    @staticmethod
    def _receipt(row: sqlite3.Row, *, replayed: bool = False) -> dict[str, Any]:
        started, ended = row["started_at"], row["ended_at"]
        return {
            "invocation_id": row["invocation_id"],
            "tool_ref": row["tool_ref"],
            "purpose": row["purpose"],
            "actor": loads(row["actor"]),
            "status": row["status"],
            "failure_code": row["failure_code"],
            "effect_state": row["effect_state"],
            "definition_revision": row["definition_revision"],
            "code_revision": row["code_revision"],
            "started_at": started,
            "ended_at": ended,
            "exit_code": row["exit_code"],
            "output": loads(row["output"]),
            "diagnostics": loads(row["diagnostics"]),
            "limits": loads(row["limits"]),
            "reconciliation": loads(row["reconciliation"]),
            "replayed": replayed,
        }

    def receipt(self, invocation_id: str, *, actor=None, selection=None) -> dict[str, Any]:
        """The stored receipt; an interrupted call reads ``indeterminate``."""
        check_operation_id(invocation_id, "invocation_id")
        if actor is not None:
            actor = normalize_actor(actor)
        selection = Selection.from_value(selection)
        row = self._fetch(invocation_id, actor=actor, selection=selection)
        if row is None:
            raise NotFoundError(f"no invocation {invocation_id!r}", details={"invocation_id": invocation_id})
        return {"receipt": self._receipt(row)}

    def reconcile(self, invocation_id: str, resolution: str, note: str, actor: dict[str, str], *,
                  selection=None) -> dict[str, Any]:
        """Record what the caller learned about an ``indeterminate`` (or stuck ``in_flight``) call.

        The call is never re-run; use a new invocation ID to try again.
        """
        check_operation_id(invocation_id, "invocation_id")
        actor = normalize_actor(actor)
        if resolution not in RECONCILE_RESOLUTIONS:
            raise InvalidRequestError(f"resolution must be one of {list(RECONCILE_RESOLUTIONS)}")
        if not isinstance(note, str) or not note.strip():
            raise InvalidRequestError("note must say how the outcome was established")
        row = self._fetch(invocation_id, actor=actor, selection=selection)
        if row is None:
            raise NotFoundError(f"no invocation {invocation_id!r}", details={"invocation_id": invocation_id})
        if row["status"] not in ("indeterminate", "in_flight"):
            raise StateConflictError(f"invocation {invocation_id!r} is {row['status']}; only indeterminate calls are reconciled",
                                     details={"status": row["status"]})
        effect = "completed" if resolution == "effect_confirmed" else "not_started"
        record = {"resolution": resolution, "note": note, "actor": actor, "at": self.ctx.now()}
        with self.ctx.store.transaction() as conn:
            changed = conn.execute(
                "UPDATE invocations SET status = 'reconciled', effect_state = ?, reconciliation = ?, ended_at = COALESCE(ended_at, ?)"
                " WHERE invocation_id = ? AND status IN ('indeterminate', 'in_flight')",
                (effect, dumps(record), record["at"], invocation_id),
            ).rowcount
        if not changed:
            raise StateConflictError(f"invocation {invocation_id!r} changed state while reconciling")
        return self.receipt(invocation_id, actor=actor, selection=selection)

    # ----------------------------------------------------------------- tests
    def test(
        self,
        ref: str,
        actor: dict[str, str],
        cases: list[dict[str, Any]] | None = None,
        selection: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run explicit fixture cases (the definition's ``tests`` unless ``cases`` is given).

        Nothing runs automatically on import or catalog reads; calling this is the
        only trigger. Each case is a real invocation with its own receipt
        (``purpose: test``). A missing runtime raises ``runtime_unavailable``.
        """
        actor = normalize_actor(actor)
        chosen = Selection.from_value(selection)
        with self.ctx.store.lock(exclusive=False):
            self.ctx.require_no_recovery()
            catalog = load_catalog(DiskSource(self.ctx.tree))
            entry = self.locate(catalog, ref, chosen)
        fixtures = cases if cases is not None else (entry.definition or {}).get("tests", [])
        if not isinstance(fixtures, list):
            raise InvalidRequestError("cases must be a list")
        if not fixtures:
            raise ValidationFailedError(f"{ref} has no test cases; define tests in the definition or pass cases",
                                        details={"ref": ref, "issues": ["no test cases"]})
        issues = validate_instance(fixtures, TOOL_DEFINITION_SCHEMA["properties"]["tests"])
        if issues:
            raise InvalidRequestError("invalid tool test cases", details={"issues": issues})
        batch = uuid.uuid4().hex[:12]
        outcomes = []
        for index, case in enumerate(fixtures):
            if not isinstance(case, dict) or not {"name", "input", "expect"} <= set(case):
                raise InvalidRequestError(f"cases[{index}] needs name, input and expect")
            expect = case["expect"]
            invocation_id = f"test-{batch}-{index}"
            try:
                receipt = self._invoke(ref, case["input"], invocation_id, actor, selection, None, None, "test")
                actual: dict[str, Any] = {"status": "succeeded" if receipt["status"] == "succeeded" else "failed",
                                          "output": receipt["output"], "failure_code": receipt["failure_code"]}
            except ValidationFailedError as exc:
                receipt = None
                actual = {"status": "rejected", "error_code": exc.code, "issues": exc.details.get("issues", [])}
            passed = actual["status"] == expect.get("status")
            if passed and "output" in expect:
                passed = actual.get("output") == expect["output"]
            if passed and "failure_code" in expect:
                passed = actual.get("failure_code") == expect["failure_code"]
            if passed and "error_code" in expect:
                passed = actual.get("error_code") == expect["error_code"]
            outcomes.append({"name": case["name"], "passed": passed, "expected": expect, "actual": actual,
                             "invocation_id": receipt["invocation_id"] if receipt else None})
        return {
            "ref": ref,
            "definition_revision": entry.revision,
            "code_revision": entry.code_revision,
            "passed": all(item["passed"] for item in outcomes),
            "cases": outcomes,
        }
