"""One fixed nightly program: verified backup, exports, then optional Git history."""

import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

from bazarlyk.background.programs import ProgramResult
from qorqut_operations.contracts import DomainError, canonical_bytes, utc_now
from qorqut_operations.storage.executor import application_claim
from .exports import atomic_write, build_exports, publish_exports, registered_path
from .recovery import BackupStore, ROOT_AUTHORED, SKIP_DIRECTORIES, _identity


HISTORY_REF = "refs/heads/bazarlyk-history"
HQ_ENTRIES = frozenset({"business.yaml", "CLAUDE.md", "README.md", ".gitignore", ".gitattributes",
    "context", "assets", "marketing-content", "agents", "notes", "journal", "tasks", "requests",
    "log", "finance", "analytics", "learnings", "routines", "exports", "tables"})
PRIVATE_PARTS = SKIP_DIRECTORIES - {"exports"} | {"sessions", "transcripts", "logs", "cache", ".cache", "work", "code"}
OBJECT_ID = re.compile(r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")


def public_path(name, businesses):
    parts = Path(name).parts
    if (not parts or Path(name).is_absolute() or ".." in parts or "\\" in name
            or any(part in PRIVATE_PARTS for part in parts)
            or any(part == ".env" or part.startswith(".env.") for part in parts)
            or any(parts[i:i + 2] == ('context', 'agent-memory') for i in range(len(parts) - 1))
            or parts[-1].endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".db", ".log", ".pyc"))):
        return False
    if ".claude" in parts:
        position = parts.index(".claude")
        if parts[position + 1:position + 2] != ("skills",):
            return False
    for business in businesses:
        agents = (*Path(business["root"]).parts, "agents")
        if parts[:len(agents)] == agents:
            # A business's registered agents sit beside its HQ; the same exclusions above apply to them.
            return len(parts) > len(agents)
        hq = Path(business["hq"]).parts
        if parts[:len(hq)] == hq:
            if parts[len(hq):len(hq) + 2] == ("context", "email"):
                return False
            return len(parts) > len(hq) and parts[len(hq)] in HQ_ENTRIES
    return parts[0] in (*ROOT_AUTHORED, "exports")


class GitVersioner:
    """Commit a saved generation on a separate branch; never touch the user's index."""

    def __init__(self, root, *, runner=None):
        self.root, self.runner = Path(root).resolve(strict=True), runner or subprocess.run

    def version(self, generation, metadata, bundle, state, save, control):
        generation = Path(generation)
        manifest = json.loads((generation / "authored/manifest.json").read_text())
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Bazarlyk", GIT_COMMITTER_NAME="Bazarlyk",
                   GIT_AUTHOR_EMAIL="bazarlyk@localhost", GIT_COMMITTER_EMAIL="bazarlyk@localhost",
                   GIT_AUTHOR_DATE=metadata["created_at"], GIT_COMMITTER_DATE=metadata["created_at"])

        def git(*arguments, data=None, allowed=(0,), strip=True):
            control.check()
            command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C", str(self.root), *arguments]
            try:
                result = self.runner(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=env, timeout=min(120, control.remaining_seconds()))
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise DomainError("versioning_failed", "Nightly Git operation did not complete") from exc
            if result.returncode not in allowed:
                raise DomainError("versioning_failed", "Nightly Git operation failed; backup and exports remain available")
            output = result.stdout.decode()
            return output.strip() if strip else output, result.returncode

        def oid(value):
            if not OBJECT_ID.fullmatch(value):
                raise DomainError("versioning_failed", "Git returned an invalid object identity")
            return value

        if Path(git("rev-parse", "--show-toplevel")[0]) != self.root:
            raise DomainError("versioning_unavailable", "Nightly versioning requires the operations root's existing Git repository")
        if "branch " + HISTORY_REF in git("worktree", "list", "--porcelain", "-z", strip=False)[0].split("\0"):
            raise DomainError("versioning_unavailable", "Nightly history must not be checked out in a working tree")
        current, code = git("rev-parse", "--verify", HISTORY_REF, allowed=(0, 128))
        current = oid(current) if code == 0 else None
        if "commit" not in state:
            base = current or oid(git("rev-parse", "--verify", "HEAD")[0])
            with tempfile.TemporaryDirectory(prefix=".nightly-index-", dir=generation.parent) as temporary:
                env["GIT_INDEX_FILE"] = str(Path(temporary) / "index")
                git("read-tree", base)
                tracked = git("ls-tree", "-r", "--name-only", "-z", base, strip=False)[0].split("\0")
                selected = set()
                with tarfile.open(generation / "authored/sources.tar", "r:") as archive:
                    for member in archive:
                        if not public_path(member.name, manifest["businesses"]) or member.name in bundle.files:
                            continue
                        body = archive.extractfile(member).read()
                        blob = oid(git("hash-object", "-w", "--stdin", data=body)[0])
                        git("update-index", "--add", "--cacheinfo", "100755" if member.mode & 0o111 else "100644", blob, member.name)
                        selected.add(member.name)
                for name, body in sorted(bundle.files.items()):
                    if not public_path(name, manifest["businesses"]):
                        raise DomainError("invalid_export", "Generated projection is outside versioning's explicit data paths")
                    blob = oid(git("hash-object", "-w", "--stdin", data=body)[0])
                    git("update-index", "--add", "--cacheinfo", "100644", blob, name)
                    selected.add(name)
                for name in tracked:
                    if name and public_path(name, manifest["businesses"]) and name not in selected:
                        git("update-index", "--force-remove", "--", name)
                tree = oid(git("write-tree")[0])
                previous_tree = oid(git("rev-parse", base + "^{tree}")[0])
                if tree == previous_tree:
                    state.update(base_ref=current, base_commit=base, tree=tree, commit=base, changed=False)
                else:
                    message = f"Bazarlyk nightly snapshot {metadata['generation_id']}\n".encode()
                    commit = oid(git("commit-tree", tree, "-p", base, data=message)[0])
                    state.update(base_ref=current, base_commit=base, tree=tree, commit=commit, changed=True)
                save()
            env.pop("GIT_INDEX_FILE", None)
        commit = oid(state["commit"])
        if current != commit:
            if current != state["base_ref"]:
                raise DomainError("versioning_conflict", "Nightly history advanced; an older job cannot replace it")
            git("update-ref", HISTORY_REF, commit, current or "0" * len(commit))
        state["updated"] = True
        save()
        return {"enabled": True, "ref": HISTORY_REF, "commit": commit, "changed": state["changed"]}


