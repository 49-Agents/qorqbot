"""Component deployment recipes.

The orchestration and approval rules live in ``worker.py``.  Adapters only
describe component-specific effects, which keeps adding a recipe small and
prevents every repository from growing a second release workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import json
import base64
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import threading
from typing import Any, Callable, Protocol

from . import artifact_placement, local_integration
from .errors import ReleaseError, UncertainRemoteEffect, UnsupportedAdapter
from .io import atomic_write, digest, file_digest


class Executor(Protocol):
    def run(self, argv: list[str], *, check: bool = True) -> str: ...

    def write_file(self, path: Path, data: bytes, *, mode: int = 0o600) -> None: ...

    def copy_tree(self, source: Path, destination: Path) -> None: ...

    def tree_digest(self, root: Path) -> str: ...

    def file_digest(self, path: Path) -> str: ...

    def read_file(self, path: Path) -> bytes: ...

    def entry_digest(self, path: Path, relative: str = "") -> str: ...

    def entry_exists(self, path: Path) -> bool: ...


class SubprocessExecutor:
    def run(self, argv: list[str], *, check: bool = True) -> str:
        result = subprocess.run(argv, text=True, capture_output=True, check=False)
        if check and result.returncode:
            raise ReleaseError(f"command failed ({result.returncode}): {' '.join(argv[:3])}")
        return result.stdout.strip()

    def write_file(self, path: Path, data: bytes, *, mode: int = 0o600) -> None:
        atomic_write(path, data, mode=mode)

    def copy_tree(self, source: Path, destination: Path) -> None:
        destination = Path(destination)
        self.run(["mkdir", "-p", str(destination)])
        self.run(["cp", "-a", str(source) + "/.", str(destination)])

    def tree_digest(self, root: Path) -> str:
        return _tree_digest(root)

    def file_digest(self, path: Path) -> str:
        return file_digest(path)

    def read_file(self, path: Path) -> bytes:
        return Path(path).read_bytes()

    def entry_digest(self, path: Path, relative: str = "") -> str:
        return digest([_artifact_entry(Path(path), relative)])

    def entry_exists(self, path: Path) -> bool:
        path = Path(path)
        return path.exists() or path.is_symlink()

class SSHExecutor(SubprocessExecutor):
    """Run installation commands on a host through an encoded argv boundary.

    OpenSSH passes its command portion through the remote login shell.  A
    base64 JSON payload keeps user-controlled argv values out of that shell;
    only the fixed bootstrap and an alphanumeric payload are parsed remotely.
    """

    def __init__(self, host: str):
        self.host = host
        self._owner_context = threading.local()

    @staticmethod
    def _plain_bootstrap() -> str:
        return "import base64,json,os,sys; argv=json.loads(base64.urlsafe_b64decode(sys.argv[1])); os.execvp(argv[0],argv)"

    @staticmethod
    def _owner_bootstrap() -> str:
        return (
            "import base64,fcntl,json,os,socket,sys,time,uuid\n"
            "payload=json.loads(base64.urlsafe_b64decode(sys.argv[1]))\n"
            "argv=payload['argv']; directory=payload['directory']; token=payload['token']; command=payload['command']\n"
            "owner_path=os.path.join(directory,'owner.json'); lock_path=os.path.join(directory,'owner.lock')\n"
            "def process_start(pid):\n"
            "    try: return open(f'/proc/{pid}/stat').read().rsplit(')',1)[1].split()[19]\n"
            "    except (OSError,IndexError): return None\n"
            "try:\n"
            "    with open(lock_path,'a+') as lock:\n"
            "        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)\n"
            "        with open(owner_path,encoding='utf-8') as stream: owner=json.load(stream)\n"
            "        now=time.time()\n"
            "        if owner.get('owner') != token or float(owner.get('heartbeat_at',0))+float(owner.get('lease_seconds',0)) <= now: raise RuntimeError('owner-lost')\n"
            "        active=owner.get('active_command')\n"
            "        if not isinstance(active,dict) or active.get('id') != command or active.get('state') != 'pending_launch': raise RuntimeError('launch-cancelled')\n"
            "        owner['heartbeat_at']=now\n"
            "        owner['active_command']={'id':command,'state':'running','pid':os.getpid(),'host':socket.gethostname(),'process_start':process_start(os.getpid()),'started_at':active.get('started_at',now),'expires_at':now+600.0}\n"
            "        temporary=owner_path+'.tmp-'+uuid.uuid4().hex\n"
            "        with open(temporary,'w',encoding='utf-8') as stream:\n"
            "            json.dump(owner,stream,sort_keys=True,separators=(',',':')); stream.flush(); os.fsync(stream.fileno())\n"
            "        os.replace(temporary,owner_path)\n"
            "except (OSError,ValueError,RuntimeError) as exc:\n"
            "    print(str(exc) or 'owner-lost')\n"
            "    raise SystemExit(75)\n"
            "os.execvp(argv[0],argv)\n"
        )

    def _encoded_remote(self, argv: list[str], *, owner: tuple[str, str, str] | None = None) -> str:
        payload_value: Any = [str(item) for item in argv]
        bootstrap = self._plain_bootstrap()
        if owner is not None:
            directory, token, command = owner
            payload_value = {
                "argv": [str(item) for item in argv],
                "directory": directory,
                "token": token,
                "command": command,
            }
            bootstrap = self._owner_bootstrap()
        payload = base64.urlsafe_b64encode(json.dumps(payload_value).encode()).decode("ascii")
        return f"python3 -I -c {shlex.quote(bootstrap)} {payload}"

    @contextmanager
    def owner_context(self, directory: str, token: str, command: str):
        previous = getattr(self._owner_context, "value", None)
        self._owner_context.value = (directory, token, command)
        try:
            yield
        finally:
            self._owner_context.value = previous

    def run(self, argv: list[str], *, check: bool = True) -> str:
        return self._run(argv, check=check)

    def _run(self, argv: list[str], *, check: bool = True) -> str:
        remote = self._encoded_remote(argv, owner=getattr(self._owner_context, "value", None))
        result = subprocess.run(
            ["ssh", "--", self.host, remote],
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode:
            # OpenSSH uses 255 for a transport/setup failure.  The remote
            # command may already have been exec'd when the response is lost,
            # so callers must reconcile its destination-side effect before
            # allowing the lock to be released or the operation retried.
            if result.returncode == 255:
                raise UncertainRemoteEffect(f"remote command transport failed on {self.host}")
            raise ReleaseError(f"remote command failed ({result.returncode}) on {self.host}")
        return result.stdout.strip()

    def write_file(self, path: Path, data: bytes, *, mode: int = 0o600) -> None:
        payload = base64.urlsafe_b64encode(json.dumps([str(path), base64.b64encode(data).decode("ascii"), oct(mode)[2:]]).encode()).decode("ascii")
        code = (
            "import base64,json,pathlib,sys; "
            "p,encoded,mode=json.loads(base64.urlsafe_b64decode(sys.argv[1])); "
            "p=pathlib.Path(p); p.parent.mkdir(parents=True, exist_ok=True); "
            "p.write_bytes(base64.b64decode(encoded)); p.chmod(int(mode, 8))"
        )
        self.run(["python3", "-I", "-c", code, payload])

    def copy_tree(
        self,
        source: Path,
        destination: Path,
        *,
        launch_context: Callable[[str], Any] | None = None,
    ) -> None:
        if launch_context is None:
            self.run(["mkdir", "-p", str(destination)])
        else:
            with launch_context("copy-mkdir"):
                self.run(["mkdir", "-p", str(destination)])
        tar = subprocess.Popen(["tar", "-C", str(source), "-cf", "-", "."], stdout=subprocess.PIPE)
        assert tar.stdout is not None
        result: subprocess.CompletedProcess[bytes] | None = None
        try:
            def extract() -> None:
                nonlocal result, tar_returncode
                owner = getattr(self._owner_context, "value", None)
                if owner is not None:
                    remote = self._encoded_remote(["tar", "-C", str(destination), "-xf", "-"], owner=owner)
                else:
                    remote = f"tar -C {shlex.quote(str(destination))} -xf -"
                try:
                    result = subprocess.run(
                        ["ssh", "--", self.host, remote],
                        stdin=tar.stdout,
                        text=False,
                        capture_output=True,
                        check=False,
                    )
                except OSError as exc:
                    raise UncertainRemoteEffect(
                        f"artifact transfer to {self.host}:{destination} has an uncertain result"
                    ) from exc
                tar.stdout.close()
                tar_returncode = tar.wait()
                if tar_returncode or result.returncode:
                    raise UncertainRemoteEffect(
                        f"artifact transfer to {self.host}:{destination} has an uncertain result"
                    )

            if launch_context is None:
                extract()
            else:
                with launch_context("copy-extract"):
                    extract()
        finally:
            tar.stdout.close()
            tar_returncode = tar.wait()
        assert result is not None

    def tree_digest(self, root: Path) -> str:
        code = (
            "import hashlib,json,pathlib,stat,sys,os; "
            "root=pathlib.Path(sys.argv[1]); values=[]; "
            "\n"
            "for p in sorted(root.rglob('*'), key=lambda x: str(x.relative_to(root))):\n"
            " s=p.lstat(); rel=str(p.relative_to(root)); mode=stat.S_IMODE(s.st_mode)\n"
            " if stat.S_ISLNK(s.st_mode): values.append([rel,'symlink',os.readlink(p)])\n"
            " elif stat.S_ISREG(s.st_mode): values.append([rel,'file',mode,'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()])\n"
            " elif stat.S_ISDIR(s.st_mode): values.append([rel,'directory',mode])\n"
            " else: raise SystemExit('unsupported artifact entry: '+rel)\n"
            "raw=(json.dumps(values,sort_keys=True,separators=(',',':'),ensure_ascii=True)+'\\n').encode(); "
            "print('sha256:'+hashlib.sha256(raw).hexdigest())"
        )
        return self.run(["python3", "-I", "-c", code, str(root)])

    def file_digest(self, path: Path) -> str:
        return "sha256:" + self.run(["sha256sum", str(path)]).split()[0]

    def entry_digest(self, path: Path, relative: str = "") -> str:
        code = (
            "import hashlib,json,os,pathlib,stat,sys; "
            "p=pathlib.Path(sys.argv[1]); rel=sys.argv[2]; s=p.lstat(); mode=stat.S_IMODE(s.st_mode); "
            "entry=([rel,'symlink',os.readlink(p)] if stat.S_ISLNK(s.st_mode) else "
            "[rel,'file',mode,'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest()] if stat.S_ISREG(s.st_mode) else "
            "[rel,'directory',mode] if stat.S_ISDIR(s.st_mode) else None)\n"
            "if entry is None:\n raise SystemExit('unsupported artifact entry')\n"
            "raw=(json.dumps([entry],sort_keys=True,separators=(',',':'),ensure_ascii=True)+'\\n').encode(); "
            "print('sha256:'+hashlib.sha256(raw).hexdigest())"
        )
        return self.run(["python3", "-I", "-c", code, str(path), relative])

    def read_file(self, path: Path) -> bytes:
        code = "import base64,pathlib,sys; print(base64.b64encode(pathlib.Path(sys.argv[1]).read_bytes()).decode())"
        encoded = self.run(["python3", "-I", "-c", code, str(path)])
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise ReleaseError(f"remote file read returned invalid data: {path}") from exc

    def entry_exists(self, path: Path) -> bool:
        code = (
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "print('present' if p.exists() or p.is_symlink() else 'missing')"
        )
        return self.run(["python3", "-I", "-c", code, str(path)]) == "present"


@dataclass
class AdapterContext:
    plan: dict[str, Any]
    artifact_root: Path
    installation_config: dict[str, Any]
    executor: Executor
    components: tuple[dict[str, Any], ...] = ()
    stage_evidence: dict[str, dict[str, Any]] | None = None
    build_evidence: dict[str, dict[str, Any]] | None = None
    operation_intent: dict[str, Any] | None = None
    phase_callback: Callable[[str, dict[str, Any]], None] | None = None


def executor_for_installation(installation: dict[str, Any]) -> Executor:
    host = str(installation.get("host", "localhost"))
    if host not in {"localhost", "127.0.0.1", "::1"}:
        return SSHExecutor(host)
    return SubprocessExecutor()


def executor_for_component(
    component: dict[str, Any],
    installation: dict[str, Any],
    fallback: Executor | None = None,
    *,
    action: str | None = None,
) -> Executor:
    """Select a component's frozen destination, never the dashboard host by inference."""

    recipe_field = {
        "adopt": "adoption_recipe",
        "deploy": "deployment_recipe",
        "inspect": "deployment_recipe",
        "recover": "recovery_recipe",
    }.get(action or "deploy", "deployment_recipe")
    recipe = component.get(recipe_field, {})
    if not isinstance(recipe, dict):
        recipe = {}
    host = recipe.get("host")
    if action == "recover" and not host:
        deployment_recipe = component.get("deployment_recipe", {})
        if isinstance(deployment_recipe, dict):
            host = deployment_recipe.get("host")
    if not host or str(host) == str(installation.get("host", "localhost")):
        return fallback or executor_for_installation(installation)
    return executor_for_installation({"host": str(host)})


