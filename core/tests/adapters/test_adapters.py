"""R1 contract checks use synthetic data and disposable roots, never native accounts."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from qorqut_core.adapters import (AdapterError, ClaudeAdapter, CodexAdapter,
    MCPBinding, ManagedRoots, NativeBinding, PreparationInput, RunBinding)
from qorqut_core.adapters.managed_io import GENERATED_FILE_LIMIT, ManagedTree

FIXTURES = Path(__file__).parent / "fixtures"
SESSION = UUID("11111111-1111-4111-8111-111111111111")
OTHER_SESSION = UUID("22222222-2222-4222-8222-222222222222")


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="adapter-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        home, workspace = self.root / "managed home", self.root / "workspace with spaces"
        home.mkdir()
        workspace.mkdir()
        self.roots = ManagedRoots(home=home, workspace=workspace)
        self.run = RunBinding(agent_id=uuid4(), conversation_id=uuid4(), recovery_epoch=uuid4(),
            environment_generation=1, boot_id=uuid4(), run_id=uuid4(), run_generation=1)
        self.data = PreparationInput(roots=self.roots, run=self.run,
            core=MCPBinding(url="http://127.0.0.1:39111/mcp", headers=(("X-Actor", str(self.run.agent_id)), ("Authorization", "Bearer synthetic-core"))),
            browser=MCPBinding(url="http://127.0.0.1:39112/mcp", headers=(("X-Target", str(self.run.agent_id)), ("Authorization", "Bearer synthetic-browser"))))
        self.claude, self.codex = ClaudeAdapter("2.1.287"), CodexAdapter("0.160.0")
        self.native = NativeBinding(run=self.run, native_session_id=SESSION)

    def put(self, relative, content):
        path = self.roots.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def source(self, adapter, *, suffix="", content=None):
        if adapter.provider == "claude":
            relative = f".claude/projects/unrelated-to-current-cwd/{SESSION}.jsonl"
        else:
            relative = f".codex/sessions/2026/01/01/rollout-2026-01-01T12-30-00-{SESSION}{suffix}.jsonl"
        return self.put(relative, content if content is not None else (FIXTURES / f"{adapter.provider}-owned.jsonl").read_bytes())


class PreparationTests(AdapterCase):
    def test_long_history_manifest_compacts_without_changing_retained_bindings(self):
        for adapter in (self.claude, self.codex):
            with self.subTest(provider=adapter.provider):
                first = adapter.prepare(self.data)
                second_data = self.data.model_copy(update={"run": self.run.model_copy(
                    update={"run_id": uuid4(), "run_generation": 2})})
                second = adapter.prepare(second_data)
                retained = {result.binding_path: result.binding_path.read_bytes() for result in (first, second)}
                retained_stats = {path: path.stat() for path in retained}
                manifest_path = self.roots.workspace / f".qorqut/adapters/{adapter.provider}/preparation.json"
                legacy = json.loads(manifest_path.read_bytes())
                prefix = f"workspace:.qorqut/adapters/{adapter.provider}/runs/"
                first_key = prefix + str(self.run.run_id) + ".json"
                legacy["files"][first_key] = hashlib.sha256(retained[first.binding_path]).hexdigest()

                def encode(value):
                    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()

                # Model a near-limit prior accumulated manifest without creating
                # thousands of sessions. Only the two retained bindings are real files.
                initial_size = len(encode(legacy))
                legacy["files"][prefix + str(UUID(int=1)) + ".json"] = "0" * 64
                entry_size = len(encode(legacy)) - initial_size
                count = (GENERATED_FILE_LIMIT - initial_size) // entry_size
                for number in range(2, count + 1):
                    legacy["files"][prefix + str(UUID(int=number)) + ".json"] = "0" * 64
                accumulated = encode(legacy)
                self.assertLessEqual(len(accumulated), GENERATED_FILE_LIMIT)
                self.assertGreater(len(accumulated), GENERATED_FILE_LIMIT - entry_size)
                manifest_path.write_bytes(accumulated)
                current_data = self.data.model_copy(update={"run": self.run.model_copy(
                    update={"run_id": uuid4(), "run_generation": 3})})
                current = adapter.prepare(current_data)
                retained[current.binding_path] = current.binding_path.read_bytes()
                retained_stats[current.binding_path] = current.binding_path.stat()
                compacted = manifest_path.read_bytes()
                self.assertLess(len(compacted), 16 * 1024)
                current_manifest = json.loads(compacted)
                self.assertEqual([key for key in current_manifest["files"] if key.startswith(prefix)],
                    [prefix + str(current_data.run.run_id) + ".json"])
                adapter.prepare(current_data)
                self.assertEqual(manifest_path.read_bytes(), compacted)

                # Old manifest membership is not authority to alter a binding.
                self.assertNotIn(first_key, current_manifest["files"])
                wrong_prior = self.data.model_copy(update={"run": self.run.model_copy(
                    update={"environment_generation": 2})})
                with self.assertRaisesRegex(AdapterError, "binding is immutable"):
                    adapter.prepare(wrong_prior)
                self.assertEqual(manifest_path.read_bytes(), compacted)
                adapter.prepare(self.data)  # Exact retained prior run is still preparable.
                prior_manifest = manifest_path.read_bytes()
                self.assertLess(len(prior_manifest), 16 * 1024)
                adapter.prepare(self.data)
                self.assertEqual(manifest_path.read_bytes(), prior_manifest)
                for path, original in retained.items():
                    self.assertEqual(path.read_bytes(), original)
                    before, after = retained_stats[path], path.stat()
                    self.assertEqual((after.st_dev, after.st_ino, after.st_mtime_ns),
                        (before.st_dev, before.st_ino, before.st_mtime_ns))

    def test_aliased_roots_rejected_in_bounded_child(self):
        child_code = """