class NightlyMaintenance:
    def __init__(self, database, paths, *, locks, backup_destination=None, git_enabled=False,
                 git_runner=None, clock=utc_now):
        if type(git_enabled) is not bool:
            raise ValueError("Git versioning must be explicitly enabled or disabled")
        self.database, self.paths, self.root = database, paths, paths.operations
        self.locks = locks
        self.backups = BackupStore(database, paths, backup_destination, clock=clock)
        self.git_enabled = git_enabled
        self.versioner = GitVersioner(self.root, runner=git_runner)

    def run(self, control, arguments):
        if arguments != {} or control.business_id is not None:
            raise DomainError("validation_error", "Nightly maintenance takes no arguments and runs only at installation scope")
        generation = _identity(control.job["id"])
        with application_claim(self.locks, generation):
            directory = registered_path(self.root, ".runtime/maintenance")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            receipt = registered_path(self.root, directory / (generation + ".json"))
            configuration = {"root": str(self.root), "backup_destination": str(self.backups.destination),
                             "git_enabled": self.git_enabled}
            state = json.loads(receipt.read_text()) if receipt.exists() else {"schema_version": 1,
                "generation_id": generation, "configuration": configuration, "versioning": {}}
            if state.get("schema_version") != 1 or state.get("generation_id") != generation:
                raise DomainError("invalid_backup", "Nightly recovery receipt has a different identity")
            if "result" in state:
                return ProgramResult(data=state["result"])
            if state["configuration"] != configuration:
                raise DomainError("configuration_changed", "Resume this nightly job with its original configuration; new settings cannot expand its effects")

            def save():
                atomic_write(receipt, canonical_bytes(state) + b"\n")

            save()
            control.check()
            metadata = self.backups.create(generation, check=control.check, timeout=min(60, control.remaining_seconds()))
            saved = self.backups.destination / generation
            bundle = build_exports(saved / "state.sqlite3", self.paths)
            if bundle.fingerprint != metadata["exports"]["fingerprint"]:
                raise DomainError("invalid_backup", "Saved operational exports do not match their generation")
            control.check()
            exports = publish_exports(bundle, self.paths)
            versioning = (self.versioner.version(saved, metadata, bundle, state["versioning"], save, control)
                          if self.git_enabled else {"enabled": False})
            control.check()
            removed = self.backups.prune()
            result = {"generation": generation, "checksum": metadata["database"]["checksum"],
                "exports": {key: value for key, value in exports.items() if key != "changed"},
                "versioning": versioning, "retention": {"daily": 7, "weekly": 4, "removed": removed}}
            state["result"] = result
            save()
            return ProgramResult(data=result)