def _tree_digest(root: Path) -> str:
    values = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item.relative_to(root))):
        values.append(_artifact_entry(path, str(path.relative_to(root))))
    return digest(values)


def _artifact_entry(path: Path, relative: str) -> list[Any]:
    metadata = path.lstat()
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISLNK(metadata.st_mode):
        return [relative, "symlink", os.readlink(path)]
    if stat.S_ISREG(metadata.st_mode):
        return [relative, "file", mode, file_digest(path)]
    if stat.S_ISDIR(metadata.st_mode):
        return [relative, "directory", mode]
    raise ReleaseError(f"unsupported artifact entry type: {path}")


def _path_digest(path: Path) -> str:
    if path.is_dir() and not path.is_symlink():
        return _tree_digest(path)
    return digest([_artifact_entry(path, "")])


def _build_output_paths(component: dict[str, Any]) -> list[Path]:
    raw = component.get("build_outputs", [])
    if not isinstance(raw, list):
        raise UnsupportedAdapter(f"{component['id']}: build_outputs must be a list of repository-relative paths")
    repo = Path(component["repository"]).resolve()
    paths: list[Path] = []
    for item in raw:
        value = item.get("path") if isinstance(item, dict) else item
        if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
            raise UnsupportedAdapter(f"{component['id']}: build_outputs must contain safe relative paths")
        path = repo / value
        if path not in paths:
            paths.append(path)
    return paths


