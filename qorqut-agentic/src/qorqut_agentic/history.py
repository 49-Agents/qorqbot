"""Non-destructive Git history for the dedicated authoring repository.

Git runs with an explicit executable and a scrubbed environment: no global or
system configuration, no hooks, no external diff/textconv, no fsmonitor and no
prompts. The layer never pushes, resets, cleans, or rewrites history.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .authoring import Authoring, Batch, BuildContext, FileOp
from .changes import Changes
from .core import LayerContext
from .errors import GitError, InvalidRequestError, NotFoundError
from .files import split_path
from .layout import require_editable
from .util import sha256_hex

MAX_DIFF_BYTES = 256 * 1024
MAX_UNTRACKED_DIFFS = 200
STDERR_EXCERPT = 2000
_GIT_OPTIONS = [
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "core.quotepath=off",
    "-c", "core.untrackedCache=false",
    "-c", "commit.gpgsign=false",
    "-c", "gc.auto=0",
    "-c", "maintenance.auto=false",
    "-c", "advice.detachedHead=false",
]


class GitRunner:
    """Runs one explicit Git executable inside the authoring root."""

    def __init__(self, executable: str | None, root: Path | str, home: Path | str) -> None:
        self.executable = executable
        self.root = os.fspath(root)
        self.home = os.fspath(home)

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": os.defpath,
            "HOME": self.home,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_ATTR_NOSYSTEM": "1",
            "LC_ALL": "C",
        }
        env.update(extra or {})
        # Never discover a parent repository, including during the config probe
        # before `init`. Caller-supplied commit identity cannot lift this bound.
        env["GIT_CEILING_DIRECTORIES"] = os.path.dirname(os.path.realpath(self.root))
        return env

    def run(
        self,
        args: list[str],
        *,
        input: bytes | None = None,
        check: bool = True,
        ok_codes: tuple[int, ...] = (0,),
        env: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> subprocess.CompletedProcess[bytes]:
        if not self.executable:
            raise GitError("no Git executable is configured", code="git_unavailable")
        command = [self.executable, "-C", self.root, *_GIT_OPTIONS, *args]
        try:
            if args[:1] != ["init"]:
                ownership = subprocess.run(
                    [self.executable, "-C", self.root, *_GIT_OPTIONS, "rev-parse", "--show-toplevel"],
                    capture_output=True, env=self.env(env), timeout=timeout, check=False,
                )
                top = os.fsdecode(ownership.stdout).rstrip("\n")
                if ownership.returncode != 0 or os.path.realpath(top) != os.path.realpath(self.root):
                    raise GitError(
                        "the authoring root is not the top level of a usable Git working tree",
                        details={"root": self.root},
                    )
            # Local clean/process filters are executable repository configuration,
            # just like hooks. Disable them even for status and add operations.
            config = subprocess.run(
                [self.executable, "-C", self.root, *_GIT_OPTIONS, "config", "--local", "--name-only",
                 "--null", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$"],
                capture_output=True, env=self.env(), timeout=timeout, check=False,
            )
            overrides = []
            for key in config.stdout.decode("utf-8", "replace").split("\0"):
                if key:
                    overrides.extend(["-c", key + ("=false" if key.endswith(".required") else "=")])
            command = [self.executable, "-C", self.root, *_GIT_OPTIONS, *overrides, *args]
            completed = subprocess.run(  # noqa: S603 - explicit executable, no shell
                command, input=input, capture_output=True, env=self.env(env), timeout=timeout, check=False
            )
        except FileNotFoundError as exc:
            raise GitError(f"the Git executable cannot be run: {exc}", code="git_unavailable",
                           details={"executable": self.executable}) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError("git timed out", details={"args": args[:1], "timeout_seconds": timeout}) from exc
        if check and completed.returncode not in ok_codes:
            raise GitError(
                f"git {args[0]} failed with exit code {completed.returncode}",
                details={
                    "args": args[:1],
                    "returncode": completed.returncode,
                    "stderr": completed.stderr.decode("utf-8", "replace")[:STDERR_EXCERPT],
                },
            )
        return completed

    def text(self, args: list[str], **kwargs: Any) -> str:
        return self.run(args, **kwargs).stdout.decode("utf-8", "replace")


def _check_paths(paths: Any, field: str = "paths") -> list[str]:
    if paths is None:
        return []
    if not isinstance(paths, list) or not paths:
        raise InvalidRequestError(f"{field} must be a non-empty list of paths when given")
    cleaned = []
    for path in paths:
        split_path(path, field=field)
        if path not in cleaned:
            cleaned.append(path)
    return cleaned


def _check_revision_text(revision: Any, field: str = "revision") -> str:
    if not isinstance(revision, str) or not revision or revision.startswith("-") or len(revision) > 200 \
            or any(ord(ch) < 33 or ord(ch) == 127 for ch in revision):
        raise InvalidRequestError(f"{field} must be a revision name or object ID", details={"field": field})
    return revision


class History:
    """Public history operations (``layer.history``)."""

    def __init__(self, ctx: LayerContext, changes: Changes, authoring: Authoring) -> None:
        self.ctx = ctx
        self.changes = changes
        self.authoring = authoring
        self.git = GitRunner(ctx.git_executable, ctx.root, ctx.store.scratch_dir / "git-home")

    # ---------------------------------------------------------------- helpers
    def _resolve_commit(self, revision: str, field: str = "revision") -> str:
        revision = _check_revision_text(revision, field)
        completed = self.git.run(["rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}"],
                                 check=False)
        if completed.returncode != 0:
            raise NotFoundError(f"no commit matches {revision!r}", details={"revision": revision})
        return completed.stdout.decode().strip()

    def _head(self) -> str | None:
        completed = self.git.run(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], check=False)
        return completed.stdout.decode().strip() if completed.returncode == 0 else None

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        """Branch, HEAD and per-path index/worktree state (``git status``, read-only)."""
        raw = self.git.run(["status", "--porcelain=v2", "--branch", "--untracked-files=all", "-z"]).stdout
        fields = raw.decode("utf-8", "replace").split("\0")
        branch = head = None
        detached = False
        entries: list[dict[str, Any]] = []
        index = 0
        while index < len(fields):
            record = fields[index]
            index += 1
            if not record:
                continue
            if record.startswith("# branch.oid "):
                value = record.split(" ", 2)[2]
                head = None if value == "(initial)" else value
            elif record.startswith("# branch.head "):
                value = record.split(" ", 2)[2]
                detached = value == "(detached)"
                branch = None if detached else value
            elif record.startswith("1 "):
                parts = record.split(" ", 8)
                entries.append({"path": parts[8], "index": parts[1][0], "worktree": parts[1][1], "orig_path": None})
            elif record.startswith("2 "):
                parts = record.split(" ", 9)
                orig = fields[index] if index < len(fields) else None
                index += 1
                entries.append({"path": parts[9], "index": parts[1][0], "worktree": parts[1][1], "orig_path": orig})
            elif record.startswith("u "):
                parts = record.split(" ", 10)
                entries.append({"path": parts[10], "index": "U", "worktree": "U", "orig_path": None})
            elif record.startswith("? "):
                entries.append({"path": record[2:], "index": "?", "worktree": "?", "orig_path": None})
        return {
            "branch": branch,
            "head": head,
            "detached": detached,
            "clean": not entries,
            "entries": entries,
            "recovery_required": bool(self.ctx.pending_change_ids()),
        }

    # -------------------------------------------------------------------- log
    def log(self, limit: int = 20, paths: list[str] | None = None, revision: str | None = None,
            include_files: bool = False) -> dict[str, Any]:
        """Recent commits, newest first (empty for a repository without commits)."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise InvalidRequestError("limit must be an integer between 1 and 200")
        selected = _check_paths(paths)
        start = self._resolve_commit(revision) if revision is not None else self._head()
        if start is None:
            return {"commits": [], "head": None}
        args = ["log", f"--max-count={limit}", "-z", "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%s", start, "--"]
        raw = self.git.run([*args, *selected]).stdout.decode("utf-8", "replace")
        commits = []
        for record in filter(None, raw.split("\0")):
            oid, name, email, date, subject = record.split("\x1f", 4)
            item: dict[str, Any] = {"commit": oid, "author_name": name, "author_email": email, "date": date,
                                    "subject": subject}
            if include_files:
                item["files"] = self._commit_files(oid)
            commits.append(item)
        return {"commits": commits, "head": self._head()}

    def _commit_files(self, oid: str) -> list[dict[str, str]]:
        raw = self.git.run(["diff-tree", "--root", "--no-commit-id", "--name-status", "-r", "-z", "--no-renames", oid]).stdout
        fields = raw.decode("utf-8", "replace").split("\0")
        return [{"status": fields[i], "path": fields[i + 1]} for i in range(0, len(fields) - 1, 2)]

    # ------------------------------------------------------------------- diff
    def diff(
        self,
        from_revision: str | None = None,
        to_revision: str | None = None,
        staged: bool = False,
        paths: list[str] | None = None,
        include_untracked: bool = True,
        max_bytes: int = MAX_DIFF_BYTES,
    ) -> dict[str, Any]:
        """Unified diff (bounded). Default: working tree against HEAD, untracked files included."""
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1024 <= max_bytes <= 8 * 1024 * 1024:
            raise InvalidRequestError("max_bytes must be an integer between 1024 and 8388608")
        selected = _check_paths(paths)
        if staged and (from_revision or to_revision):
            raise InvalidRequestError("staged cannot be combined with revisions")
        base_args = ["--no-color", "--no-ext-diff", "--no-textconv", "--no-renames"]
        if to_revision is not None and from_revision is None:
            raise InvalidRequestError("to_revision needs from_revision")
        if from_revision is not None and to_revision is not None:
            left, right = self._resolve_commit(from_revision, "from_revision"), self._resolve_commit(to_revision, "to_revision")
            target = [left, right]
            label_from, label_to = left, right
        else:
            if from_revision is not None:
                left = self._resolve_commit(from_revision, "from_revision")
            else:
                left = self._head()
                if left is None:
                    left = self.git.text(["hash-object", "-t", "tree", "--stdin"], input=b"").strip()
            target = [left] if not staged else ["--cached", left]
            label_from, label_to = left, "index" if staged else "working-tree"
        pathspec = ["--", *selected]
        patch = self.git.run(["diff", *base_args, *target, *pathspec]).stdout
        numstat = self.git.run(["diff", "--numstat", "-z", *base_args, *target, *pathspec]).stdout
        names = self.git.run(["diff", "--name-status", "-z", *base_args, *target, *pathspec]).stdout
        status_by_path: dict[str, str] = {}
        fields = names.decode("utf-8", "replace").split("\0")
        for i in range(0, len(fields) - 1, 2):
            status_by_path[fields[i + 1]] = fields[i]
        files = []
        for record in filter(None, numstat.decode("utf-8", "replace").split("\0")):
            added, deleted, path = record.split("\t", 2)
            files.append({
                "path": path,
                "status": status_by_path.get(path, "M"),
                "additions": None if added == "-" else int(added),
                "deletions": None if deleted == "-" else int(deleted),
            })
        chunks = [patch]
        if include_untracked and not staged and to_revision is None:
            untracked = [e["path"] for e in self.status()["entries"] if e["index"] == "?"]
            if selected:
                untracked = [p for p in untracked if any(p == s or p.startswith(s.rstrip("/") + "/") for s in selected)]
            for path in untracked[:MAX_UNTRACKED_DIFFS]:
                completed = self.git.run(["diff", "--no-index", *base_args, "--", os.devnull, path], ok_codes=(0, 1))
                chunks.append(completed.stdout)
                added = sum(1 for line in completed.stdout.split(b"\n") if line.startswith(b"+") and not line.startswith(b"+++"))
                files.append({"path": path, "status": "untracked", "additions": added, "deletions": 0})
        data = b"".join(chunks)
        truncated = len(data) > max_bytes
        text = data[:max_bytes].decode("utf-8", "replace")
        return {"from": label_from, "to": label_to, "diff": text, "truncated": truncated, "files": files}

    # ----------------------------------------------------------------- commit
    def commit(self, message: str, paths: list[str] | None = None, author: dict[str, str] | None = None) -> dict[str, Any]:
        """Commit selected paths (or every change when ``paths`` is omitted).

        With ``paths`` only those files are committed; unrelated staged work stays
        staged. No hooks run. Existing change events gain the commit reference.
        """
        if not isinstance(message, str) or not message.strip() or len(message) > 20000:
            raise InvalidRequestError("message must be a non-empty string of at most 20000 characters")
        selected = _check_paths(paths)
        env: dict[str, str] = {}
        if author is not None:
            if not isinstance(author, dict) or set(author) != {"name", "email"} or not all(
                isinstance(v, str) and v for v in author.values()
            ):
                raise InvalidRequestError("author must be {\"name\": ..., \"email\": ...}")
            env = {
                "GIT_AUTHOR_NAME": author["name"], "GIT_AUTHOR_EMAIL": author["email"],
                "GIT_COMMITTER_NAME": author["name"], "GIT_COMMITTER_EMAIL": author["email"],
            }
        with self.ctx.store.lock(exclusive=True):
            self.ctx.require_no_recovery()
            self.ctx.require_no_check_mutation()
            if author is None:
                for key in ("user.name", "user.email"):
                    completed = self.git.run(["config", "--local", "--get", key], check=False)
                    if completed.returncode != 0 or not completed.stdout.strip():
                        raise GitError(
                            "a commit needs an author: pass author or configure user.name and user.email in the repository",
                            code="git_identity_required",
                        )
            pathspec = ["--", *selected]
            pending = self.git.run(["status", "--porcelain=v1", "-z", "--untracked-files=all", *pathspec]).stdout
            if not pending:
                raise GitError("the selected paths have no changes to commit", code="nothing_to_commit",
                               details={"paths": selected})
            # With rename detection disabled, both old and new names are checked.
            raw = self.git.run(["-c", "status.renames=false", "status", "--porcelain=v1", "-z", "--untracked-files=all", *pathspec]).stdout
            candidates = [os.fsdecode(record[3:]) for record in raw.split(b"\0") if record]
            require_editable(candidates)
            self.git.run(["add", "-A", *pathspec], env=env)
            commit_args = ["commit", "--no-verify", "--no-gpg-sign", "-q", "-F", "-"]
            if selected:
                commit_args += ["--only", *pathspec]
            self.git.run(commit_args, input=(message.rstrip("\n") + "\n").encode("utf-8"), env=env)
            oid = self._head()
            assert oid is not None
            files = self._commit_files(oid)
            committed: dict[str, str | None] = {}
            for item in files:
                if item["status"].startswith("D"):
                    committed[item["path"]] = None
                else:
                    blob = self.git.run(["cat-file", "blob", f"{oid}:{item['path']}"]).stdout
                    committed[item["path"]] = sha256_hex(blob)
            linked = self.changes.link_commit(oid, committed)
        return {"commit": oid, "message": message, "files": files, "linked_changes": linked}

    # ---------------------------------------------------------------- restore
    def restore(
        self,
        attribution: dict[str, Any],
        revision: str,
        paths: list[str],
        expected_sha256: dict[str, str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Re-create selected files as they were at ``revision``, as a new ordinary change.

        Nothing in Git moves: the files are written through the normal expected-hash
        batch (so a concurrent edit is a conflict) and recorded as a ``restore``
        event; commit the result like any other change. ``expected_sha256`` may pin
        current file hashes per path; otherwise the hashes found under the lock are used.
        """
        selected = _check_paths(paths)
        if not selected:
            raise InvalidRequestError("paths must name at least one file to restore")
        require_editable(selected)
        pinned = expected_sha256 or {}
        if not isinstance(pinned, dict) or any(k not in selected for k in pinned):
            raise InvalidRequestError("expected_sha256 may only name restored paths")

        resolved: dict[str, str] = {}

        def build(bctx: BuildContext) -> Batch:
            oid = resolved["revision"] = self._resolve_commit(revision)
            ops: list[FileOp] = []
            for path in selected:
                listing = self.git.run(["ls-tree", "-z", oid, "--", path]).stdout.decode("utf-8", "replace")
                record = next(iter(filter(None, listing.split("\0"))), None)
                if record is None:
                    raise NotFoundError(f"{path} does not exist at revision {oid}",
                                        details={"path": path, "revision": oid})
                meta, _, name = record.partition("\t")
                mode, kind, blob_id = meta.split(" ")
                if name != path or kind != "blob" or mode not in ("100644", "100755"):
                    raise NotFoundError(f"{path} is not a regular file at revision {oid}",
                                        details={"path": path, "revision": oid, "mode": mode})
                content = self.git.run(["cat-file", "blob", blob_id]).stdout
                current = self.ctx.tree.read(path) if self.ctx.tree.kind(path) == "file" else None
                if current is None:
                    if path in pinned:
                        ops.append(FileOp(path, "edit", content, pinned[path], mode == "100755"))
                    else:
                        ops.append(FileOp(path, "create", content, None, mode == "100755"))
                else:
                    ops.append(FileOp(path, "edit", content, pinned.get(path, sha256_hex(current)), mode == "100755"))
            return Batch("restore", ops, details={"revision": oid, "paths": selected}, source_refs=[f"git:{oid}"])

        result = self.authoring._run("restore", attribution, build, dry_run=dry_run)
        result["revision"] = resolved["revision"]
        return result

