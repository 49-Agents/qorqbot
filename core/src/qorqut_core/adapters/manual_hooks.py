"""Explicit, agent-local hook installation. No CLI launch or credential discovery."""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import shlex
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from ..contracts import Model, Revision
from ..runner.shell_models import absolute_path
from .capabilities import require_profile
from .managed_io import ManagedTree, validate_generated_size
from .preparation import EVENTS, PREFIX as ADAPTER_PREFIX, digest, json_bytes
from .types import AdapterError, ManagedRoots, MCPBinding, Provider

PREFIX = ".qorqut/manual-hooks"


class NativeProgram(Model):
    provider: Provider
    cli_version: str = Field(min_length=1, max_length=100)
    executable: Path

    @field_validator("executable")
    @classmethod
    def native_executable(cls, value):
        return absolute_path(value)


class ManualBinding(Model):
    agent_id: UUID
    conversation_id: UUID
    terminal_id: UUID
    recovery_epoch: UUID
    environment_generation: Revision


class HookConfiguration(Model):
    schema_version: Literal[1] = 1
    roots: ManagedRoots
    binding: ManualBinding
    core: MCPBinding = Field(repr=False)
    program: NativeProgram


class ManualHookSetup(Model):
    configuration_paths: tuple[Path, ...]
    binding_paths: tuple[Path, ...]
    limitations: tuple[str, ...] = (
        "native_authentication_and_hook_trust_require_setup",
        "supported_native_startup_requires_combined_acceptance",
        "native_capture_and_input_readiness_are_separate",
    )


def binding_relative(provider: Provider) -> str:
    if provider not in {"claude", "codex"}:
        raise AdapterError("Unsupported hook provider")
    return f"{PREFIX}/{provider}.json"


def read_hook_configuration(path: Path) -> HookConfiguration:
    """Only an explicit private regular file, without symlink traversal."""
    absolute_path(path)
    with ManagedTree(path.parent) as tree, tree.open_read(path.name) as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise AdapterError("Hook binding must be private and owned by this user")
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise AdapterError("Hook binding exceeds its read limit")
    return HookConfiguration.model_validate_json(raw)


def _object(raw):
    try:
        result = json.loads(raw) if raw is not None else {}
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise AdapterError("Invalid hook configuration or ownership record") from None


def _merge(raw, previous, desired):
    value = _object(raw)
    hooks = value.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise AdapterError("Invalid native hooks object")
    for event in set(previous) | set(desired):
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise AdapterError("Invalid native hook entries")
        old, new = previous.get(event), desired.get(event)
        if old is not None:
            # A modified/deleted owned group is a conflict, not an authored hook
            # to silently leave beside a replacement registration.
            if groups.count(old) != 1:
                raise AdapterError("Owned native hook entry was changed")
            groups.remove(old)
        elif new in groups:
            if groups.count(new) != 1:
                raise AdapterError("Ambiguous native hook registration")
            groups.remove(new)  # Identical retry after installation interruption.
        if new is not None:
            groups.append(new)
    return json_bytes(value)