import sys
from qorqut_core.adapters import AdapterError, ClaudeAdapter, CodexAdapter, PreparationInput
data = PreparationInput.model_validate_json(sys.stdin.read())
adapter = ClaudeAdapter('2.1.287') if sys.argv[1] == 'claude' else CodexAdapter('0.160.0')
try:
    adapter.prepare(data)
except AdapterError as exc:
    assert 'same filesystem directory' in str(exc), str(exc)
    print('rejected before locks')
else:
    raise AssertionError('Equivalent roots were accepted')
"""
        with tempfile.TemporaryDirectory(prefix="adapter-root-alias-", dir="/tmp") as temporary:
            actual = Path(temporary) / "managed"
            actual.mkdir()
            alias = Path("/" + str(actual))
            self.assertNotEqual(actual, alias)
            self.assertEqual(actual.stat().st_ino, alias.stat().st_ino)
            data = self.data.model_copy(update={"roots": ManagedRoots(home=actual, workspace=alias)})
            for adapter in (self.claude, self.codex):
                with self.subTest(provider=adapter.provider):
                    # subprocess.run kills/reaps only this owned child on timeout,
                    # making the old self-deadlock a bounded, clean test failure.
                    result = subprocess.run([sys.executable, "-c", child_code, adapter.provider],
                        input=data.model_dump_json(), text=True, capture_output=True, timeout=5,
                        cwd=temporary, env={"HOME": str(actual), "PATH": os.defpath,
                            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "rejected before locks")
                    self.assertEqual(list(actual.iterdir()), [])

    def test_generated_output_size_preflight_preserves_readable_projection(self):
        for adapter in (self.claude, self.codex):
            with self.subTest(provider=adapter.provider):
                prepared = adapter.prepare(self.data)
                originals = {path: path.read_bytes() for path in prepared.generated_paths}
                next_run = self.run.model_copy(update={"run_id": uuid4(), "run_generation": 2})
                excessive = (
                    {"core": MCPBinding(url=self.data.core.url, headers=(("X-Large", "x" * GENERATED_FILE_LIMIT),))},
                    {"hook_sink_argv": ("/synthetic/hook", "x" * (GENERATED_FILE_LIMIT // 2))},
                )
                for changed in excessive:
                    with self.assertRaisesRegex(AdapterError, "read limit"):
                        adapter.prepare(self.data.model_copy(update={"run": next_run, **changed}))
                    for path, original in originals.items():
                        self.assertEqual(path.read_bytes(), original)
                    self.assertFalse(prepared.binding_path.with_name(str(next_run.run_id) + ".json").exists())
                adapter.prepare(self.data)
        with ManagedTree(self.roots.home) as tree:
            boundary = b"x" * GENERATED_FILE_LIMIT
            tree.atomic_write("synthetic-size-boundary", boundary)
            self.assertEqual(tree.read("synthetic-size-boundary"), boundary)
            with self.assertRaisesRegex(AdapterError, "read limit"):
                tree.atomic_write("synthetic-size-boundary", boundary + b"x")
            self.assertEqual(tree.read("synthetic-size-boundary"), boundary)

    def test_roundtrip_preserves_authored_bytes_identity_and_credentials(self):
        authored = {"persona.md": b"\xff authored persona\r\n", "memory.md": b"My notes\x00\n",
            "notes/a.md": b"unchanged notes", "research/report": b"research"}
        for name, content in authored.items():
            path = self.roots.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        credentials = self.put(".codex/auth.json", b"synthetic credential sentinel; never read")
        for adapter in (self.claude, self.codex, self.claude):
            result = adapter.prepare(self.data)
            self.assertEqual(result.run, self.run)
            self.assertEqual(result.authentication, "required_setup")
            for name, content in authored.items():
                self.assertEqual((self.roots.workspace / name).read_bytes(), content)
            self.assertEqual(credentials.read_bytes(), b"synthetic credential sentinel; never read")
        for name in ("CLAUDE.md", "AGENTS.md"):
            text = (self.roots.workspace / name).read_text()
            for fragment in (str(self.run.agent_id), "persona.md", "memory.md", "browser_request_input", "files_reference", "with a path", "saved reference"):
                self.assertIn(fragment, text)
            for word in ("iframe", "modal", "toast"):
                self.assertNotIn(word, text.lower())
        self.assertEqual((self.roots.workspace / "CLAUDE.md").read_bytes(), (self.roots.workspace / "AGENTS.md").read_bytes())

    def test_separate_mcp_endpoints_and_explicit_hook_sink_binding(self):
        sink = (str(self.roots.home / "bin/hook sink"), "--persist", "argument with spaces;literal")
        data = self.data.model_copy(update={"hook_sink_argv": sink})
        cl = self.claude.prepare(data)
        co = self.codex.prepare(data)
        claude_config = json.loads((self.roots.workspace / ".qorqut/adapters/claude/mcp.json").read_text())["mcpServers"]
        codex_config = tomllib.loads((self.roots.home / ".codex/config.toml").read_text())["mcp_servers"]
        for config in (claude_config, codex_config):
            self.assertEqual(set(config), {"qorqut-core", "qorqut-browser"})
            for name, expected in (("qorqut-core", self.data.core), ("qorqut-browser", self.data.browser)):
                self.assertEqual(config[name]["url"], expected.url)
                self.assertEqual(config[name].get("headers", config[name].get("http_headers")), dict(expected.headers))
        for result, path in ((cl, self.roots.workspace / ".qorqut/adapters/claude/settings.json"),
                             (co, self.roots.home / ".codex/hooks.json")):
            hooks = json.loads(path.read_text())["hooks"]
            command = hooks["Stop"][0]["hooks"][0]["command"]
            self.assertEqual(shlex.split(command), ["env", "QORQUT_ADAPTER_BINDING_FILE=" + str(result.binding_path), *sink])
            self.assertEqual(json.loads(result.binding_path.read_text())["run"]["run_id"], str(self.run.run_id))
            self.assertNotIn("durable_hook_sink_not_configured", result.gaps)
        self.assertFalse(codex_config["qorqut-browser"]["required"])

    def test_generated_toml_preserves_unicode_endpoint_and_literal_header_values(self):
        core = MCPBinding(url="http://127.0.0.1:39111/\U0001f4c1/mcp",
            headers=(("X-Binding", 'literal "quoted" \\ value'),))
        self.codex.prepare(self.data.model_copy(update={"core": core}))
        config = tomllib.loads((self.roots.home / ".codex/config.toml").read_text())
        self.assertEqual(config["mcp_servers"]["qorqut-core"]["url"], core.url)
        self.assertEqual(config["mcp_servers"]["qorqut-core"]["http_headers"], dict(core.headers))

    def test_authored_config_and_edited_generated_bytes_are_not_overwritten(self):
        target = self.roots.workspace / "CLAUDE.md"
        target.write_bytes(b"authored CLAUDE.md")
        with self.assertRaises(AdapterError):
            self.claude.prepare(self.data)
        self.assertEqual(target.read_bytes(), b"authored CLAUDE.md")
        target.unlink()
        self.claude.prepare(self.data)
        target.write_bytes(b"an owner edit")
        with self.assertRaises(AdapterError):
            self.claude.prepare(self.data)
        self.assertEqual(target.read_bytes(), b"an owner edit")

    def test_wrong_identity_roots_and_mutated_run_fail_closed(self):
        self.claude.prepare(self.data)
        for changes in ({"agent_id": uuid4()}, {"conversation_id": uuid4()}, {"run_generation": 2}):
            with self.subTest(changes=changes), self.assertRaises(AdapterError):
                self.claude.prepare(self.data.model_copy(update={"run": self.run.model_copy(update=changes)}))
        other = self.root / "other workspace"
        other.mkdir()
        roots = self.roots.model_copy(update={"workspace": other})
        with self.assertRaises(AdapterError):
            self.codex.prepare(self.data.model_copy(update={"roots": roots}))

    def test_symlink_root_ancestor_and_destination_cannot_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"not managed")
        (self.roots.workspace / "CLAUDE.md").symlink_to(sentinel)
        with self.assertRaises(OSError):
            self.claude.prepare(self.data)
        self.assertEqual(sentinel.read_bytes(), b"not managed")
        alias = self.root / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        (outside / "child").mkdir()
        with self.assertRaises(OSError):
            with ManagedTree(alias / "child"):
                self.fail("A symlink ancestor was followed")

    def test_atomic_failure_leaves_whole_files_and_identical_retry_recovers(self):
        self.claude.prepare(self.data)
        updated = self.data.model_copy(update={"run": self.run.model_copy(update={"run_id": uuid4(), "run_generation": 2}),
            "core": self.data.core.model_copy(update={"url": "http://127.0.0.1:39113/mcp"})})
        replace = os.replace
        replacements = 0

        def fail_once(*args, **kwargs):
            nonlocal replacements
            replacements += 1
            if replacements == 3:
                raise OSError("synthetic install interruption")
            return replace(*args, **kwargs)

        with patch("qorqut_core.adapters.managed_io.os.replace", side_effect=fail_once), self.assertRaises(OSError):
            self.claude.prepare(updated)
        for file in self.roots.workspace.rglob("*.json"):
            json.loads(file.read_text())  # Every installed file is complete, including the prior manifest.
        result = self.claude.prepare(updated)
        self.assertTrue(result.binding_path.exists())
        config = json.loads((self.roots.workspace / ".qorqut/adapters/claude/mcp.json").read_text())
        self.assertEqual(config["mcpServers"]["qorqut-core"]["url"], updated.core.url)
        self.assertEqual(list(self.roots.workspace.rglob(".adapter-*")), [])
        self.assertEqual((result.binding_path.stat().st_mode & 0o777), 0o600)


class LaunchTests(AdapterCase):
    def test_native_resume_has_exact_identity_no_picker_and_no_prompt(self):
        native = self.native.model_copy(update={"resumed_from_run_id": uuid4()})
        for adapter in (self.claude, self.codex):
            prepared = adapter.prepare(self.data)
            launch = adapter.launch(Path("/synthetic/bin") / adapter.provider, prepared, native=native,
                model="provider/model with spaces;literal")
            self.assertEqual(launch.native_session_id, SESSION)
            self.assertEqual(launch.resumed_from_run_id, native.resumed_from_run_id)
            self.assertFalse(launch.executed)
            env = dict(launch.environment)
            self.assertEqual(env["HOME"], str(self.roots.home))
            self.assertEqual(env["CODEX_HOME"], str(self.roots.home / ".codex"))
            self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.roots.home / ".claude"))
            self.assertNotIn("--last", launch.argv)
            self.assertNotIn("--continue", launch.argv)
            if adapter.provider == "claude":
                self.assertEqual(launch.argv[launch.argv.index("--resume") + 1], str(SESSION))
                self.assertEqual(len(launch.argv), 12)
            else:
                self.assertEqual(launch.argv[1:3], ("resume", str(SESSION)))
                self.assertEqual(len(launch.argv), 12)
            self.assertEqual(launch.argv[-2:], ("--model", "provider/model with spaces;literal"))

    def test_new_launch_cannot_silently_rebind_another_native_session(self):
        cl, co = self.claude.prepare(self.data), self.codex.prepare(self.data)
        self.assertIn("--session-id", self.claude.launch(Path("/claude"), cl, native=self.native).argv)
        self.assertIsNone(self.codex.launch(Path("/codex"), co).native_session_id)
        with self.assertRaises(AdapterError):
            self.codex.launch(Path("/codex"), co, native=self.native)
        with self.assertRaises(AdapterError):
            self.claude.launch(Path("/claude"), cl)
        with self.assertRaises(AdapterError):
            self.claude.launch(Path("/claude"), cl, native=self.native.model_copy(update={"run": self.run.model_copy(update={"run_id": uuid4()})}))


class DiscoveryTests(AdapterCase):
    def test_claude_primary_source_retains_embedded_child_records(self):
        def encoded(record):
            return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()

        primary = encoded({"type": "user", "sessionId": str(SESSION), "uuid": "synthetic-primary-1",
            "message": {"content": "Primary record with Unicode: ü"}})
        following = encoded({"type": "assistant", "sessionId": str(SESSION), "uuid": "synthetic-primary-2",
            "message": {"content": "Another independent primary record"}})
        for marker in ({"isSidechain": True}, {"agentId": "synthetic-child"}):
            for child_session in (SESSION, OTHER_SESSION):
                child = encoded({"type": "attachment", "sessionId": str(child_session), **marker,
                    "attachment": {"type": "unverified_child_file", "path": "/not-owned/child-ü.bin"}})
                cases = (("before", child + primary + following, len(child)),
                    ("between", primary + child + following, 0),
                    ("after", primary + following + child, 0))
                for position, raw, primary_offset in cases:
                    with self.subTest(marker=marker, child_session=child_session, position=position):
                        path = self.source(self.claude, content=raw)
                        before = path.stat()
                        result = self.claude.discover(self.roots, self.native)
                        self.assertEqual(len(result.sources), 1)
                        source = result.sources[0]
                        self.assertEqual((source.path, source.device, source.inode, source.binding),
                            (path, before.st_dev, before.st_ino, self.native))
                        self.assertEqual(source.observed_size, len(raw))
                        self.assertEqual(len(source.ownership), 1)
                        proof = source.ownership[0]
                        self.assertEqual((proof.kind, proof.offset, proof.length, proof.sha256),
                            ("native_session_id", primary_offset, len(primary), hashlib.sha256(primary).hexdigest()))
                        self.assertEqual(raw[proof.offset:proof.offset + proof.length], primary)
                        self.assertEqual(path.read_bytes(), raw)
                        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
                        self.assertEqual(result.attachments, ())
                        codes = {gap.code for gap in result.gaps}
                        self.assertIn("unsupported_child_association", codes)
                        self.assertNotIn("native_identity_conflict", codes)
                        self.assertNotIn("native_identity_unverified", codes)
                        self.assertNotIn("unsupported_attachment_reference", codes)

        # A conflicting primary after an embedded child still invalidates ownership.
        conflicting = encoded({"type": "user", "sessionId": str(OTHER_SESSION)})
        self.source(self.claude, content=primary + child + conflicting + following)
        result = self.claude.discover(self.roots, self.native)
        self.assertEqual(result.sources, ())
        self.assertIn("native_identity_conflict", {gap.code for gap in result.gaps})

    def test_cwd_changes_unknown_types_and_repeated_text_keep_one_raw_source(self):
        for adapter in (self.claude, self.codex):
            with self.subTest(provider=adapter.provider):
                path = self.source(adapter)
                original = path.read_bytes()
                wrong_path = path.with_name(path.name.replace(str(SESSION), str(OTHER_SESSION)))
                wrong_path.write_bytes(original.replace(str(SESSION).encode(), str(OTHER_SESSION).encode()))
                os.utime(wrong_path, (2000000000, 2000000000))
                result = adapter.discover(self.roots, self.native)
                self.assertEqual(len(result.sources), 1)
                source = result.sources[0]
                self.assertEqual(source.path, path)
                self.assertEqual(source.binding, self.native)
                self.assertFalse(source.archived)
                self.assertEqual(path.read_bytes(), original)
                self.assertNotIn("same text", source.model_dump_json())
                proof = source.ownership[0]
                self.assertEqual(hashlib.sha256(original[proof.offset:proof.offset + proof.length]).hexdigest(), proof.sha256)

    def test_codex_suffix_resume_link_and_multiple_sources_are_retained(self):
        first = self.source(self.codex)
        second = self.source(self.codex, suffix="_33333333-3333-4333-8333-333333333333")
        resumed = NativeBinding(run=self.run.model_copy(update={"run_id": uuid4(), "run_generation": 2}),
            native_session_id=SESSION, resumed_from_run_id=self.run.run_id)
        result = self.codex.discover(self.roots, resumed)
        self.assertEqual({source.path for source in result.sources}, {first, second})
        self.assertTrue(all(source.binding == resumed for source in result.sources))
        self.source(self.codex, suffix="_unsupported-suffix")
        result = self.codex.discover(self.roots, resumed)
        self.assertIn("unsupported_source_filename", {gap.code for gap in result.gaps})
        self.assertEqual(len(result.sources), 2)

    def test_child_ownership_is_an_explicit_gap_not_inferred_from_parent_path(self):
        for adapter in (self.claude, self.codex):
            path = self.source(adapter, content=(FIXTURES / f"{adapter.provider}-child-unsupported.jsonl").read_bytes())
            result = adapter.discover(self.roots, self.native)
            self.assertEqual(result.sources, ())
            self.assertIn("unsupported_child_association", {gap.code for gap in result.gaps})
            if adapter.provider == "claude":
                self.assertIn("native_identity_unverified", {gap.code for gap in result.gaps})
                self.assertNotIn("native_identity_conflict", {gap.code for gap in result.gaps})
            path.unlink()
        self.put(f".claude/projects/p/{SESSION}/subagents/agent-synthetic-child.jsonl", (FIXTURES / "claude-child-unsupported.jsonl").read_bytes())
        self.assertIn("unsupported_child_association", {gap.code for gap in self.claude.discover(self.roots, self.native).gaps})

    def test_conflicting_identity_missing_metadata_and_symlink_are_not_owned(self):
        for adapter in (self.claude, self.codex):
            original = (FIXTURES / f"{adapter.provider}-owned.jsonl").read_bytes()
            path = self.source(adapter, content=original.replace(str(SESSION).encode(), str(OTHER_SESSION).encode()))
            result = adapter.discover(self.roots, self.native)
            self.assertEqual(result.sources, ())
            self.assertIn("native_identity_conflict", {g.code for g in result.gaps})
            path.write_bytes(b'{"type":"future-format","cwd":"/synthetic/start"}\n')
            self.assertEqual(adapter.discover(self.roots, self.native).sources, ())
            outside = self.root / (adapter.provider + "-outside.jsonl")
            outside.write_bytes(original)
            path.unlink()
            path.symlink_to(outside)
            result = adapter.discover(self.roots, self.native)
            self.assertEqual(result.sources, ())
            self.assertTrue(any(g.code.startswith("managed_source_unavailable") for g in result.gaps))

    def test_attachments_return_coordinates_and_unavailable_external_reference(self):
        path = self.source(self.codex)
        result = self.codex.discover(self.roots, self.native)
        self.assertEqual([r.kind for r in result.attachments], ["embedded", "external"])
        self.assertIsNone(result.attachments[0].location)
        self.assertEqual(result.attachments[1].location, "file:///not-an-owned-fixture/do-not-open.png")
        self.assertIn("external_attachment_not_captured", {g.code for g in result.gaps})
        for reference in result.attachments:
            self.assertEqual(reference.source_path, path)
            self.assertFalse(reference.captured)
            evidence = reference.record
            self.assertEqual(hashlib.sha256(path.read_bytes()[evidence.offset:evidence.offset + evidence.length]).hexdigest(), evidence.sha256)

    def test_large_record_and_partial_tail_do_not_drop_bound_raw_source(self):
        path = self.source(self.codex)
        with path.open("ab") as stream:
            stream.write(b'{"type":"future-large","payload":"' + b"x" * (9 * 1024 * 1024) + b'"}\n{"partial":')
        result = self.codex.discover(self.roots, self.native)
        self.assertEqual(result.sources[0].observed_size, path.stat().st_size)
        self.assertIn("metadata_record_too_large", {g.code for g in result.gaps})
        self.assertIn("incomplete_metadata_record", {g.code for g in result.gaps})


class HookAndComposerTests(AdapterCase):
    def test_hooks_require_bound_source_and_return_unpersisted_observation(self):
        for adapter in (self.claude, self.codex):
            path = self.source(adapter)
            source = adapter.discover(self.roots, self.native).sources[0]
            payload = {"hook_event_name": "PermissionRequest", "session_id": str(SESSION),
                "transcript_path": str(path), "cwd": "/synthetic/changed"}
            observation = adapter.translate_hook(self.native, source, payload, sequence=3, observed_at=datetime.now(timezone.utc))
            self.assertEqual(observation.activity, "waiting_user")
            self.assertFalse(observation.durable)
            self.assertTrue(observation.requires_persistence)
            ended = adapter.translate_hook(self.native, source, {**payload, "hook_event_name": "SessionEnd"},
                sequence=4, observed_at=datetime.now(timezone.utc))
            self.assertEqual(ended.activity, "unknown")
            for changes in ({"session_id": str(OTHER_SESSION)}, {"run_id": str(self.run.run_id)},
                            {"parent_session_id": str(OTHER_SESSION)}, {"hook_event_name": "UnknownEvent"},
                            {"transcript_path": "/personal/history.jsonl"}):
                with self.subTest(changes=changes), self.assertRaises(AdapterError):
                    adapter.translate_hook(self.native, source, {**payload, **changes}, sequence=3, observed_at=datetime.now(timezone.utc))
            with self.assertRaises(AdapterError):
                adapter.translate_hook(self.native.model_copy(update={"run": self.run.model_copy(update={"run_id": uuid4()})}), source,
                    payload, sequence=3, observed_at=datetime.now(timezone.utc))

    def test_composer_known_synthetic_states_never_certify_native_paste(self):
        fixtures = json.loads((FIXTURES / "composers.json").read_text())
        for adapter in (self.claude, self.codex):
            fixture = fixtures[adapter.provider]
            screen, cursor = fixture["empty"], tuple(fixture["cursor"])
            empty = adapter.composer(screen, cursor)
            self.assertEqual(empty.state, "empty")
            self.assertFalse(empty.permits_paste)
            self.assertFalse(empty.native_verified)
            self.assertEqual(adapter.composer(screen.replace("? for shortcuts", "esc to interrupt"), cursor).state, "busy")
            self.assertEqual(adapter.composer(screen.replace("? for shortcuts", "Enter to confirm · Esc to cancel"), cursor).state, "blocked")
            self.assertEqual(adapter.composer(screen.replace("\x1b[2mTry a task\x1b[0m", "half a draft").replace("\x1b[2mAsk Codex\x1b[0m", "half a draft"), cursor).state, "blocked")
            self.assertEqual(adapter.composer(screen).state, "unknown")
            self.assertEqual(adapter.composer("shell$ ", cursor).state, "unknown")
            self.assertEqual(adapter.composer(screen, (20, cursor[1], True)).state, "unknown")
            self.assertNotEqual(empty.fingerprint, adapter.composer(screen, (3, cursor[1], True)).fingerprint)
            unknown = type(adapter)("99.0.0").composer(screen, cursor)
            self.assertEqual(unknown.state, "unknown")
            self.assertFalse(unknown.permits_paste)

    def test_unknown_versions_are_unsupported_for_every_dependent_operation(self):
        for adapter in (ClaudeAdapter("2.1.288"), CodexAdapter("0.161.0")):
            profile = adapter.compatibility()
            self.assertFalse(profile.known_preparation_contract)
            self.assertFalse(profile.native_verified)
            self.assertTrue(all(c.evidence == "unsupported" for c in profile.capabilities))
            with self.assertRaises(AdapterError):
                adapter.prepare(self.data)
            with self.assertRaises(AdapterError):
                adapter.discover(self.roots, self.native)
        for adapter in (self.claude, self.codex):
            self.assertFalse(any(cap.native_verified for cap in adapter.compatibility().capabilities))


class ProbeTests(AdapterCase):
    def test_probe_uses_only_help_commands_and_isolated_environment(self):
        executable = self.root / "synthetic-cli"
        executable.write_text("#!/usr/bin/python3\n"
            "import json, os, sys\n"
            "assert sys.argv[1:] in [['--version'], ['--help'], ['resume', '--help']]\n"
            "print(json.dumps({'argv':sys.argv[1:],'home':os.environ['HOME'],"
            "'codex':os.environ['CODEX_HOME'],'claude':os.environ['CLAUDE_CONFIG_DIR'],"
            "'secret':os.environ.get('ADAPTER_TEST_SECRET')}))\n"
            "print('x' * 24000)\n")
        executable.chmod(0o700)
        for adapter in (self.claude, self.codex):
            root = self.root / (adapter.provider + "-probe")
            root.mkdir()
            with patch.dict(os.environ, {"ADAPTER_TEST_SECRET": "do not inherit", "HOME": "/synthetic-personal-home"}):
                results = adapter.probe(executable, root)
            self.assertEqual(len(results), 2 if adapter.provider == "claude" else 3)
            for result in results:
                payload, content = result.stdout.splitlines()
                payload = json.loads(payload)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(payload["home"], str(root / "home"))
                self.assertEqual(payload["codex"], str(root / "codex"))
                self.assertEqual(payload["claude"], str(root / "claude"))
                self.assertIsNone(payload["secret"])
                self.assertEqual(len(content), 24000)
            with self.assertRaises(AdapterError):
                adapter.probe(executable, root)  # Existing native stores are never probes.


if __name__ == "__main__":
    unittest.main()
