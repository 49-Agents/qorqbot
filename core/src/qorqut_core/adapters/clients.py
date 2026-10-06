"""Two concrete preparation adapters; no launcher, model catalog or plugin API."""

from pathlib import Path

from .capabilities import compatibility, probe, require_profile
from .composer import classify
from .discovery import discover
from .hooks import translate_hook
from .preparation import PREFIX, prepare
from .types import AdapterError, LaunchSpec, NativeBinding, PreparationResult


def _launch(provider, version, executable: Path, prepared: PreparationResult,
            native: NativeBinding | None, model: str | None):
    require_profile(provider, version)
    if not executable.is_absolute() or prepared.provider != provider:
        raise AdapterError("Executable and preparation must belong to the concrete adapter")
    if prepared.binding_path != prepared.roots.workspace / f"{PREFIX}/{provider}/runs/{prepared.run.run_id}.json":
        raise AdapterError("Preparation binding path mismatch")
    if native is not None and native.run != prepared.run:
        raise AdapterError("Native identity belongs to another run")
    if model is not None and (not model.strip() or model.startswith("-") or "\0" in model):
        raise AdapterError("Invalid opaque native model label")
    resume = native is not None and native.resumed_from_run_id is not None
    if provider == "claude":
        if native is None:
            raise AdapterError("Claude launch requires a reserved exact native session UUID")
        directory = prepared.roots.workspace / PREFIX / "claude"
        argv = [str(executable), "--settings", str(directory / "settings.json"),
            "--mcp-config", str(directory / "mcp.json"), "--strict-mcp-config",
            "--permission-mode", "bypassPermissions",
            "--resume" if resume else "--session-id", str(native.native_session_id)]
    else:
        if native is not None and not resume:
            raise AdapterError("Codex chooses a fresh native UUID; it cannot be supplied at launch")
        argv = [str(executable)]
        if resume:
            argv.extend(("resume", str(native.native_session_id)))
        argv.extend(("--no-daemon", "--cd", str(prepared.roots.workspace),
            "--sandbox", "danger-full-access", "--ask-for-approval", "never"))
    if model is not None:
        argv.extend(("--model", model))
    env = {"HOME": str(prepared.roots.home), "CODEX_HOME": str(prepared.roots.home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(prepared.roots.home / ".claude"),
        "XDG_CONFIG_HOME": str(prepared.roots.home / ".config"),
        "XDG_CACHE_HOME": str(prepared.roots.home / ".cache"),
        "XDG_DATA_HOME": str(prepared.roots.home / ".local/share"),
        "QORQUT_ADAPTER_BINDING_FILE": str(prepared.binding_path)}
    return LaunchSpec(provider=provider, run=prepared.run, argv=tuple(argv),
        cwd=prepared.roots.workspace, environment=tuple(env.items()),
        native_session_id=native.native_session_id if native else None,
        resumed_from_run_id=native.resumed_from_run_id if native else None)


class _LocalAdapter:
    def __init__(self, version: str):
        self.version = version

    def compatibility(self):
        return compatibility(self.provider, self.version)

    def probe(self, executable: Path, isolated_root: Path):
        return probe(self.provider, executable, isolated_root)

    def prepare(self, data):
        return prepare(self.provider, self.version, data)

    def launch(self, executable: Path, prepared: PreparationResult, *, native: NativeBinding | None = None,
               model: str | None = None) -> LaunchSpec:
        return _launch(self.provider, self.version, executable, prepared, native, model)

    def discover(self, roots, binding):
        return discover(self.provider, self.version, roots, binding)

    def translate_hook(self, binding, source, payload, *, sequence, observed_at):
        return translate_hook(self.provider, self.version, binding, source, payload,
            sequence=sequence, observed_at=observed_at)

    def composer(self, raw, cursor=None):
        return classify(self.provider, self.version, raw, cursor)


class ClaudeAdapter(_LocalAdapter):
    provider = "claude"


class CodexAdapter(_LocalAdapter):
    provider = "codex"