def prepare_manual_hooks(roots: ManagedRoots, binding: ManualBinding, core: MCPBinding,
                         programs: tuple[NativeProgram, ...], hook_sink_argv: tuple[str, ...]) -> ManualHookSetup:
    roots, binding, core = (ManagedRoots.model_validate(roots), ManualBinding.model_validate(binding),
                            MCPBinding.model_validate(core))
    programs = tuple(NativeProgram.model_validate(program) for program in programs)
    if not programs or len({p.provider for p in programs}) != len(programs):
        raise AdapterError("Supply distinct explicit native programs")
    if (not hook_sink_argv or not Path(hook_sink_argv[0]).is_absolute()
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in hook_sink_argv)):
        raise AdapterError("Supply an explicit absolute hook sink argv")
    for program in programs:
        require_profile(program.provider, program.cli_version)
    identity = {"agent_id": str(binding.agent_id), "conversation_id": str(binding.conversation_id),
                "roots": roots.model_dump(mode="json")}
    identity_bytes = json_bytes(identity)
    marker, manifest_path = f"{ADAPTER_PREFIX}/identity.json", f"{PREFIX}/preparation.json"
    configurations, bindings = [], []
    with ExitStack() as stack:
        trees = {name: stack.enter_context(ManagedTree(getattr(roots, name))) for name in ("home", "workspace")}
        identities = {name: tree.identity for name, tree in trees.items()}
        if identities["home"] == identities["workspace"]:
            raise AdapterError("HOME and workspace must be distinct directories")
        for name in sorted(trees, key=identities.__getitem__):
            stack.enter_context(trees[name].lock(f"{ADAPTER_PREFIX}/prepare.lock"))
        for tree in trees.values():
            prior = tree.read(marker)
            if prior is not None and prior != identity_bytes:
                raise AdapterError("Managed roots belong to another binding")
        manifest = _object(trees["workspace"].read(manifest_path))
        if manifest and (manifest.get("identity") != identity or manifest.get("schema_version") != 1):
            raise AdapterError("Manual hook ownership binding changed")
        owned = manifest.get("providers", {})
        if not isinstance(owned, dict):
            raise AdapterError("Invalid manual hook ownership record")
        files = {}
        for program in programs:
            provider = program.provider
            relative = binding_relative(provider)
            path = roots.workspace / relative
            bindings.append(path)
            config = HookConfiguration(roots=roots, binding=binding, core=core, program=program)
            body = json_bytes(config.model_dump(mode="json"))
            previous = owned.get(provider, {})
            if not isinstance(previous, dict) or not isinstance(previous.get("hooks", {}), dict):
                raise AdapterError("Invalid manual hook ownership record")
            before = trees["workspace"].read(relative)
            if before is not None:
                read_hook_configuration(path)  # Existing secrets must remain private.
            if before is not None and before != body and digest(before) != previous.get("binding_sha256"):
                raise AdapterError("Owned manual hook binding was changed")
            files["workspace", relative] = (before, body)
            command = shlex.join((*hook_sink_argv, "--binding", str(path), "--provider", provider))
            events = (*EVENTS, "PostToolUseFailure") if provider == "claude" else (*EVENTS, "Interrupt")
            desired = {event: {"hooks": [{"type": "command", "command": command, "timeout": 3}]}
                       for event in events}
            root, destination = (("workspace", ".claude/settings.local.json") if provider == "claude"
                                 else ("home", ".codex/hooks.json"))
            before = trees[root].read(destination)
            files[root, destination] = (before, _merge(before, previous.get("hooks", {}), desired))
            configurations.append(getattr(roots, root) / destination)
            owned[provider] = {"binding_sha256": digest(body), "hooks": desired}
        manifest_bytes = json_bytes({"schema_version": 1, "identity": identity, "providers": owned})
        for body in (identity_bytes, manifest_bytes, *(body for _, body in files.values())):
            validate_generated_size(body)
        # Preflight all destinations before any hook/binding replacement.
        for (root, path), (before, _) in files.items():
            if trees[root].read(path) != before:
                raise AdapterError("Native hook configuration changed during preparation")
        for tree in trees.values():
            if tree.read(marker) is None:
                tree.atomic_write(marker, identity_bytes)
        for (root, path), (before, body) in files.items():
            if trees[root].read(path) != before:
                raise AdapterError("Native hook configuration changed during preparation")
            if before != body:
                trees[root].atomic_write(path, body)
        for (root, path), (_, body) in files.items():
            if trees[root].read(path) != body:
                raise AdapterError("Native hook configuration changed during preparation")
        trees["workspace"].atomic_write(manifest_path, manifest_bytes)
    return ManualHookSetup(configuration_paths=tuple(configurations), binding_paths=tuple(bindings))