def _capture_build_outputs(component: dict[str, Any]) -> list[dict[str, str]]:
    outputs = []
    repo = Path(component["repository"]).resolve()
    for path in _build_output_paths(component):
        if not path.exists() and not path.is_symlink():
            raise ReleaseError(f"{component['id']}: declared build output is missing: {path.relative_to(repo)}")
        outputs.append({"path": str(path.relative_to(repo)), "digest": _path_digest(path)})
    return outputs


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _copy_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        _remove_path(destination)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


def _build_output_evidence(component: dict[str, Any]) -> dict[str, Any]:
    return {"build_outputs": _capture_build_outputs(component)}


def _archive_source(
    component: dict[str, Any],
    destination: Path,
    *,
    build_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for child in destination.iterdir():
        _remove_path(child)
    repo = Path(component["repository"])
    try:
        archive = subprocess.check_output(["git", "-C", str(repo), "archive", component["source_commit"]])
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(f"cannot archive {component['id']} at {component['source_commit']}") from exc
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        subprocess.run(["tar", "-x", "-f", "-", "-C", str(temporary)], input=archive, check=True)
        for child in temporary.iterdir():
            target = destination / child.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(child), target)
        repo = Path(component["repository"]).resolve()
        for output in (build_evidence or {}).get("build_outputs", []):
            if not isinstance(output, dict) or not isinstance(output.get("path"), str) or not isinstance(output.get("digest"), str):
                raise ReleaseError(f"{component['id']}: build output evidence is invalid")
            source = repo / output["path"]
            if not source.exists() and not source.is_symlink():
                raise ReleaseError(f"{component['id']}: declared build output disappeared before staging: {output['path']}")
            if _path_digest(source) != output["digest"]:
                raise ReleaseError(f"{component['id']}: build output changed after build: {output['path']}")
            _copy_path(source, destination / output["path"])
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return {"artifact": str(destination), "source_commit": component["source_commit"], "tree_digest": _tree_digest(destination)}


