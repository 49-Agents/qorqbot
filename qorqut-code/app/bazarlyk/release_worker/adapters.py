"""Component deployment recipes.

The orchestration and approval rules live in ``worker.py``.  Adapters only
describe component-specific effects, which keeps adding a recipe small and
prevents every repository from growing a second release workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import functools
import hashlib
import json
import base64
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import threading
from typing import Any, Callable, Protocol

from bazarlyk.catalog.packages import validate_schema as validate_catalog_schema
from bazarlyk.catalog.sources import SOURCE_SCHEMA, load_sources
from bazarlyk.contracts import DomainError

from . import entry_verifier
from .errors import ReleaseError, UncertainRemoteEffect, UnsupportedAdapter
from .io import atomic_json, atomic_write, digest, file_digest


class Executor(Protocol):
    def run(self, argv: list[str], *, check: bool = True) -> str: ...

    def write_file(self, path: Path, data: bytes, *, mode: int = 0o600) -> None: ...

    def copy_tree(self, source: Path, destination: Path) -> None: ...

    def tree_digest(self, root: Path) -> str: ...

    def file_digest(self, path: Path) -> str: ...

    def read_file(self, path: Path) -> bytes: ...

    def entry_digest(self, path: Path, relative: str = "") -> str: ...

    def entry_exists(self, path: Path) -> bool: ...

    def verify_entries(
        self,
        destination: Path,
        expected_entries: list[list[Any]],
        *,
        ownership_check: Callable[[], None] | None = None,
    ) -> None: ...


_ENTRY_VERDICT_LIMIT = 64 * 1024


@functools.lru_cache(maxsize=1)
def _entry_verifier_source() -> str:
    return Path(entry_verifier.__file__).read_text(encoding="utf-8")


def _raise_entry_mismatch(reason: str, destination: Path, relative: str | None) -> None:
    target = destination / relative if relative else destination
    if reason == "missing":
        raise ReleaseError(f"assembled artifact entry is missing after deployment: {target}")
    if reason == "changed":
        raise ReleaseError(f"assembled artifact file changed after deployment: {target}")
    if reason == "unreadable":
        raise ReleaseError(f"assembled artifact entry cannot be read after deployment: {target}")
    if reason == "owner-lost":
        raise ReleaseError(f"release lock ownership was lost while verifying artifact entries: {destination}")
    raise ReleaseError(f"artifact verification manifest is invalid: {ascii(relative) if relative else destination}")


def _check_entry_verdict(output: str, destination: Path, count: int, host: str) -> None:
    """Accept only a complete success verdict; any other response fails closed."""

    verdict: Any = None
    if len(output) <= _ENTRY_VERDICT_LIMIT:
        try:
            verdict = json.loads(output)
        except ValueError:
            verdict = None
    if isinstance(verdict, dict):
        checked = verdict.get("checked")
        if verdict.get("ok") is True and isinstance(checked, int) and not isinstance(checked, bool) and checked == count:
            return
        if verdict.get("ok") is False and verdict.get("reason") in entry_verifier.REASONS:
            path = verdict.get("path")
            _raise_entry_mismatch(verdict["reason"], destination, path[:512] if isinstance(path, str) else None)
    raise ReleaseError(f"remote artifact verification returned an invalid response from {host}")


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

    def verify_entries(
        self,
        destination: Path,
        expected_entries: list[list[Any]],
        *,
        ownership_check: Callable[[], None] | None = None,
    ) -> None:
        """Compare every expected entry in one pass; raise ``ReleaseError`` on any difference.

        ``ownership_check`` is a read-only callback that raises if the caller
        lost its lock; it runs before, every 128 entries during, and after
        the comparison.
        """

        destination = Path(destination)
        try:
            entry_verifier.verify(str(destination), expected_entries, ownership_check)
        except entry_verifier.Mismatch as exc:
            _raise_entry_mismatch(exc.reason, destination, exc.path)


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

    def _run(self, argv: list[str], *, check: bool = True, stdin: str | None = None) -> str:
        remote = self._encoded_remote(argv, owner=getattr(self._owner_context, "value", None))
        options: dict[str, Any] = {} if stdin is None else {"input": stdin}
        result = subprocess.run(
            ["ssh", "--", self.host, remote],
            text=True,
            capture_output=True,
            check=False,
            **options,
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

    def verify_entries(
        self,
        destination: Path,
        expected_entries: list[list[Any]],
        *,
        ownership_check: Callable[[], None] | None = None,
    ) -> None:
        # The manifest travels on stdin, not argv.  Ownership is read on the
        # destination host, where the verifier can see the owner record; a
        # local callback would observe the wrong host.
        owner = getattr(self._owner_context, "value", None)
        request = {
            "destination": str(destination),
            "entries": expected_entries,
            "owner": None if owner is None else dict(zip(("directory", "token", "command"), owner)),
        }
        output = self._run(
            ["python3", "-I", "-c", _entry_verifier_source()],
            stdin=json.dumps(request, separators=(",", ":")),
        )
        _check_entry_verdict(output, Path(destination), len(expected_entries), self.host)


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


def _verify_destination_files(executor: Executor, source: Path, destination: Path) -> None:
    """Verify immutable source files after runtime-only assembly additions.

    The dashboard release adds a virtualenv below ``biz-core/.venv`` after the
    source transfer, so a whole-directory digest is intentionally not valid
    during resume.  Every staged source entry must still match its destination;
    the executor compares the whole manifest in one guarded operation rather
    than one command and owner-record update per entry.
    """

    expected = [
        _artifact_entry(path, str(path.relative_to(source)))
        for path in sorted(source.rglob("*"), key=lambda item: str(item.relative_to(source)))
    ]
    verify_entries = getattr(executor, "verify_entries", None)
    if not callable(verify_entries):
        raise ReleaseError("destination executor cannot verify artifact entries")
    if expected:
        verify_entries(destination, expected)


# Startup readiness policy for the dashboard health probe.  The deadline covers
# every connect, header wait, body read, retry delay and path together.
_HEALTH_DEADLINE_SECONDS = 30.0
_HEALTH_REQUEST_TIMEOUT_SECONDS = 2.0
_HEALTH_RETRY_DELAY_SECONDS = 0.25
_HEALTH_BODY_LIMIT = 1024 * 1024
_HEALTH_TRANSIENT_STATUSES = (502, 503, 504)
_HEALTH_OUTPUT_LIMIT = 64 * 1024

_HEALTH_PROBE = r'''
import errno, hashlib, json, signal, socket, sys, time, urllib.error
from urllib.request import Request, urlopen

spec = json.loads(sys.argv[1])
started = time.monotonic()
budget = float(spec["deadline"])
deadline = started + budget
attempts = 0
current = None
last = (None, None)
checks = []
UNREACHABLE = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EHOSTDOWN}


class Expired(BaseException):
    pass


def expire(signum, frame):
    raise Expired()


def transient_connection(error):
    if isinstance(error, (ConnectionError, TimeoutError, socket.timeout)):
        return True
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    return isinstance(error, OSError) and error.errno in UNREACHABLE


def attempt(url):
    """One bounded request: (outcome, value, label).

    ``value`` is the body digest for ok and the HTTP status (or None) otherwise.
    """
    try:
        timeout = max(0.001, min(float(spec["request_timeout"]), deadline - time.monotonic()))
        with urlopen(Request(url, headers={"Accept": "application/json"}), timeout=timeout) as response:
            status = response.status
            body = response.read(int(spec["body_limit"]))
        if status != 200:
            return "fail", status, "HTTPStatus"
        return "ok", hashlib.sha256(body).hexdigest(), None
    except urllib.error.HTTPError as error:
        error.close()
        return ("retry" if error.code in spec["transient_statuses"] else "fail"), error.code, "HTTPError"
    except urllib.error.URLError as error:
        reason = error.reason
        label = type(reason).__name__ if isinstance(reason, BaseException) else "URLError"
        return ("retry" if transient_connection(reason) else "fail"), None, label
    except OSError as error:
        return ("retry" if transient_connection(error) else "fail"), None, type(error).__name__
    except Exception as error:
        return "fail", None, type(error).__name__


def probe():
    global attempts, current, last
    for path, url in spec["targets"]:
        current = path
        while True:
            if deadline - time.monotonic() <= 0:
                raise Expired()
            attempts += 1
            outcome, value, label = attempt(url)
            if outcome == "ok":
                checks.append({"path": path, "status": 200, "body_digest": value})
                break
            if outcome == "fail":
                return {"ok": False, "path": path, "status": value, "error": label}
            last = (value, label)
            delay = min(float(spec["retry_delay"]), deadline - time.monotonic())
            if delay <= 0:
                raise Expired()
            time.sleep(delay)
    return {"ok": True, "checks": checks}


try:
    try:
        signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, budget)
        result = probe()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
except Expired:
    result = {"ok": False, "path": current, "status": last[0], "error": "DeadlineExceeded", "last_error": last[1]}
except Exception as error:
    result = {"ok": False, "path": current, "status": None, "error": "Probe" + type(error).__name__}
result.update(attempts=attempts, elapsed=round(time.monotonic() - started, 3), timeout=budget)
sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
'''

_HEALTH_LABEL = re.compile(r"[A-Za-z0-9_]{1,64}")


def _health_number(value: Any, *, maximum: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= maximum:
        return None
    return float(value)


def _health_result(output: str, paths: list[str]) -> dict[str, Any]:
    """Validate the probe verdict and turn a failure into a bounded, body-free error."""

    verdict: Any = None
    if len(output) <= _HEALTH_OUTPUT_LIMIT:
        try:
            verdict = json.loads(output)
        except ValueError:
            verdict = None
    if not isinstance(verdict, dict):
        raise ReleaseError("health probe returned invalid output")
    if verdict.get("ok") is True:
        observed = verdict.get("checks")
        if not isinstance(observed, list) or len(observed) != len(paths):
            raise ReleaseError("health probe returned invalid output")
        checks = []
        for path, item in zip(paths, observed):
            if not isinstance(item, dict) or item.get("path") != path or item.get("status") != 200:
                raise ReleaseError("health probe returned invalid output")
            body_digest = item.get("body_digest")
            if not isinstance(body_digest, str) or len(body_digest) > 128:
                raise ReleaseError("health probe returned invalid output")
            checks.append({"path": path, "status": 200, "body_digest": body_digest})
        return {"checks": checks}
    if verdict.get("ok") is not False:
        raise ReleaseError("health probe returned invalid output")
    path = verdict.get("path")
    path = path if path in paths else "unknown"
    status = verdict.get("status")
    status = status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None
    labels = [
        value if isinstance(value, str) and _HEALTH_LABEL.fullmatch(value) else None
        for value in (verdict.get("error"), verdict.get("last_error"))
    ]
    if labels[0] == "DeadlineExceeded":
        last = f"HTTP {status}" if status else labels[1]
        detail = "deadline exceeded" + (f" (last {last})" if last else "")
    else:
        detail = f"HTTP {status}" if status else (labels[0] or "probe error")
    attempts = _health_number(verdict.get("attempts"), maximum=1_000_000)
    elapsed = _health_number(verdict.get("elapsed"), maximum=86_400)
    timeout = _health_number(verdict.get("timeout"), maximum=86_400)
    timing = ""
    if attempts is not None and elapsed is not None and timeout is not None:
        timing = f" (attempts={int(attempts)}, elapsed={elapsed:.1f}s, limit={timeout:g}s)"
    raise ReleaseError(f"health check failed: {path}: {detail}{timing}")


def _validate_source_map(path: Path, workspace: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        validate_catalog_schema(value, SOURCE_SCHEMA, "package source map")
        # Exercise the application's loader as part of staging.  It validates
        # path/manifest semantics in addition to the JSON schema.
        load_sources(workspace, config_path=path)
    except (OSError, ValueError, DomainError) as exc:
        raise ReleaseError(f"generated package-source map failed application validation: {path}") from exc
    if not value:
        raise ReleaseError(f"generated package-source map must preserve at least one source: {path}")
    return value


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


def _runtime_argv(config: dict[str, Any], field: str, target: Path) -> list[str]:
    raw = config.get(field)
    if not isinstance(raw, list) or not raw or any(not isinstance(item, (str, int, float)) for item in raw):
        raise UnsupportedAdapter(f"installation.{field} must be a non-empty argv list")
    values = {
        "release": str(target),
        "venv": str(target / "biz-core" / ".venv"),
        "python": str(target / "biz-core" / ".venv" / "bin" / "python"),
    }
    argv = [_expand_placeholders(str(item), values) for item in raw]
    if any(item in {";", "&&", "||", "|", ">", "<"} for item in argv):
        raise UnsupportedAdapter(f"installation.{field} must contain direct argv, not shell syntax")
    return argv


def _launcher_stop_argv(config: dict[str, Any], target: Path, launcher: list[str]) -> list[str]:
    raw = config.get("stopper")
    if not isinstance(raw, list) or not raw or any(not isinstance(item, (str, int, float)) for item in raw):
        raise UnsupportedAdapter("installation.stopper must be a non-empty executable argv list")
    values = {"release": str(target)}
    argv = [_expand_placeholders(str(item), values) for item in raw]
    if any(item in {";", "&&", "||", "|", ">", "<"} for item in argv):
        raise UnsupportedAdapter("installation.stopper must contain direct argv, not shell syntax")
    return argv


def _dashboard_state_dir(target: Path) -> Path:
    """Return the state directory used by the checked-in dashboard launcher."""

    return target / "biz-dashboard" / ".state"


def _verify_effective_systemd_unit(
    executor: Executor,
    service: str,
    *,
    target: Path,
    launcher: list[str],
    stopper: list[str],
    package_map: Path,
    config_file: Path,
) -> dict[str, str]:
    output = executor.run([
        "systemctl",
        "show",
        service,
        "-p",
        "Type",
        "-p",
        "PIDFile",
        "-p",
        "ExecStart",
        "-p",
        "ExecStop",
        "-p",
        "WorkingDirectory",
        "-p",
        "Environment",
    ])
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    state_dir = _dashboard_state_dir(target)
    expected_pidfile = str(state_dir / "dashboard.pid")
    required = {
        "Type": "forking",
        "PIDFile": expected_pidfile,
        "WorkingDirectory": str(target / "biz-dashboard"),
    }
    mismatches = [key for key, expected in required.items() if fields.get(key) != expected]
    start_argv = shlex.join(launcher)
    stop_argv = shlex.join(stopper)
    if start_argv not in fields.get("ExecStart", ""):
        mismatches.append("ExecStart")
    if stop_argv not in fields.get("ExecStop", ""):
        mismatches.append("ExecStop")
    environment = fields.get("Environment", "")
    for marker in (
        f"BIZ_DASH_CONF={config_file}",
        f"BIZ_DASH_STATE={state_dir}",
        f"QORQUT_PACKAGE_SOURCES={package_map}",
    ):
        if marker not in environment:
            mismatches.append(f"Environment:{marker.split('=', 1)[0]}")
    if mismatches:
        raise ReleaseError(
            f"effective {service} lifecycle does not match the assembled release: {', '.join(mismatches)}"
        )
    return {key: fields[key] for key in required} | {
        "ExecStart": fields.get("ExecStart", ""),
        "ExecStop": fields.get("ExecStop", ""),
        "Environment": environment,
    }


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


def _validate_business_template(root: Path) -> None:
    from bazarlyk.domains.ideas.conversion import SCAFFOLD

    # These are the same files consumed by idea-to-business conversion. Reject
    # symlinked parents too: a release must carry its own immutable scaffold.
    for relative in SCAFFOLD:
        if relative == ".gitignore":
            continue
        source = root / relative
        parts = Path(relative).parts
        has_symlink = any(root.joinpath(*parts[:i]).is_symlink() for i in range(len(parts) + 1))
        if not source.is_file() or has_symlink:
            raise ReleaseError(f"business template is missing a regular scaffold file: {relative}")


def _runtime_template(context: AdapterContext) -> dict[str, Any]:
    templates = [item for item in context.plan["components"] if item["category"] == "template"]
    if len(templates) != 1 or templates[0]["id"] != "biz-template":
        raise ReleaseError("runtime deployment requires a pinned biz-template component")
    artifact = _verify_staged_artifact(templates[0], context)
    _validate_business_template(artifact)
    return templates[0]


class TemplateAdapter(Adapter):
    name = "template-source"
    categories = frozenset({"template"})

    def stage(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        result = super().stage(component, context)
        if component["id"] == "biz-template":
            _validate_business_template(context.artifact_root / component["id"])
        return result

    def deploy(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        return {"deployed": False, "reason": "template source does not provision or modify businesses"}


_ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _env_value(data: bytes | str, key: str) -> str | None:
    """Find one dotenv assignment without interpreting unrelated credentials."""

    value: str | None = None
    text = data.decode() if isinstance(data, bytes) else data
    for raw in text.splitlines():
        match = _ENV_ASSIGNMENT.match(raw)
        if not match or match.group(1) != key:
            continue
        candidate = match.group(2).strip()
        if candidate and candidate[0] in {'"', "'"}:
            quote = candidate[0]
            closing = candidate.find(quote, 1)
            if closing > 0:
                candidate = candidate[1:closing]
        else:
            candidate = candidate.split("#", 1)[0].rstrip()
        value = candidate
    return value


def _append_env_override(original: bytes, key: str, value: str) -> bytes:
    """Preserve the private env file byte-for-byte and append one override."""

    separator = b"" if not original or original.endswith((b"\n", b"\r")) else b"\n"
    return original + separator + f"{key}={value}\n".encode()


class QorqutDashboardAdapter(Adapter):
    name = "qorqut-dashboard"
    categories = frozenset({"core", "dashboard"})
    runtime = True

    def validate(self, component: dict[str, Any], installation: dict[str, Any]) -> None:
        super().validate(component, installation)
        required = {
            "host", "port", "service", "release_root", "database", "private_config", "launcher", "stopper",
            "runtime_venv_source", "runtime_install_command", "runtime_validate_command",
        }
        missing = required - set(installation)
        if missing:
            raise UnsupportedAdapter(f"{component['id']}: Qorqut installation config is missing {sorted(missing)}")
        if int(installation["port"]) != 1071:
            raise UnsupportedAdapter(f"{component['id']}: production adapter requires the current port 1071, not {installation['port']}")
        if not isinstance(installation["launcher"], list) or not installation["launcher"]:
            raise UnsupportedAdapter(f"{component['id']}: launcher must name the complete release executable argv")
        if not any("{release}" in str(item) for item in installation["launcher"]):
            raise UnsupportedAdapter(f"{component['id']}: launcher must explicitly target {{release}}")
        if not isinstance(installation["stopper"], list) or not installation["stopper"]:
            raise UnsupportedAdapter(f"{component['id']}: stopper must name the complete release stop executable argv")
        if not any("{release}" in str(item) for item in installation["stopper"]):
            raise UnsupportedAdapter(f"{component['id']}: stopper must explicitly target {{release}}")
        if not isinstance(installation["runtime_venv_source"], str) or not installation["runtime_venv_source"]:
            raise UnsupportedAdapter(f"{component['id']}: runtime_venv_source must identify an existing compatible environment")
        _runtime_argv(installation, "runtime_install_command", Path("/release"))
        _runtime_argv(installation, "runtime_validate_command", Path("/release"))
        if not re.fullmatch(r"[A-Za-z0-9_.@:-]+", installation["service"]):
            raise UnsupportedAdapter("service name is not a safe systemd unit")
        override = installation.get("override_filename", "zzzzzzz-qorqut-release.conf")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.conf", override) or len(override) > 255:
            raise UnsupportedAdapter("stable systemd override filename is missing or unsafe")

    def inspect(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        self.validate(component, context.installation_config)
        config = context.installation_config
        service = config["service"]
        main_pid = context.executor.run(["systemctl", "show", service, "-p", "MainPID", "--value"])
        fragment = context.executor.run(["systemctl", "show", service, "-p", "FragmentPath", "--value"])
        drops = context.executor.run(["systemctl", "show", service, "-p", "DropInPaths", "--value"])
        if main_pid == "0":
            state = context.executor.run(["systemctl", "show", service, "-p", "ActiveState", "--value"])
            if config.get("allow_stopped_service") is not True or state != "inactive":
                raise ReleaseError("service is stopped without an explicit frozen maintenance selection")
            return {"adapter": self.name, "service": service, "host": config["host"],
                    "port": int(config["port"]), "main_pid": "0", "active_state": state,
                    "maintenance_stopped": True, "fragment_path": fragment,
                    "drop_in_paths": drops.split()}
        probe_code = (
            "import json, pathlib, sys; "
            "p=pathlib.Path('/proc')/sys.argv[1]; "
            "argv=p.joinpath('cmdline').read_bytes().decode().split('\\0'); "
            "env={}; "
            "[env.__setitem__(x.split('=',1)[0], x.split('=',1)[1]) for x in p.joinpath('environ').read_bytes().decode().split('\\0') if '=' in x and x.split('=',1)[0] in {'BIZ_ROOT','QORQUT_PACKAGE_SOURCES','QORQUT_TERMINAL_WORKSPACE','BIZ_DASH_CONF','BIZ_DASH_STATE','HOST','PORT'}]; "
            "print(json.dumps({'argv':argv,'env':env,'cwd':str(p.joinpath('cwd').resolve())}))"
        )
        try:
            runtime = json.loads(context.executor.run(["python3", "-I", "-c", probe_code, str(main_pid)]))
        except (ValueError, ReleaseError) as exc:
            raise ReleaseError(f"{component['id']}: could not inspect live PID argv/environment") from exc
        argv = runtime.get("argv", [])
        if "--port" in argv and argv[argv.index("--port") + 1] != str(config["port"]):
            raise ReleaseError(f"{component['id']}: live process is not using configured port {config['port']}")
        package_map = runtime.get("env", {}).get("QORQUT_PACKAGE_SOURCES")
        if package_map and not package_map.endswith("/package-sources.json"):
            raise ReleaseError(f"{component['id']}: live package-source selector is not a release-local map")
        return {
            "adapter": self.name,
            "service": service,
            "host": config["host"],
            "port": int(config["port"]),
            "main_pid": main_pid,
            "fragment_path": fragment,
            "drop_in_paths": drops.split(),
            "argv": argv,
            "cwd": runtime.get("cwd"),
            "argv_fingerprint": hashlib.sha256("\0".join(argv).encode()).hexdigest(),
            "selected_paths": {key: value for key, value in runtime.get("env", {}).items() if key in {"BIZ_ROOT", "QORQUT_PACKAGE_SOURCES", "QORQUT_TERMINAL_WORKSPACE", "BIZ_DASH_CONF", "BIZ_DASH_STATE"}},
            "runtime_validation": "systemd identity and selected argv/env/package-map observed; health is required before deploy",
        }

    @staticmethod
    def _identity_matches(identity: dict[str, Any], target: Path, config: dict[str, Any]) -> bool:
        selected = identity.get("selected_paths", {})
        package_map = str(target / "package-sources.json")
        if selected.get("QORQUT_PACKAGE_SOURCES") != package_map:
            return False
        # start.sh deliberately launches the child under env -i.  The
        # launcher-only BIZ_DASH_CONF/BIZ_DASH_STATE settings therefore do not
        # survive in /proc/<pid>/environ; the process cwd, argv and package-map
        # selector are the authoritative runtime identity instead.
        optional_markers = {
            "BIZ_DASH_CONF": str(target / "biz-dashboard" / ".env.dashboard"),
            "BIZ_DASH_STATE": str(_dashboard_state_dir(target)),
        }
        if any(key in selected and selected[key] != value for key, value in optional_markers.items()):
            return False
        cwd = identity.get("cwd")
        if not cwd or os.path.normpath(str(cwd)) != os.path.normpath(str(target / "biz-dashboard")):
            return False
        argv = identity.get("argv", [])
        python = str(target / "biz-core" / ".venv" / "bin" / "python")
        if not argv or os.path.normpath(str(argv[0])) != os.path.normpath(python):
            return False
        try:
            workspace = argv[argv.index("--workspace") + 1]
            port = argv[argv.index("--port") + 1]
        except (ValueError, IndexError):
            return False
        return os.path.normpath(str(workspace)) == os.path.normpath(str(target)) and port == str(config["port"])

    def build(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        self._verify_source(component)
        build = self._run_declared(component, "build")
        return {
            "built": True,
            "source_commit": component["source_commit"],
            "build": build,
            "check_notes": component.get("check_notes", [
                "python -I import bazarlyk.service and bootstrap modules",
                "pip check",
                "installed-package tests with pythonpath disabled",
                "migration history and fallback compatibility",
            ]),
            **_build_output_evidence(component),
        }

    def stage(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        value = _archive_source(
            component,
            context.artifact_root / component["id"],
            build_evidence=(context.build_evidence or {}).get(component["id"]),
        )
        config = context.installation_config
        source_map = context.artifact_root / "package-sources.json"
        source_entries: dict[str, Any] = {}
        private = Path(config["private_config"])
        private_bytes: bytes | None
        try:
            private_bytes = context.executor.read_file(private)
        except (OSError, ReleaseError):
            private_bytes = None
        existing_map = _env_value(private_bytes, "QORQUT_PACKAGE_SOURCES") if private_bytes is not None else None
        configured_map = config.get("package_source_map")
        source_workspace = Path(
            str(
                config.get("package_source_workspace")
                or config.get("stable_terminal_workspace")
                or private.parent.parent
            )
        )
        if not source_workspace.is_absolute():
            raise ReleaseError("package-source map workspace must be an absolute destination path")
        existing_map_path: Path | None = None
        existing_map_fingerprint: str | None = None
        for candidate in (existing_map, configured_map):
            if not candidate:
                continue
            candidate_path = Path(str(candidate))
            if not candidate_path.is_absolute():
                candidate_path = source_workspace / candidate_path
            try:
                map_bytes = context.executor.read_file(candidate_path)
            except (OSError, ReleaseError):
                continue
            try:
                loaded = json.loads(map_bytes)
            except (ValueError, UnicodeDecodeError) as exc:
                raise ReleaseError(f"cannot read existing package-source map {candidate_path}") from exc
            if not isinstance(loaded, dict):
                raise ReleaseError(f"existing package-source map is not an object: {candidate_path}")
            source_entries = loaded
            existing_map_path = candidate_path
            existing_map_fingerprint = context.executor.file_digest(candidate_path)
            break
        target_root = Path(config["release_root"]) / context.plan["release_id"]
        for identifier, prior in list(source_entries.items()):
            if not isinstance(prior, dict):
                continue
            path = prior.get("path")
            if isinstance(path, str) and path and not Path(path).is_absolute():
                source_entries[identifier] = {**prior, "path": str(source_workspace / path)}
        for item in context.plan["components"]:
            if item["category"] in {"role", "plugin"}:
                prior = source_entries.get(item["id"], {})
                if not isinstance(prior, dict):
                    prior = {}
                source_entries[item["id"]] = {
                    **prior,
                    "kind": item["category"],
                    "path": str(target_root / item["id"]),
                    "manifest": item.get("manifest") or prior.get("manifest", ""),
                    "repository": item["repository"],
                }
        if not source_entries:
            raise ReleaseError("existing package-source map is required; refusing to generate an empty application map")
        atomic_json(source_map, source_entries)
        _validate_source_map(source_map, target_root.parent)
        if private_bytes is not None:
            config_path = context.artifact_root / "private-config.env"
            config_path.write_bytes(_append_env_override(private_bytes, "QORQUT_PACKAGE_SOURCES", str(target_root / "package-sources.json")))
            config_path.chmod(0o600)
            value.update(
                {
                    "private_config_preserved": True,
                    "private_config_path": str(private),
                    "private_config_fingerprint": context.executor.file_digest(private),
                    "release_config_fingerprint": file_digest(config_path),
                    "package_source_map": str(source_map),
                    "package_source_map_digest": file_digest(source_map),
                    "package_source_map_source": str(existing_map_path) if existing_map_path else None,
                    "package_source_map_source_fingerprint": existing_map_fingerprint,
                    "package_source_provenance": {
                        item["id"]: {"source_commit": item["source_commit"], "kind": item["category"]}
                        for item in context.plan["components"]
                        if item["category"] in {"role", "plugin"}
                    },
                }
            )
        else:
            value.update({"private_config_preserved": False, "private_config_missing": str(private)})
        override = config.get("override_filename", "zzzzzzz-qorqut-release.conf")
        value["systemd_override"] = {
            "filename": override,
            "strategy": "stable owned override, overwritten in place; no unbounded drop-in names",
            "service": config["service"],
        }
        return value

    def deploy(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        return self.deploy_group([component], context)

    def deploy_group(self, components: list[dict[str, Any]], context: AdapterContext) -> dict[str, Any]:
        self.validate(components[0], context.installation_config)
        config = context.installation_config
        if {item["category"] for item in components} != {"core", "dashboard"}:
            raise UnsupportedAdapter("qorqut-dashboard deployment requires one frozen core and one frozen dashboard artifact")
        if isinstance(context.plan.get("migration"), dict) and context.plan["migration"].get("required"):
            raise UnsupportedAdapter(
                "required migrations are not supported by release-worker; execute a separately reviewed migration lifecycle before deployment"
            )
        _runtime_template(context)
        artifacts = {item["id"]: _verify_staged_artifact(item, context) for item in components}
        auxiliary_artifacts = {
            item["id"]: _verify_staged_artifact(item, context)
            for item in context.plan["components"]
            if item["category"] in {"role", "plugin", "template"}
        }
        service = config["service"]
        current = self.inspect(components[0], context)
        stopped = config.get("allow_stopped_service") is True and current.get("maintenance_stopped") is True
        if not stopped and (not str(current.get("main_pid", "")).isdigit() or int(current["main_pid"]) <= 0):
            raise ReleaseError(f"{components[0]['id']}: service has no stable running PID; inspect before deployment")
        executor = context.executor
        private_path = Path(config["private_config"])
        for evidence in (context.stage_evidence or {}).values():
            expected_private = evidence.get("private_config_fingerprint")
            if expected_private and executor.file_digest(private_path) != expected_private:
                raise ReleaseError("private configuration changed after staging; re-plan and re-stage before switching")
            source_path = evidence.get("package_source_map_source")
            expected_source = evidence.get("package_source_map_source_fingerprint")
            if source_path and expected_source and executor.file_digest(Path(source_path)) != expected_source:
                raise ReleaseError("source package map changed after staging; re-plan and re-stage before switching")
        package_map_source = context.artifact_root / "package-sources.json"
        config_source = context.artifact_root / "private-config.env"
        if not package_map_source.is_file() or not config_source.is_file():
            raise ReleaseError("staged package-source map and private configuration are required")
        _validate_source_map(package_map_source, Path(config["release_root"]).parent)
        map_evidence = next((value for value in (context.stage_evidence or {}).values() if value.get("package_source_map_digest")), None)
        if not map_evidence or file_digest(package_map_source) != map_evidence["package_source_map_digest"]:
            raise ReleaseError("staged package-source map changed after its recorded stage evidence")
        config_evidence = next((value for value in (context.stage_evidence or {}).values() if value.get("release_config_fingerprint")), None)
        if not config_evidence or file_digest(config_source) != config_evidence["release_config_fingerprint"]:
            raise ReleaseError("staged private configuration changed after its recorded stage evidence")
        target = Path(config["release_root"]) / context.plan["release_id"]
        executor.run(["test", "!", "-e", str(target)])
        runtime_source = _expand_placeholders(str(config["runtime_venv_source"]), {"release": str(target)})
        executor.run(["test", "-x", str(Path(runtime_source) / "bin" / "python")])
        socket = config.get("tmux_socket", "bazarlyk")
        sessions_before = executor.run(["tmux", "-L", socket, "list-sessions", "-F", "#{session_id}:#{session_name}"], check=False).splitlines()
        quiescence = config.get("quiescence_command")
        if not isinstance(quiescence, list) or not quiescence:
            raise ReleaseError("installation must declare a supported quiescence_command")
        backup = Path(config["release_root"]) / ".backups" / context.plan["release_id"]
        executor.run(["mkdir", "-p", str(backup)])
        database = str(config["database"])
        backup_db = str(backup / "state.sqlite3")
        backup_code = (
            "import sqlite3, sys; "
            "src=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro', uri=True); "
            "dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()"
        )
        executor.run(["python3", "-I", "-c", backup_code, database, backup_db])
        executor.run(["test", "-s", backup_db])
        private = str(config["private_config"])
        executor.run(["cp", "--preserve=mode", private, str(backup / "private-config.env")])
        executor.run([*map(str, quiescence)])
        executor.run(["mkdir", "-p", str(target)])
        copier = getattr(executor, "copy_tree", None)
        if copier is None:
            raise ReleaseError("production executor cannot transfer a staged artifact")
        for item in components:
            destination = target / ("biz-core" if item["category"] == "core" else "biz-dashboard")
            executor.run(["mkdir", "-p", str(destination)])
            copier(artifacts[item["id"]], destination)
            _verify_destination(executor, artifacts[item["id"]], destination, _stage_record(context, item["id"])["tree_digest"])
        # Package-map paths and the application's fixed biz-template path
        # must resolve inside the assembled release, never local staging.
        for item in context.plan["components"]:
            if item["category"] not in {"role", "plugin", "template"}:
                continue
            destination = target / item["id"]
            executor.run(["mkdir", "-p", str(destination)])
            copier(auxiliary_artifacts[item["id"]], destination)
            _verify_destination(executor, auxiliary_artifacts[item["id"]], destination, _stage_record(context, item["id"])["tree_digest"])
        launcher = [_expand_placeholders(str(item), {"release": str(target)}) for item in config["launcher"]]
        stopper = _launcher_stop_argv(config, target, launcher)
        executor.run(["test", "-x", launcher[0]])
        runtime_target = target / "biz-core" / ".venv"
        executor.run(["mkdir", "-p", str(runtime_target)])
        executor.run(["cp", "-a", runtime_source + "/.", str(runtime_target)])
        runtime_python = runtime_target / "bin" / "python"
        executor.run(["test", "-x", str(runtime_python)])
        install_argv = _runtime_argv(config, "runtime_install_command", target)
        validate_argv = _runtime_argv(config, "runtime_validate_command", target)
        executor.run(install_argv)
        executor.run(validate_argv)
        package_map = target / "package-sources.json"
        config_file = target / "biz-dashboard" / ".env.dashboard"
        writer = getattr(executor, "write_file", None)
        if writer is None:
            raise ReleaseError("production executor cannot write the assembled runtime")
        writer(package_map, package_map_source.read_bytes(), mode=0o644)
        writer(config_file, config_source.read_bytes(), mode=0o600)
        if executor.file_digest(package_map) != map_evidence["package_source_map_digest"]:
            raise ReleaseError("destination package-source map failed integrity verification")
        if executor.file_digest(config_file) != config_evidence["release_config_fingerprint"]:
            raise ReleaseError("destination private configuration failed integrity verification")
        state_dir = _dashboard_state_dir(target)
        override = (
            "[Service]\n"
            "Type=forking\n"
            f"PIDFile={state_dir / 'dashboard.pid'}\n"
            "ExecStart=\n"
            f"ExecStart={shlex.join(launcher)}\n"
            "ExecStop=\n"
            f"ExecStop={shlex.join(stopper)}\n"
            f"WorkingDirectory={target / 'biz-dashboard'}\n"
            f"Environment=BIZ_DASH_CONF={config_file}\n"
            f"Environment=BIZ_DASH_STATE={state_dir}\n"
            f"Environment=QORQUT_PACKAGE_SOURCES={package_map}\n"
            "KillMode=process\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
        ).encode()
        override_path = Path(config.get("override_directory", "/etc/systemd/system/qorqut-dashboard.service.d")) / config.get("override_filename", "zzzzzzz-qorqut-release.conf")
        # The operator owns source, runtime and tmux files. Escalate only the
        # systemd configuration and control commands when explicitly selected.
        systemd_prefix = ["sudo", "-n"] if config.get("sudo_systemd") else []
        if systemd_prefix:
            override_code = (
                "import base64,os,pathlib,sys,tempfile; "
                "p=pathlib.Path(sys.argv[1]); "
                "fd,tmp=tempfile.mkstemp(dir=p.parent); "
                "os.write(fd,base64.b64decode(sys.argv[2])); os.fchmod(fd,0o644); "
                "os.fsync(fd); os.close(fd); os.replace(tmp,p)"
            )
            executor.run([*systemd_prefix, "python3", "-I", "-c", override_code,
                          str(override_path), base64.b64encode(override).decode("ascii")])
        else:
            writer(override_path, override, mode=0o644)
        executor.run([*systemd_prefix, "systemctl", "daemon-reload"])
        effective_unit = _verify_effective_systemd_unit(
            executor,
            service,
            target=target,
            launcher=launcher,
            stopper=stopper,
            package_map=package_map,
            config_file=config_file,
        )
        executor.run([*systemd_prefix, "systemctl", "restart", service])
        executor.run(["systemctl", "is-active", "--quiet", service])
        health_result = self._health(config, config.get("health", {}), executor)
        identity = self.inspect(components[0], context)
        if not self._identity_matches(identity, target, config):
            raise ReleaseError("deployed service identity does not exactly identify the newly assembled release")
        sessions_after = executor.run(["tmux", "-L", socket, "list-sessions", "-F", "#{session_id}:#{session_name}"], check=False).splitlines()
        if not set(sessions_before).issubset(sessions_after):
            raise ReleaseError("a pre-existing tmux session disappeared during deployment")
        return {
            "deployed": True,
            "service": service,
            "host": config["host"],
            "port": int(config["port"]),
            "backup": str(backup),
            "backup_verified": True,
            "health": health_result,
            "runtime_identity": identity,
            "sessions_before": len(sessions_before),
            "sessions_preserved": True,
            "systemd_override": str(override_path),
            "systemd_effective": effective_unit,
            "running_release": str(target),
            "runtime_python": str(runtime_python),
            "runtime_install": install_argv,
            "runtime_validation": validate_argv,
            "database_restore": "never automatic; explicit reviewed recovery only",
        }

    def _health(self, config: dict[str, Any], health: dict[str, Any], executor: Executor) -> dict[str, Any]:
        paths = [str(path) for path in health.get("paths", ["/health/live", "/health/ready"])]
        if not paths:
            return {"checks": []}
        host = health.get("host", config["host"])
        spec = {
            "targets": [[path, f"http://{host}:{int(config['port'])}{path}"] for path in paths],
            "deadline": _HEALTH_DEADLINE_SECONDS,
            "request_timeout": _HEALTH_REQUEST_TIMEOUT_SECONDS,
            "retry_delay": _HEALTH_RETRY_DELAY_SECONDS,
            "body_limit": _HEALTH_BODY_LIMIT,
            "transient_statuses": list(_HEALTH_TRANSIENT_STATUSES),
        }
        # One guarded destination process polls every path within the shared
        # deadline.  Executor failures (remote transport uncertainty, lost
        # lease) propagate untouched; only the probe's own verdict is a health
        # result.
        output = executor.run(["python3", "-I", "-c", _HEALTH_PROBE, json.dumps(spec, separators=(",", ":"))])
        return _health_result(output, paths)

    def recover(self, component: dict[str, Any], context: AdapterContext) -> dict[str, Any]:
        recipe = component.get("recovery_recipe")
        if not isinstance(recipe, dict):
            raise UnsupportedAdapter(f"{component['id']}: production recovery requires an explicit reviewed recovery_recipe")
        receipt = _run_receipt_recipe(
            component,
            context,
            recipe,
            "recovery_recipe",
            operation="recover",
            release=str(Path(context.installation_config["release_root"]) / context.plan["release_id"]),
        )
        return {
            "recovered": True,
            "requires_review": True,
            "receipt_verified": receipt,
            "database_restore": "never automatic; only the reviewed recovery command was executed",
        }

    def reconcile(self, component: dict[str, Any], context: AdapterContext, action: str, before: Any) -> dict[str, Any]:
        if action == "recover":
            recipe = component.get("recovery_recipe")
            if not isinstance(recipe, dict):
                return {"safe_to_retry": False, "observed": before, "reason": "recovery has no receipt-backed postcondition"}
            try:
                receipt = _observe_receipt(
                    component,
                    context,
                    recipe,
                    operation="recover",
                    release=str(Path(context.installation_config["release_root"]) / context.plan["release_id"]),
                )
            except (OSError, ReleaseError) as exc:
                return {"safe_to_retry": False, "observed": before, "reason": f"recovery receipt is not verified: {exc}"}
            return {"already_succeeded": True, "safe_to_retry": False, "receipt_verified": receipt, "reason": "recovery receipt already exists"}
        if action != "deploy":
            return {"safe_to_retry": False, "observed": before, "reason": f"qorqut-dashboard has no {action} reconciliation postcondition"}
        target = Path(context.installation_config["release_root"]) / context.plan["release_id"]
        observed = self.inspect(component, context)
        if not self._identity_matches(observed, target, context.installation_config):
            return {"safe_to_retry": False, "observed": observed, "reason": "live executable, workspace, configuration and package map do not exactly identify the frozen release"}
        try:
            if context.components:
                _runtime_template(context)
                for item in context.components:
                    artifact = _verify_staged_artifact(item, context)
                    destination = target / ("biz-core" if item["category"] == "core" else "biz-dashboard")
                    _verify_destination_files(context.executor, artifact, destination)
                for item in context.plan["components"]:
                    if item["category"] not in {"role", "plugin", "template"}:
                        continue
                    artifact = _verify_staged_artifact(item, context)
                    _verify_destination(
                        context.executor,
                        artifact,
                        target / item["id"],
                        _stage_record(context, item["id"])["tree_digest"],
                    )
                map_evidence = next((value for value in (context.stage_evidence or {}).values() if value.get("package_source_map_digest")), None)
                config_evidence = next((value for value in (context.stage_evidence or {}).values() if value.get("release_config_fingerprint")), None)
                for evidence in (context.stage_evidence or {}).values():
                    expected_private = evidence.get("private_config_fingerprint")
                    if expected_private and context.executor.file_digest(Path(context.installation_config["private_config"])) != expected_private:
                        raise ReleaseError("private configuration changed after staging")
                    source_path = evidence.get("package_source_map_source")
                    expected_source = evidence.get("package_source_map_source_fingerprint")
                    if source_path and expected_source and context.executor.file_digest(Path(source_path)) != expected_source:
                        raise ReleaseError("source package map changed after staging")
                if map_evidence and context.executor.file_digest(target / "package-sources.json") != map_evidence["package_source_map_digest"]:
                    raise ReleaseError("deployed package-source map does not match recorded stage evidence")
                if config_evidence and context.executor.file_digest(target / "biz-dashboard" / ".env.dashboard") != config_evidence["release_config_fingerprint"]:
                    raise ReleaseError("deployed private configuration does not match recorded stage evidence")
            health = self._health(context.installation_config, context.installation_config.get("health", {}), context.executor)
        except (OSError, ReleaseError) as exc:
            return {"safe_to_retry": False, "observed": observed, "reason": f"frozen release identity, artifacts or health are not verified: {exc}"}
        return {"already_succeeded": True, "safe_to_retry": False, "observed": observed, "health": health, "reason": "exact runtime identity and health postconditions verified"}


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
    TemplateAdapter.name: TemplateAdapter,
    QorqutDashboardAdapter.name: QorqutDashboardAdapter,
    IndependentEngineAdapter.name: IndependentEngineAdapter,
}


def adapter_for(component: dict[str, Any]) -> Adapter:
    name = component.get("adapter")
    try:
        return ADAPTER_TYPES[name]()
    except (KeyError, TypeError) as exc:
        raise UnsupportedAdapter(f"{component['id']}: adapter recipe {name!r} is not registered") from exc
