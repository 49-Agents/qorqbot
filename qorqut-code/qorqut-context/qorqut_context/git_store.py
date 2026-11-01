"""Git is the publication boundary; working files are never read as a snapshot."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from .schema import Conflict, ContextError, identifier

PUBLIC_REF = "refs/heads/main"


class Git:
    def __init__(self, root: Path):
        self.root = root

    def run(self, *args, data: bytes | None = None, env: dict | None = None,
            check: bool = True) -> bytes:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(env or {})
        command = ["git", "-C", str(self.root), "-c", "core.autocrlf=false",
                   "-c", "core.attributesFile=/dev/null", "-c", "core.hooksPath=/dev/null",
                   "-c", "user.name=Qorqut Context", "-c", "user.email=context@qorqut.invalid",
                   *args]
        result = subprocess.run(command, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=environment)
        if check and result.returncode:
            raise ContextError(f"Git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout

    def head(self) -> str:
        return self.run("rev-parse", "--verify", PUBLIC_REF).decode().strip()

    def common_directory(self) -> Path:
        return Path(self.run("rev-parse", "--path-format=absolute", "--git-common-dir")
                    .decode().strip()).resolve()

    def resolve(self, revision: str | None = None) -> str:
        head = self.head()
        revision = revision or head
        if not isinstance(revision, str):
            raise ContextError("revision must be a Git revision string")
        commit = self.run("rev-parse", "--verify", "--end-of-options",
                          revision + "^{commit}").decode().strip()
        # Only published history is a source of evidence or queryable snapshots.
        self.run("merge-base", "--is-ancestor", commit, head)
        return commit

    def files(self, revision: str) -> dict[str, bytes]:
        entries = self.run("ls-tree", "-r", "-z", revision).split(b"\0")
        objects = []
        for entry in entries:
            if not entry:
                continue
            header, path = entry.split(b"\t", 1)
            mode, kind, oid = header.split()
            if kind != b"blob" or mode != b"100644":
                raise ContextError("Context snapshots may contain only regular non-executable files")
            objects.append((path.decode("utf-8"), oid))
        if not objects:
            return {}
        batch = self.run("cat-file", "--batch", data=b"".join(oid + b"\n" for _, oid in objects))
        offset, result = 0, {}
        for path, _ in objects:
            end = batch.index(b"\n", offset)
            _, kind, size = batch[offset:end].split()
            if kind != b"blob":
                raise ContextError("Expected a Git blob")
            start, length = end + 1, int(size)
            result[path] = batch[start:start + length]
            offset = start + length + 1
        return result

    def event_commits(self, revision: str) -> dict[str, str]:
        output = self.run("log", "--reverse", "--format=COMMIT:%H", "--name-only",
                          "--diff-filter=A", revision, "--", "graph/events/").decode()
        current, result = None, {}
        for line in output.splitlines():
            if line.startswith("COMMIT:"):
                current = line[7:]
            elif line.startswith("graph/events/"):
                event_id = Path(line).stem
                if event_id in result:
                    raise ContextError("An immutable event ID was reintroduced")
                result[event_id] = current
        return result

    def require_clean(self):
        if self.run("status", "--porcelain", "--untracked-files=all", "--ignored").strip():
            raise Conflict("The context checkout has external edits; preserve and resolve them first")

    def commit(self, files: dict[str, bytes | None], runtime: Path, base: str | None,
               message: str) -> str:
        descriptor, name = tempfile.mkstemp(prefix="git-index-", dir=runtime)
        os.close(descriptor)
        os.unlink(name)  # Git expects either a valid index or no file.
        env = {"GIT_INDEX_FILE": name}
        try:
            self.run("read-tree", base or "--empty", env=env)
            for path, content in sorted(files.items()):
                if content is None:
                    self.run("update-index", "--force-remove", "--", path, env=env)
                else:
                    oid = self.run("hash-object", "-w", "--stdin", data=content).decode().strip()
                    self.run("update-index", "--add", "--cacheinfo", "100644", oid, path, env=env)
            tree = self.run("write-tree", env=env).decode().strip()
            args = ["commit-tree", tree]
            if base:
                args += ["-p", base]
            return self.run(*args, data=(message + "\n").encode()).decode().strip()
        finally:
            for candidate in (name, name + ".lock"):
                Path(candidate).unlink(missing_ok=True)

    def publish(self, commit: str, base: str | None):
        try:
            self.run("update-ref", PUBLIC_REF, commit, base or "0" * len(commit))
        except ContextError as error:
            raise Conflict("Publication base changed; the draft has been retained") from error

    def checkout(self, base: str | None, commit: str):
        if base:
            self._reconcile_checkout_index(base, commit)
        else:
            base = self.run("mktree", data=b"").decode().strip()
        # Initialization and recovery both refuse overlapping external edits.
        self.run("read-tree", "-m", "-u", base, commit)

    def _working_matches(self, path: str, entry):
        file = self.root / path
        for parent in file.parents:
            if parent == self.root:
                break
            if parent.is_symlink():
                return False
        try:
            mode = file.lstat().st_mode
        except FileNotFoundError:
            return entry is None
        except NotADirectoryError:
            return False
        if (entry is None or entry[0] != b"100644" or
                not stat.S_ISREG(mode) or mode & 0o111):
            return False
        return file.read_bytes() == self.run("cat-file", "blob", entry[1].decode())

    def _reconcile_checkout_index(self, base: str, commit: str):
        """Recognize exact checkout results without replacing owner-staged work."""
        raw = self.run("diff-tree", "--no-commit-id", "--raw", "--no-abbrev",
                       "--no-renames", "-r", "-z", base, commit).split(b"\0")
        changed = {}
        for offset in range(0, len(raw) - 1, 2):
            old_mode, new_mode, old_oid, new_oid, _ = raw[offset].split()
            old_mode = old_mode[1:]  # The raw diff header starts with ':'.
            changed[raw[offset + 1]] = (
                (old_mode, old_oid) if old_mode != b"000000" else None,
                (new_mode, new_oid) if new_mode != b"000000" else None)
        if not changed:
            return
        index = Path(self.run("rev-parse", "--path-format=absolute", "--git-path", "index")
                     .decode().strip())
        lock = Path(str(index) + ".lock")
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError as error:
            raise Conflict("Git index is locked; the existing lock was preserved") from error
        lock_identity = os.fstat(descriptor)
        locked, temporary = True, None
        try:
            with os.fdopen(descriptor, "wb") as handle:
                entries = {}
                for item in self.run("ls-files", "--stage", "-z").split(b"\0"):
                    if not item:
                        continue
                    header, path = item.split(b"\t", 1)
                    if path not in changed:
                        continue
                    mode, oid, stage = header.split()
                    if stage != b"0":
                        raise Conflict("A checkout path has unresolved staged entries")
                    entries[path] = (mode, oid)
                updates = []
                for path, (before, after) in changed.items():
                    staged = entries.get(path)
                    name = path.decode("utf-8")
                    if staged != before and staged != after:
                        raise Conflict(f"A checkout path has unrelated staged changes: {name}")
                    if self._working_matches(name, after):
                        if staged != after:
                            updates.append((name, after))
                    elif staged == after or not self._working_matches(name, before):
                        raise Conflict(f"A checkout path has external changes: {name}")
                if not updates:
                    return
                fd, temporary = tempfile.mkstemp(prefix="checkout-index-", dir=index.parent)
                os.close(fd)
                env = {"GIT_INDEX_FILE": temporary}
                if index.exists():
                    shutil.copyfile(index, temporary)
                else:
                    Path(temporary).unlink()
                    self.run("read-tree", "--empty", env=env)
                for path, entry in updates:
                    if entry is None:
                        self.run("update-index", "--force-remove", "--", path, env=env)
                    else:
                        mode, oid = entry
                        self.run("update-index", "--add", "--cacheinfo",
                                 mode.decode(), oid.decode(), path, env=env)
                handle.write(Path(temporary).read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
                os.replace(lock, index)
                locked = False
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)
            if locked:
                # A replacement after installation belongs to another Git user.
                try:
                    current = lock.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (current.st_dev, current.st_ino) == (lock_identity.st_dev, lock_identity.st_ino):
                        lock.unlink()

    def recorded_at(self, revision: str) -> str:
        seconds = int(self.run("show", "-s", "--format=%ct", revision).strip())
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class Snapshot:
    """An immutable published view, including evidence and source/object history."""

    def __init__(self, git: Git, revision: str):
        self.git, self.revision = git, revision
        self.files = git.files(revision)
        config = self._json("graph/config.json")
        if (config.get("format_version") not in (1, 2) or
                config != {"format_version": config["format_version"], "published_ref": PUBLIC_REF}):
            raise ContextError("Not a supported context repository")
        self.format_version = config["format_version"]
        self.nodes, self.edges = {}, {}
        for path, content in self.files.items():
            for prefix, target in (("graph/nodes/", self.nodes), ("graph/edges/", self.edges)):
                if path.startswith(prefix):
                    record = json.loads(content)
                    if path != prefix + record["id"] + ".json":
                        raise ContextError("Object ID does not match its path")
                    target[record["id"]] = record
        self.catalogs = {kind: self._json(f"graph/catalogs/{kind}s.json")
                         for kind in ("node", "edge")}
        self.event_commits = git.event_commits(revision)
        self.events, self.by_commit = {}, {}
        self.sources, self.support, self.used_ids = {}, {}, set()
        self.actions, self.known_objects = {}, set()
        for event_id, commit in self.event_commits.items():
            path = f"graph/events/{event_id}.json"
            if path not in self.files:
                raise ContextError("A published immutable event is missing")
            event = self._json(path)
            if event["id"] != event_id:
                raise ContextError("Event identity does not match its path")
            if event.get("format_version") != self.format_version:
                raise ContextError("Event/repository format version mismatch")
            original = git.run("cat-file", "blob", f"{commit}:{path}")
            if original != self.files[path]:
                raise ContextError("An immutable event was modified")
            self.events[event_id], self.by_commit[commit] = event, event
            self.used_ids.add(event_id)
            self.used_ids.add(event["source"]["id"])
            self.sources[event["source"]["id"]] = event
            if event.get("input_mode") == "OPERATIONAL_ACTION":
                self.actions[event["action"]["action_ref"]] = event
            for change in event["changes"]:
                self.used_ids.add(change["id"])
                key = (change["kind"], change["id"])
                self.known_objects.add(key)
                self.support[key] = self.resolve_refs(change["support_after"], commit)

    def _json(self, path):
        try:
            return json.loads(self.files[path])
        except (KeyError, ValueError) as error:
            raise ContextError(f"Missing or invalid record: {path}") from error

    @staticmethod
    def resolve_refs(refs, commit):
        return [commit if ref == "self" else ref for ref in refs]

    def objects(self, kind):
        if kind not in ("node", "edge"):
            raise ContextError("Object kind must be node or edge")
        return self.nodes if kind == "node" else self.edges

    def read(self, kind, object_id):
        try:
            record = self.objects(kind)[object_id]
        except KeyError as error:
            raise ContextError(f"No current {kind} with ID {object_id}") from error
        return {"record": json.loads(json.dumps(record)),
                "support": list(self.support.get((kind, object_id), [])),
                "revision": self.revision}

    def source_change(self, commit):
        if commit not in self.by_commit:
            raise ContextError("Citation does not identify a published source event")
        event = self.by_commit[commit]
        path = event["source"]["path"]
        before = self.git.files(event["base_revision"]).get(path)
        after = self.git.files(commit).get(path)
        return {"commit": commit, "input_mode": self.input_mode(event),
                "event": self.event(event["id"]), "before": before, "after": after}

    @staticmethod
    def input_mode(event):
        # Format 1 had no trust classification; never relabel its semantics GENERAL.
        return event.get("input_mode", "LEGACY")

    def event(self, event_id):
        if event_id not in self.events:
            raise ContextError("No published event with this ID")
        event = json.loads(json.dumps(self.events[event_id]))
        commit = self.event_commits[event_id]
        for change in event["changes"]:
            for field in ("citations", "support_after"):
                change[field] = self.resolve_refs(change[field], commit)
            for development in change["developments"]:
                development["citations"] = self.resolve_refs(development["citations"], commit)
        if "action_associations" in event:
            for item in event["action_associations"]["associations"]:
                item["citations"] = self.resolve_refs(item["citations"], commit)
        return {**event, "input_mode": self.input_mode(event),
                "commit": commit, "recorded_at": self.git.recorded_at(commit)}

    def history(self, object_id, *, kind=None):
        result = []
        for event_id, event in self.events.items():
            commit = self.event_commits[event_id]
            if kind == "source" and event["source"]["id"] == object_id:
                result.append({"event_id": event_id, "commit": commit,
                               "input_mode": self.input_mode(event),
                               "recorded_at": self.git.recorded_at(commit),
                               "operation": event["operation"], "source": event["source"]})
                continue
            for change in event["changes"]:
                if change["id"] != object_id or (kind and change["kind"] != kind):
                    continue
                resolved = json.loads(json.dumps(change))
                for field in ("citations", "support_after"):
                    resolved[field] = self.resolve_refs(resolved[field], commit)
                for development in resolved["developments"]:
                    development["citations"] = self.resolve_refs(development["citations"], commit)
                path = f"graph/{change['kind']}s/{object_id}.json"
                before = self.git.files(event["base_revision"]).get(path)
                after = self.git.files(commit).get(path)
                result.append({"event_id": event_id, "commit": commit,
                               "input_mode": self.input_mode(event),
                               "record_before": json.loads(before) if before else None,
                               "record_after": json.loads(after) if after else None,
                               "recorded_at": self.git.recorded_at(commit), **resolved})
        return result

    def neighbors(self, node_id: str, direction: str = "both"):
        if node_id not in self.nodes or direction not in ("in", "out", "both"):
            raise ContextError("Expected an existing node and direction in/out/both")
        edges = [edge for edge in self.edges.values()
                 if ((direction in ("out", "both") and edge["from_id"] == node_id) or
                     (direction in ("in", "both") and edge["to_id"] == node_id))]
        ids = {e[key] for e in edges for key in ("from_id", "to_id")} - {node_id}
        return {"nodes": [self.nodes[key] for key in sorted(ids)],
                "edges": sorted(edges, key=lambda e: e["id"]), "revision": self.revision}

    def supported_by(self, source_id: str):
        """Objects to reassess when a source changes, including support-only links."""
        if source_id not in self.sources:
            raise ContextError("No source with this ID")
        result = []
        for kind, objects in (("node", self.nodes), ("edge", self.edges)):
            for object_id in objects:
                refs = self.support.get((kind, object_id), [])
                if any(self.by_commit[ref]["source"]["id"] == source_id for ref in refs):
                    result.append({"kind": kind, "id": object_id, **self.read(kind, object_id)})
        return sorted(result, key=lambda value: (value["kind"], value["id"]))

    def _note_metadata(self, event):
        commit = self.event_commits[event["id"]]
        return {"event_id": event["id"], "source": event["source"],
                "operation": event["operation"], "input_mode": event["input_mode"],
                "previous_source_event": event["previous_source_event"],
                "commit": commit, "recorded_at": self.git.recorded_at(commit),
                "disposition": event["handoff_note"]["disposition"],
                "text_bytes": len(event["handoff_note"]["text"].encode("utf-8"))}

    def handoff_notes(self, *, include_empty=False, offset=0, limit=20):
        from .input_modes import collection_page
        items = [self._note_metadata(event) for event in self.events.values()
                 if "handoff_note" in event and
                 (include_empty or event["handoff_note"]["disposition"] == "prepared")]
        return {"revision": self.revision, **collection_page(items, offset, limit)}

    def handoff_note(self, event_id, *, offset=0, limit=16000):
        event = self.events.get(event_id)
        if not event or "handoff_note" not in event:
            raise ContextError("No committed handoff note disposition for this event")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 64000:
            raise ContextError("Text offset must be nonnegative and limit between 1 and 64000")
        content = event["handoff_note"]["text"]
        selected = content[offset:offset + limit]
        more = offset + len(selected) < len(content)
        return {"revision": self.revision, **self._note_metadata(event),
                "text": selected, "offset": offset, "total_characters": len(content),
                "has_more": more, "truncated": offset > 0 or more,
                "next_offset": offset + len(selected) if more else None}

    def action_events(self, action_ref):
        return [event for event in self.events.values()
                if event.get("action", {}).get("action_ref") == action_ref]

    def _action_revision(self, event):
        resolved = self.event(event["id"])
        # Read the original accepted text once from its deterministic evidence envelope.
        content = self.git.files(resolved["commit"]).get(event["source"]["path"])
        return ({key: resolved[key] for key in (
            "id", "input_mode", "operation", "source", "action", "action_associations",
            "association_transition", "previous_source_event", "commit", "recorded_at")}
            | {"text": json.loads(content)["text"] if content is not None else None,
               "withdrawn": event["operation"] == "remove"})

    def action(self, action_ref, *, audit=False, offset=0, limit=20):
        from .input_modes import collection_page, opaque_ref
        opaque_ref(action_ref, "action_ref")
        if action_ref not in self.actions:
            raise ContextError("No accepted action with this reference at the selected revision")
        if audit:
            events = self.action_events(action_ref)
            result = collection_page(events, offset, limit)
            result["items"] = [self._action_revision(event) for event in result["items"]]
            return {"revision": self.revision, **result}
        return {"revision": self.revision, **self._action_revision(self.actions[action_ref])}

    def action_timeline(self, kind, object_id, *, audit=False, offset=0, limit=20):
        from .input_modes import collection_page
        self.objects(kind)
        identifier(object_id)
        if (kind, object_id) not in self.known_objects:
            raise ContextError("No object of this kind/ID in published history")
        relevant, events = set(), []
        for event in self.events.values():
            if "action" not in event:
                continue
            ref = event["action"]["action_ref"]
            associated = any(item["kind"] == kind and item["id"] == object_id
                             for item in event["action_associations"]["associations"])
            if associated:
                relevant.add(ref)
            if (audit and ref in relevant) or (not audit and associated and self.actions[ref]["id"] == event["id"]):
                events.append(event)
        result = collection_page(events, offset, limit)
        result["items"] = [self._action_revision(event) for event in result["items"]]
        return {"revision": self.revision, "kind": kind, "id": object_id, "audit": audit, **result}