def _stage_record(context: AdapterContext, component_id: str) -> dict[str, Any]:
    evidence = (context.stage_evidence or {}).get(component_id)
    if not evidence or not evidence.get("tree_digest"):
        raise ReleaseError(f"{component_id}: staged artifact evidence is missing from the durable record")
    return evidence


def _verify_staged_artifact(component: dict[str, Any], context: AdapterContext) -> Path:
    artifact = context.artifact_root / component["id"]
    if not artifact.is_dir():
        raise ReleaseError(f"{component['id']}: stage the immutable artifact before deployment")
    expected = _stage_record(context, component["id"])["tree_digest"]
    actual = _tree_digest(artifact)
    if actual != expected:
        raise ReleaseError(f"{component['id']}: staged artifact changed after its recorded stage evidence")
    return artifact


def _verify_destination(executor: Executor, source: Path, destination: Path, expected: str) -> str:
    actual = executor.tree_digest(destination)
    if actual != expected:
        raise ReleaseError(f"artifact transfer changed {source} before runtime assembly")
    return actual


def _recipe_values(
    component: dict[str, Any],
    context: AdapterContext,
    *,
    release: str | None = None,
    artifact: str | None = None,
) -> dict[str, str]:
    return {
        "component": component["id"],
        "source_commit": component["source_commit"],
        "plan_digest": context.plan["plan_digest"],
        "release": release or "",
        "artifact": artifact or "",
        "agents": ",".join(sorted(component.get("adoption", {}).get("agents", [])))
        if isinstance(component.get("adoption"), dict)
        else "",
    }


def _recipe_argv(
    component: dict[str, Any],
    recipe: dict[str, Any],
    field: str,
    *,
    context: AdapterContext | None = None,
    release: str | None = None,
    artifact: str | None = None,
) -> list[str]:
    raw = recipe.get(field)
    if not isinstance(raw, list) or not raw or any(not isinstance(item, (str, int, float)) for item in raw):
        raise UnsupportedAdapter(f"{component['id']}: recipe.{field} must be a non-empty argv list")
    values = _recipe_values(component, context, release=release, artifact=artifact) if context else {
        "component": component["id"],
        "source_commit": component["source_commit"],
        "plan_digest": "",
        "release": release or "",
        "artifact": artifact or "",
        "agents": ",".join(sorted(component.get("adoption", {}).get("agents", [])))
        if isinstance(component.get("adoption"), dict)
        else "",
    }
    argv = [_expand_placeholders(str(item), values) for item in raw]
    if any(item in {";", "&&", "||", "|", ">", "<"} for item in argv):
        raise UnsupportedAdapter(f"{component['id']}: recipe.{field} must contain direct argv, not shell syntax")
    return argv


def _expand_placeholders(value: str, values: dict[str, str]) -> str:
    for key, replacement in values.items():
        value = value.replace("{" + key + "}", replacement)
    return value


def _receipt_path(
    component: dict[str, Any],
    context: AdapterContext,
    recipe: dict[str, Any],
    *,
    release: str | None = None,
    artifact: str | None = None,
) -> str:
    try:
        raw = recipe["receipt_path"]
    except KeyError as exc:
        raise UnsupportedAdapter(f"{component['id']}: receipt_path is required for effect verification") from exc
    return _expand_placeholders(
        str(raw), _recipe_values(component, context, release=release, artifact=artifact)
    )


