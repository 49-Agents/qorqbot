"""Exact preparation profiles, distinct from unperformed native acceptance."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import tempfile

from .managed_io import ManagedTree
from .types import AdapterError, Capability, Compatibility, Provider

VERSIONS = {"claude": "2.1.287", "codex": "0.160.0"}


def compatibility(provider: Provider, version: str) -> Compatibility:
    known = VERSIONS[provider] == version
    names = (
        ("preparation", "synthetic", "Generated config and preservation fixtures; native loading unverified"),
        ("launch_resume_argv", "native_help", "Exact installed version/help; argv construction only"),
        ("native_source_discovery", "synthetic", "Bound JSONL source identity fixtures, not native capture acceptance"),
        ("hook_translation", "synthetic", "Bound inputs only; runner persistence required"),
        ("composer_classification", "synthetic", "P06 layout fixtures only; paste remains disabled"),
        ("child_source_coverage", "unsupported", "Pinned source has no verified child association contract"),
        ("native_mcp_discovery", "unsupported", "Deferred isolated native MCP acceptance"),
        ("native_resume", "unsupported", "No native resume interaction observed"),
        ("native_transcript_capture", "unsupported", "No recorder/native integration observed"),
        ("bootstrap_delivery", "unsupported", "M08 restoration and delivery are deferred"),
        ("authentication", "unsupported", "Native authentication/trust require setup evidence"),
    )
    return Compatibility(provider=provider, version=version, known_preparation_contract=known,
        capabilities=tuple(Capability(name=name, evidence=evidence if known else "unsupported",
            detail=detail if known else "Unknown CLI version; no evidence profile")
            for name, evidence, detail in names))


def require_profile(provider: Provider, version: str):
    if version != VERSIONS[provider]:
        raise AdapterError("Unsupported CLI version: " + version)


@dataclass(frozen=True)
class ProbeCommand:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None


def probe(provider: Provider, executable: Path, isolated_root: Path) -> tuple[ProbeCommand, ...]:
    """Version/help only, with a fresh caller-owned root and no inherited auth env.

    No sessions, network probes, credential reads, or model invocation. The caller
    creates and disposes the root. Output does not promote native capabilities.
    """
    if provider not in VERSIONS or not executable.is_absolute():
        raise AdapterError("Supply a concrete provider and absolute executable")
    with ManagedTree(isolated_root) as tree:
        if os.listdir(tree.fd):
            raise AdapterError("Probe root must be empty; personal homes are not probe fixtures")
        for name in ("home", "codex", "claude", "config", "cache", "data"):
            os.mkdir(name, 0o700, dir_fd=tree.fd)
    env = {"PATH": str(executable.parent) + os.pathsep + os.defpath,
        "HOME": str(isolated_root / "home"), "CODEX_HOME": str(isolated_root / "codex"),
        "CLAUDE_CONFIG_DIR": str(isolated_root / "claude"),
        "XDG_CONFIG_HOME": str(isolated_root / "config"), "XDG_CACHE_HOME": str(isolated_root / "cache"),
        "XDG_DATA_HOME": str(isolated_root / "data"), "NO_COLOR": "1", "TERM": "dumb"}
    result = []
    arguments = [("--version",), ("--help",)]
    if provider == "codex":
        arguments.append(("resume", "--help"))
    for args in arguments:
        argv = (str(executable), *args)
        try:
            # Some native launchers exit before flushing piped help output. Regular
            # temporary files avoid certifying a silently truncated help surface.
            with tempfile.TemporaryFile(dir=isolated_root) as stdout, tempfile.TemporaryFile(dir=isolated_root) as stderr:
                output = subprocess.run(argv, cwd=isolated_root, env=env, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, timeout=15, check=False)
                stdout.seek(0)
                stderr.seek(0)
                result.append(ProbeCommand(argv, output.returncode,
                    stdout.read().decode("utf-8", "replace"), stderr.read().decode("utf-8", "replace")))
        except (OSError, subprocess.TimeoutExpired) as exc:
            result.append(ProbeCommand(argv, None, "", "", type(exc).__name__))
    return tuple(result)