def _run_receipt_recipe(
    component: dict[str, Any],
    context: AdapterContext,
    recipe: dict[str, Any],
    field: str,
    *,
    command_field: str = "command",
    operation: str,
    release: str | None = None,
    artifact: str | None = None,
    artifact_digest: str | None = None,
    agents: list[str] | None = None,
) -> dict[str, Any]:
    argv = _recipe_argv(component, recipe, command_field, context=context, release=release, artifact=artifact)
    output = context.executor.run(argv)
    observed = _observe_receipt(
        component,
        context,
        recipe,
        operation=operation,
        release=release,
        artifact=artifact,
        artifact_digest=artifact_digest,
        agents=agents,
    )
    return {"command": argv, "command_output_digest": hashlib.sha256(output.encode()).hexdigest(), **observed}


def _observe_receipt(
    component: dict[str, Any],
    context: AdapterContext,
    recipe: dict[str, Any],
    *,
    operation: str,
    release: str | None = None,
    artifact: str | None = None,
    artifact_digest: str | None = None,
    agents: list[str] | None = None,
) -> dict[str, Any]:
    receipt = _receipt_path(component, context, recipe, release=release, artifact=artifact)
    try:
        raw = context.executor.read_file(Path(receipt))
        value = json.loads(raw)
    except (OSError, ValueError, ReleaseError) as exc:
        raise ReleaseError(f"{component['id']}: effect receipt is missing or invalid: {receipt}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"{component['id']}: effect receipt is not an object: {receipt}")
    expected = {
        "schema_version": 1,
        "operation": operation,
        "release_id": context.plan["release_id"],
        "plan_digest": context.plan["plan_digest"],
        "component": component["id"],
        "source_commit": component["source_commit"],
        "outcome": "succeeded",
    }
    if release is not None:
        expected["release"] = release
    if artifact_digest is not None:
        expected["artifact_digest"] = artifact_digest
    if agents is not None:
        expected["agents"] = sorted(agents)
    mismatches = [key for key, expected_value in expected.items() if value.get(key) != expected_value]
    if mismatches:
        raise ReleaseError(f"{component['id']}: effect receipt is not bound to this {operation} action ({', '.join(mismatches)})")
    if component.get("experience_binding") and operation == "adopt":
        from .experience_binding import expected_receipt
        selected = component["experience_binding"]
        binding_expected = expected_receipt(context.plan, component)
        if value.get("experience_binding") != binding_expected:
            raise ReleaseError("adoption receipt is missing the exact experience binding receipt")
        binding_actual = json.loads(context.executor.read_file(Path(selected["receipt_path"])))
        if binding_actual != binding_expected:
            raise ReleaseError("experience binding receipt changed or belongs to another plan")
        bound_target = context.executor.run(["readlink", "-f", binding_expected["binding_path"]]).strip()
        if bound_target != selected["dataset_path"]:
            raise ReleaseError("experience binding target changed")
        marker = json.loads(context.executor.read_file(Path(selected["dataset_path"]).parent / "dataset.json"))
        contract = json.loads(context.executor.read_file(Path(selected["source_view"]) / "experiences/contract.json"))
        session_format = contract.get("session_format") if isinstance(contract, dict) else None
        if type(session_format) is not int or session_format not in (2, 3) or contract != {"schema_version": 1, "channel_id": component["id"], "session_format": session_format, "binding": "experiences/sessions", "dataset_layout": f"plugin-experiences/{component['id']}/sessions"}:
            raise ReleaseError("unsupported installed experience contract")
        if marker != {"schema_version": 1, "dataset_id": selected["dataset_id"], "channel_id": component["id"], "session_format": session_format}:
            raise ReleaseError("experience dataset marker changed")
        contract_digest = context.executor.file_digest(Path(selected["source_view"]) / "experiences/contract.json")
        if contract_digest != "sha256:" + selected["contract_sha256"]:
            raise ReleaseError("installed experience contract changed")
    receipt_digest = context.executor.file_digest(Path(receipt))
    return {"receipt": receipt, "receipt_digest": receipt_digest, "receipt_fields": expected}


class Adapter:
    name = "base"
    categories: frozenset[str] = frozenset()
    runtime = False

    def validate(self, component: dict[str, Any], installation: dict[str, Any]) -> None:
        if component["category"] not in self.categories:
            raise UnsupportedAdapter(f"adapter {self.name} cannot handle {component['category']}: {component['id']}")

    def _run_declared(self, component: dict[str, Any], field: str) -> list[dict[str, Any]]:
        results = []
        for raw in component.get(field, []):
            if isinstance(raw, dict):
                raw = raw.get("command")
                if not isinstance(raw, list):
                    raise ReleaseError(f"{component['id']}: {field} command must be a direct argv list")
            argv = [str(item) for item in raw] if isinstance(raw, list) else shlex.split(str(raw))
            if not argv or any(item in {";", "&&", "||", "|", ">", "<"} for item in argv):
                raise ReleaseError(f"{component['id']}: {field} must contain direct argv commands, not shell pipelines")
            result = subprocess.run(argv, cwd=component["repository"], text=True, capture_output=True, check=False)
            evidence = {
                "argv": argv,
                "returncode": result.returncode,
                "stdout_digest": hashlib.sha256(result.stdout.encode()).hexdigest(),
                "stderr_digest": hashlib.sha256(result.stderr.encode()).hexdigest(),
            }
            if result.returncode:
                raise ReleaseError(f"{component['id']}: declared {field} command failed: {argv[0]}")
            results.append(evidence)
        return results

    def _verify_source(self, component: dict[str, Any], *, require_clean: bool = True) -> None:
        repo = Path(component["repository"])
        status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], text=True, capture_output=True, check=False)
        if status.returncode or (require_clean and (status.stdout.strip() or component.get("dirty"))):
            raise ReleaseError(f"component {component['id']} repository is dirty; clean it before release checks/build")
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True, capture_output=True, check=False).stdout.strip()
        if head != component["source_commit"]:
            raise ReleaseError(f"component {component['id']} source commit changed; re-plan and retest")

    def inspect(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        return {"adapter": self.name, "component": component["id"], "runtime": self.runtime}

    def check(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        self.validate(component, context.installation_config)
        self._verify_source(component)
        repo = Path(component["repository"])
        return {
            "checked": True,
            "source_commit": component["source_commit"],
            "repository": str(repo),
            "tests": self._run_declared(component, "tests"),
            "checks": self._run_declared(component, "checks"),
        }

    def build(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        self._verify_source(component)
        return {
            "built": bool(component.get("build")),
            "source_commit": component["source_commit"],
            "build": self._run_declared(component, "build"),
            **_build_output_evidence(component),
        }

    def stage(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        return _archive_source(
            component,
            context.artifact_root / component["id"],
            build_evidence=(context.build_evidence or {}).get(component["id"]),
        )

    def deploy(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        return {"deployed": False, "reason": "source publication is separate from runtime deployment"}

    def deploy_group(self, components: list[dict[str, Any]], context: AdapterContext) -> dict[str, Any]:
        if len(components) != 1:
            raise UnsupportedAdapter(f"{self.name}: grouped deployment is not implemented for this adapter")
        return self.deploy(components[0], context)

    def adopt(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        return {"adopted": False, "reason": "no explicit adoption selection"}

    def reconcile(self, component: dict[str, Any], context: AdapterContext, action: str, before: Any) -> dict[str, Any]:
        return {"safe_to_retry": False, "observed": before, "reason": f"{self.name} cannot inspect the {action} effect safely"}

    def recover(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        return {"recovered": False, "reason": "component has no runtime effect to recover"}


class DocumentationAdapter(Adapter):
    name = "documentation"
    categories = frozenset({"documentation"})


class SourceLibraryAdapter(Adapter):
    """Test, merge and archive a dependency; installation belongs to its consumer."""

    name = "source-library"
    categories = frozenset({"library"})


class ImmutablePackageAdapter(Adapter):
    name = "immutable-package"
    categories = frozenset({"role", "plugin"})

    def build(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        self._verify_source(component)
        return {
            "built": True,
            "kind": component["category"],
            "source_commit": component["source_commit"],
            "build": self._run_declared(component, "build"),
            **_build_output_evidence(component),
        }

    def adopt(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        selection = component.get("adoption")
        if not selection:
            return {"adopted": False, "reason": "component has no selected agents"}
        if not isinstance(selection, dict) or not selection.get("agents"):
            raise ReleaseError(f"{component['id']}: adoption must name selected agents explicitly")
        recipe = component.get("adoption_recipe")
        if not isinstance(recipe, dict):
            raise UnsupportedAdapter(f"{component['id']}: canonical publication/adoption requires an explicit adoption_recipe")
        artifact = _verify_staged_artifact(component, context)
        release_root = recipe.get("release_root")
        if not isinstance(release_root, str) or not release_root:
            raise UnsupportedAdapter(f"{component['id']}: adoption_recipe.release_root is required for destination transfer")
        target = Path(release_root) / context.plan["release_id"] / component["id"]
        expected_digest = _stage_record(context, component["id"])["tree_digest"]
        context.executor.run(["test", "!", "-e", str(target)])
        context.executor.run(["mkdir", "-p", str(target)])
        copier = getattr(context.executor, "copy_tree", None)
        if copier is None:
            raise ReleaseError(f"{component['id']}: executor cannot transfer package artifact")
        copier(artifact, target)
        _verify_destination(context.executor, artifact, target, expected_digest)
        agents = sorted(selection["agents"])
        receipt = _run_receipt_recipe(
            component,
            context,
            recipe,
            "adoption_recipe",
            operation="adopt",
            release=str(target),
            artifact=str(target),
            artifact_digest=expected_digest,
            agents=agents,
        )
        return {
            "adopted": True,
            "package": component["id"],
            "agents": agents,
            "source_commit": component["source_commit"],
            "artifact": str(target),
            "artifact_digest": expected_digest,
            "effect": "canonical publication/adoption command completed and receipt verified",
            **receipt,
        }

    def reconcile(self, component: dict[str, Any], context: AdapterContext, action: str, before: Any) -> dict[str, Any]:
        if action != "adopt":
            return super().reconcile(component, context, action, before)
        recipe = component.get("adoption_recipe")
        if not isinstance(recipe, dict):
            return {"safe_to_retry": False, "observed": before, "reason": "adoption has no inspectable receipt recipe"}
        try:
            artifact = _verify_staged_artifact(component, context)
            release_root = recipe["release_root"]
            target = Path(str(release_root)) / context.plan["release_id"] / component["id"]
            expected_digest = _stage_record(context, component["id"])["tree_digest"]
            observed_digest = context.executor.tree_digest(target)
            if observed_digest != expected_digest:
                raise ReleaseError("adopted artifact on destination does not match recorded stage evidence")
            selection = component.get("adoption", {})
            agents = sorted(selection.get("agents", [])) if isinstance(selection, dict) else []
        except (KeyError, ReleaseError) as exc:
            return {"safe_to_retry": False, "observed": before, "reason": f"adoption artifact is not verified: {exc}"}
        try:
            receipt = _observe_receipt(
                component,
                context,
                recipe,
                operation="adopt",
                release=str(target),
                artifact=str(target),
                artifact_digest=expected_digest,
                agents=agents,
            )
        except (OSError, ReleaseError) as exc:
            return {"safe_to_retry": False, "observed": before, "reason": f"adoption receipt is not verified: {exc}"}
        return {"already_succeeded": True, "safe_to_retry": False, "receipt_verified": receipt, "reason": "adoption receipt already exists"}


class IndependentEngineAdapter(Adapter):
    name = "independent-engine"
    categories = frozenset({"engine", "outreach"})
    runtime = True

    def validate(self, component: dict[str, Any], installation: dict[str, Any]) -> None:
        super().validate(component, installation)
        recipe = component.get("deployment_recipe")
        if not isinstance(recipe, dict):
            raise UnsupportedAdapter(f"{component['id']}: independent engine needs deployment_recipe; dashboard host is not inferred")
        required = {"service", "release_root", "health", "backup", "configuration", "deploy_command", "receipt_path", "health_command", "inspect_command"}
        missing = required - set(recipe)
        if missing:
            raise UnsupportedAdapter(f"{component['id']}: engine recipe is missing {sorted(missing)}")
        if not isinstance(recipe["deploy_command"], list) or not any("{release}" in str(item) for item in recipe["deploy_command"]):
            raise UnsupportedAdapter(f"{component['id']}: deployment_recipe.deploy_command must explicitly target {{release}}")
        for field in ("health_command", "inspect_command"):
            if not isinstance(recipe[field], list) or not any("{release}" in str(item) for item in recipe[field]):
                raise UnsupportedAdapter(f"{component['id']}: deployment_recipe.{field} must explicitly target {{release}}")
        local_integration.validate_component(component, installation)

    def _verify_source(self, component: dict[str, Any], *, require_clean: bool = True) -> None:
        if local_integration.is_local(component):
            local_integration.verify_frozen(component)
            return
        super()._verify_source(component, require_clean=require_clean)

    def stage(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        if local_integration.is_local(component):
            local_integration.verify_frozen(component)
        return super().stage(component, context)

    def inspect(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        self.validate(component, context.installation_config)
        recipe = component["deployment_recipe"]
        release = str(Path(recipe["release_root"]) / context.plan["release_id"])
        inspect_argv = _recipe_argv(component, recipe, "inspect_command", context=context, release=release)
        output = context.executor.run(inspect_argv)
        return {
            "adapter": self.name,
            "service": recipe["service"],
            "health": recipe["health"],
            "host_source": recipe.get("host", "recipe"),
            "inspect": {"argv": inspect_argv, "output_digest": hashlib.sha256(output.encode()).hexdigest()},
        }

    def build(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        self.validate(component, context.installation_config)
        self._verify_source(component)
        return {
            "built": True,
            "check_notes": component.get("check_notes", []),
            "build": self._run_declared(component, "build"),
            "source_commit": component["source_commit"],
            **_build_output_evidence(component),
        }

    def deploy(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        self.validate(component, context.installation_config)
        recipe = component["deployment_recipe"]
        artifact = _verify_staged_artifact(component, context)
        release = str(Path(recipe["release_root"]) / context.plan["release_id"])
        if local_integration.is_local(component):
            # Immediately before the first destination effect.
            local_integration.verify_frozen(component)
        if local_integration.placement_enabled(component):
            artifact_placement.place(context.executor, artifact, recipe["release_root"], context.plan["release_id"])
        else:
            context.executor.run(["test", "!", "-e", release])
            context.executor.run(["mkdir", "-p", release])
            copier = getattr(context.executor, "copy_tree", None)
            if copier is None:
                raise ReleaseError(f"{component['id']}: executor cannot transfer staged engine artifact")
            copier(artifact, Path(release))
        expected_digest = _stage_record(context, component["id"])["tree_digest"]
        _verify_destination(context.executor, artifact, Path(release), expected_digest)
        receipt = _run_receipt_recipe(
            component,
            context,
            recipe,
            "deployment_recipe",
            command_field="deploy_command",
            operation="deploy",
            release=release,
            artifact=release,
            artifact_digest=expected_digest,
        )
        health_argv = _recipe_argv(component, recipe, "health_command", context=context, release=release, artifact=release)
        health_output = context.executor.run(health_argv)
        inspect_argv = _recipe_argv(component, recipe, "inspect_command", context=context, release=release, artifact=release)
        inspect_output = context.executor.run(inspect_argv)
        return {
            "deployed": True,
            "service": recipe["service"],
            "health": recipe["health"],
            "backup": recipe["backup"],
            "configuration": "preserve recipe-declared private configuration; values never enter the record",
            "release": release,
            "receipt_verified": receipt,
            "health_verified": {"argv": health_argv, "output_digest": hashlib.sha256(health_output.encode()).hexdigest()},
            "runtime_identity": {"argv": inspect_argv, "output_digest": hashlib.sha256(inspect_output.encode()).hexdigest()},
        }

    def reconcile(self, component: dict[str, Any], context: AdapterContext, action: str, before: Any) -> dict[str, Any]:
        if action == "recover":
            recipe = component.get("recovery_recipe")
            if not isinstance(recipe, dict):
                return {"safe_to_retry": False, "observed": before, "reason": "recovery has no receipt-backed postcondition"}
            release = str(Path(component["deployment_recipe"]["release_root"]) / context.plan["release_id"])
            try:
                artifact = _verify_staged_artifact(component, context)
                expected_digest = _stage_record(context, component["id"])["tree_digest"]
                if context.executor.tree_digest(Path(release)) != expected_digest:
                    raise ReleaseError("engine release artifact does not match recorded stage evidence")
            except (OSError, ReleaseError) as exc:
                return {"safe_to_retry": False, "observed": before, "reason": f"engine artifact is not verified: {exc}"}
            try:
                receipt = _observe_receipt(
                    component,
                    context,
                    recipe,
                    operation="recover",
                    release=release,
                    artifact=release,
                )
            except (OSError, ReleaseError) as exc:
                return {"safe_to_retry": False, "observed": before, "reason": f"recovery receipt is not verified: {exc}"}
            return {"already_succeeded": True, "safe_to_retry": False, "receipt_verified": receipt, "reason": "recovery receipt already exists"}
        if action != "deploy":
            return {"safe_to_retry": False, "observed": before, "reason": f"independent engine has no {action} reconciliation postcondition"}
        recipe = component["deployment_recipe"]
        release = str(Path(recipe["release_root"]) / context.plan["release_id"])
        try:
            artifact = _verify_staged_artifact(component, context)
            expected_digest = _stage_record(context, component["id"])["tree_digest"]
            if context.executor.tree_digest(Path(release)) != expected_digest:
                raise ReleaseError("engine release artifact does not match recorded stage evidence")
            receipt = _observe_receipt(
                component,
                context,
                recipe,
                operation="deploy",
                release=release,
                artifact=release,
                artifact_digest=expected_digest,
            )
            health_argv = _recipe_argv(component, recipe, "health_command", context=context, release=release, artifact=release)
            health_output = context.executor.run(health_argv)
            inspect_argv = _recipe_argv(component, recipe, "inspect_command", context=context, release=release, artifact=release)
            inspect_output = context.executor.run(inspect_argv)
        except (OSError, ReleaseError) as exc:
            return {"safe_to_retry": False, "observed": before, "reason": f"engine deployment postconditions are not verified: {exc}"}
        return {
            "already_succeeded": True,
            "safe_to_retry": False,
            "receipt_verified": receipt,
            "health_verified": {"argv": health_argv, "output_digest": hashlib.sha256(health_output.encode()).hexdigest()},
            "runtime_identity": {"argv": inspect_argv, "output_digest": hashlib.sha256(inspect_output.encode()).hexdigest()},
            "reason": "engine receipt, health and identity postconditions verified",
        }

    def recover(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        recipe = component.get("recovery_recipe")
        if not isinstance(recipe, dict):
            raise UnsupportedAdapter(f"{component['id']}: independent engine recovery requires an explicit recovery_recipe")
        release = str(Path(component["deployment_recipe"]["release_root"]) / context.plan["release_id"])
        receipt = _run_receipt_recipe(
            component,
            context,
            recipe,
            "recovery_recipe",
            operation="recover",
            release=release,
            artifact=release,
        )
        return {"recovered": True, "receipt_verified": receipt}


ADAPTER_TYPES = {
    SourceLibraryAdapter.name: SourceLibraryAdapter,
    DocumentationAdapter.name: DocumentationAdapter,
    ImmutablePackageAdapter.name: ImmutablePackageAdapter,
    IndependentEngineAdapter.name: IndependentEngineAdapter,
}


def adapter_for(component: dict[str, Any]) -> Adapter:
    name = component.get("adapter")
    try:
        return ADAPTER_TYPES[name]()
    except (KeyError, TypeError) as exc:
        raise UnsupportedAdapter(f"{component['id']}: adapter recipe {name!r} is not registered") from exc
