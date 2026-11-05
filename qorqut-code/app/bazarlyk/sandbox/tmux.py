"""PTY mirroring for a verified business container's native tmux sessions.

The dashboard's existing broker owns the host PTY and mirror object. This
adapter owns only the container-side tmux calls. Session creation sends its
environment as JSON on stdin to the fixed in-container boot helper; no secret
is placed in a host argv, Docker ``Config.Env``, or the attach command.
"""

from dataclasses import dataclass
import json
import os
import re
import time
from uuid import uuid4

from qorqut_operations.contracts import DomainError
from .docker import Docker, Ownership


_CONTAINER_ID = re.compile(r"[a-f0-9]{64}")
_SESSION = re.compile(r"[A-Za-z0-9_-]{1,200}")
BOOT_COMMAND = ("/opt/qorqut/venv/bin/python", "-I", "-m", "bazarlyk.sandbox.tmux_session")
_TMUX = "/usr/bin/tmux"
_CLI_PATHS = (("claude-code", "/usr/local/bin/claude"), ("codex", "/usr/local/bin/codex"))
_VERSION_PROBE = r'''
import os, select, subprocess, sys, time
path = sys.argv[1]
status = 127
output = b""
try:
    process = subprocess.Popen((path, "--version"), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    descriptor = process.stdout.fileno()
    deadline = time.monotonic() + 10
    while len(output) < 4096:
        wait = max(0, min(0.5, deadline - time.monotonic()))
        if not select.select([descriptor], [], [], wait)[0]:
            if process.poll() is not None:
                break
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                status = 124
                break
            continue
        chunk = os.read(descriptor, 4096 - len(output))
        if not chunk:
            break
        output += chunk
    else:
        process.kill()
        process.wait()
        status = 125
    if status == 127:
        try:
            status = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            status = 124
except FileNotFoundError:
    status = 1
except PermissionError:
    status = 126
except (OSError, subprocess.SubprocessError):
    status = 125
sys.stdout.buffer.write(output[:4096])
sys.stdout.write("\nQORQUT_STATUS:%d\n" % status)
'''


def _session(value):
    if not isinstance(value, str) or not _SESSION.fullmatch(value):
        raise DomainError("invalid_configuration", "Terminal session identity is invalid")
    return value


@dataclass(frozen=True)
class DockerTmuxBroker:
    """One immutable observed container target backed by the host PTY broker."""

    docker: Docker
    ownership: Ownership
    container_id: str
    generation: int
    mirror: object
    boot_command: tuple[str, ...] = BOOT_COMMAND

    def __post_init__(self):
        if not _CONTAINER_ID.fullmatch(self.container_id):
            raise DomainError("invalid_configuration", "Terminal execution requires the exact observed container ID")
        if type(self.generation) is not int or self.generation < 1:
            raise DomainError("invalid_configuration", "Terminal execution requires a positive environment generation")
        if (not isinstance(self.boot_command, tuple) or not self.boot_command
                or any(not isinstance(value, str) or not value or "\0" in value for value in self.boot_command)
                or self.boot_command != BOOT_COMMAND):
            raise DomainError("invalid_configuration", "Terminal boot helper is invalid")

    def _running(self):
        current = self.docker.owned("container", self.container_id, self.ownership)
        if current is None or not current.get("State", {}).get("Running"):
            raise DomainError("operation_unavailable", "Business terminal environment is not running", retryable=True)
        labels = current.get("Config", {}).get("Labels", {})
        if labels.get("qorqut.role") != "agent" or labels.get("qorqut.generation") != str(self.generation):
            raise DomainError("environment_conflict", "Business terminal container generation is not current")
        return current

    def _key(self, key):
        if not isinstance(key, str) or not key or len(key) > 1024:
            raise DomainError("invalid_configuration", "Terminal mirror identity is invalid")
        # TerminalService uses NUL-separated path/agent identities. This key is
        # an in-memory dictionary key only; it never enters Docker/tmux argv.
        # The generation and exact container ID prevent an old mirror object
        # from being reused after environment replacement.
        return "\0".join(("qorqut-sandbox", self.ownership.business_id, str(self.generation), self.container_id, key))

    def _exec(self, *arguments, input=None):
        self._running()
        return self.docker.exec(self.container_id, self.ownership, *arguments, input=input)

    def tmux_available(self):
        return bool(self._exec(_TMUX, "-V"))

    def installed_versions(self):
        """Return agent CLI versions from this container, never from the host."""
        versions = {}
        for runtime, executable in _CLI_PATHS:
            output = self._exec(BOOT_COMMAND[0], "-I", "-c", _VERSION_PROBE, executable)
            output, status = self._status(output, operation=f"{runtime} version check")
            if status == 0:
                versions[runtime] = output.strip()
            elif status == 1:
                versions[runtime] = ""
            else:
                raise DomainError("environment_unavailable", f"Container {runtime} version check failed", retryable=True)
        return versions

    def verify_interpreter(self, interpreter):
        if interpreter != BOOT_COMMAND[0]:
            raise DomainError("invalid_configuration", "Terminal interpreter is not the pinned container runtime")
        self._exec("test", "-x", interpreter)
        return True

    def binding_probe(self, script, arguments):
        """Trusted adapter code only, on this exact observed container generation."""
        return self._exec(BOOT_COMMAND[0], '-I', '-c', script, *map(str, arguments))

    def reference_probe(self, request, *, timeout=1):
        """Fixed helper on the exact owned instance; never a supplied script/path."""
        from bazarlyk.release_worker.lifetime import local_instance
        expected_instance=local_instance()['target_instance']+':'+self.container_id
        if request['target_instance']!=expected_instance:
            raise DomainError('environment_conflict','Reference probe execution host/instance changed')
        deadline = time.monotonic() + timeout
        current = self.docker.owned('container', self.container_id, self.ownership, timeout=timeout)
        if current is None:
            return self._reference_lifecycle(request, 'destroyed')
        if current.get('Id') != self.container_id:
            raise DomainError('environment_conflict', 'Reference probe instance changed')
        labels = current.get('Config', {}).get('Labels', {})
        if labels.get('qorqut.role') != 'agent' or labels.get('qorqut.generation') != str(self.generation):
            raise DomainError('environment_conflict', 'Reference probe target generation changed')
        if not current.get('State', {}).get('Running'):
            return self._reference_lifecycle(request, 'stopped')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DomainError('environment_unavailable', 'Reference probe budget expired')
        return self.docker.exec(self.container_id, self.ownership,
            '/opt/qorqut/bin/reference-probe', input=json.dumps(request), timeout=remaining)

    def _reference_lifecycle(self, request, state):
        """Exact owned-ID inspection, never a newer container or cached inventory."""
        from bazarlyk.release_worker.lifetime import local_instance
        expected_instance=local_instance()['target_instance']+':'+self.container_id
        return json.dumps({key:request[key] for key in ('version','request_id','nonce','target','target_instance')} | {
            'provenance':'trusted_target_lifecycle','target_lifecycle':state,'boot_id':None,'pid_namespace':None,
            'coverage':[item['id'] for item in request['records']], 'errors':[],
            'processes':[{'id':item['id'],'pid':item['pid'],'started':item['started'],'observed_started':None,
                'state':'exited' if item.get('target_instance')==expected_instance else 'unknown',
                'reason':'verified_exact_container_'+state if item.get('target_instance')==expected_instance else 'historical_target_identity_unproved',
                'slot_evidence':[]} for item in request['records']]})

    def queue_notice(self, session, text):
        return self._exec('/usr/local/bin/codex','queue','--thread',session,'--message',text)

    def session_name(self, slug, agent):
        return self.mirror.session_name(slug, agent)

    def session_prefix(self, slug):
        return self.mirror.session_prefix(slug)

    def attach_cmd(self, session):
        session = _session(session)
        self._running()
        return [*self.docker.command, "exec", "-i", "-t", "--user", "1000:1000", self.container_id,
                _TMUX, "attach-session", "-t", "=" + session]

    @staticmethod
    def _dimensions(cols, rows):
        if type(cols) is not int or type(rows) is not int or not 20 <= cols <= 500 or not 5 <= rows <= 250:
            raise DomainError("validation_error", "Terminal dimensions must be 20-500 columns and 5-250 rows")
        return cols, rows

    @staticmethod
    def _environment(env):
        if env is None:
            return {}
        if not isinstance(env, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                            or not key or "\0" in key or "\0" in value
                                            for key, value in env.items()):
            raise DomainError("validation_error", "Terminal environment must be a string mapping")
        return dict(env)

    def ensure_session(self, session, shell, cwd, *, env=None, cols=120, rows=32):
        session = _session(session)
        if (not isinstance(shell, (list, tuple)) or not shell or
                any(not isinstance(value, str) or not value or "\0" in value for value in shell)):
            raise DomainError("validation_error", "Terminal shell command is invalid")
        if not isinstance(cwd, str) or not os.path.isabs(cwd) or "\0" in cwd:
            raise DomainError("validation_error", "Terminal working directory is invalid")
        cols, rows = self._dimensions(cols, rows)
        payload = json.dumps({"session": session, "shell": list(shell), "cwd": cwd,
                              "env": self._environment(env), "cols": cols, "rows": rows},
                             ensure_ascii=True, separators=(",", ":"))
        self._exec(*self.boot_command, input=payload)

    def get_or_start(self, key, cmd, cwd, *, cols=120, rows=32):
        # ``cmd`` is intentionally ignored. The caller's host command cannot
        # become a container command; attach_cmd() supplies the fixed target.
        self._dimensions(cols, rows)
        self._running()
        return self.mirror.get_or_start(self._key(key), self.attach_cmd(self._session_from_cmd(cmd)), cwd,
                                        cols=cols, rows=rows)

    @staticmethod
    def _session_from_cmd(cmd):
        if (not isinstance(cmd, (list, tuple)) or len(cmd) < 1 or
                not isinstance(cmd[-1], str) or not cmd[-1].startswith("=")):
            raise DomainError("invalid_configuration", "Terminal attach command is invalid")
        return _session(cmd[-1][1:])

    def get(self, key):
        self._running()
        return self.mirror.get(self._key(key))

    def submit_input(self, session, text):
        """Paste to the existing container pane, then send a separate Enter."""
        session = _session(session)
        buffer = 'broadcast-' + uuid4().hex
        try:
            self._exec(_TMUX, 'load-buffer', '-b', buffer, '-', ';',
                       'paste-buffer', '-d', '-r', '-p', '-b', buffer, '-t', '=' + session + ':', input=text)
            time.sleep(0.2)
            self._exec(_TMUX, 'send-keys', '-t', '=' + session + ':', 'Enter')
        except Exception:
            try:
                self._exec(_TMUX, 'delete-buffer', '-b', buffer)
            except Exception:
                pass
            raise

    def stop(self, key):
        mirror_key = self._key(key)
        try:
            self._running()
        except DomainError:
            # The observed container may have been removed during replacement.
            # Its namespaced host mirror is still safe to remove locally.
            return self.mirror.stop(mirror_key)
        return self.mirror.stop(mirror_key)

    @staticmethod
    def _status(output, *, operation):
        lines = output.splitlines()
        marker = "QORQUT_STATUS:"
        marker_positions = [index for index, line in enumerate(lines) if line.startswith(marker)]
        if not marker_positions:
            raise DomainError("environment_unavailable", f"Container tmux {operation} returned an invalid status")
        marker_index = marker_positions[-1]
        value = lines[marker_index][len(marker):]
        try:
            status = int(value.strip())
        except ValueError:
            raise DomainError("environment_unavailable", f"Container tmux {operation} returned an invalid status") from None
        return "\n".join(lines[:marker_index]), status

    def list_sessions(self, prefix):
        output = self._exec("/bin/sh", "-c", f"{_TMUX} list-sessions -F '#{{session_name}}' 2>&1; rc=$?; printf '\\nQORQUT_STATUS:%s\\n' \"$rc\"")
        output, status = self._status(output, operation="listing")
        if status == 1 and not self._absent(output, "listing"):
            raise DomainError("environment_unavailable", "Container tmux listing failed", retryable=True)
        if status not in (0, 1):
            raise DomainError("environment_unavailable", "Container tmux listing failed", retryable=True)
        return [name for name in output.splitlines() if name.startswith(prefix)]

    def resize_session(self, session, rows, cols):
        session = _session(session)
        self._dimensions(cols, rows)
        self._exec(_TMUX, "resize-window", "-t", "=" + session + ":", "-x", str(cols), "-y", str(rows))

    def kill_session(self, session):
        session = _session(session)
        output = self._exec(
            "/bin/sh", "-c", f"{_TMUX} kill-session -t \"=$1\" 2>&1; rc=$?; printf '\\nQORQUT_STATUS:%s\\n' \"$rc\"",
            "qorqut-tmux", session)
        body, status = self._status(output, operation="kill")
        if status == 1 and not self._absent(body, "session"):
            raise DomainError("environment_unavailable", "Container tmux kill failed", retryable=True)
        if status not in (0, 1):
            raise DomainError("environment_unavailable", "Container tmux kill failed", retryable=True)
        return status == 0

    def session_alive(self, session):
        session = _session(session)
        output = self._exec(
            "/bin/sh", "-c", f"{_TMUX} has-session -t \"=$1\" 2>&1; rc=$?; printf '\\nQORQUT_STATUS:%s\\n' \"$rc\"",
            "qorqut-tmux", session)
        body, status = self._status(output, operation="session check")
        if status == 1 and not self._absent(body, "session"):
            raise DomainError("environment_unavailable", "Container tmux session check failed", retryable=True)
        if status not in (0, 1):
            raise DomainError("environment_unavailable", "Container tmux session check failed", retryable=True)
        return status == 0

    @staticmethod
    def _absent(output, operation):
        """Recognize tmux's documented no-server/no-session responses only."""
        value = output.strip().lower()
        if not value:
            return False
        if re.fullmatch(r"no server running on \S+", value):
            return True
        if re.fullmatch(r"failed to connect to server: no such file or directory", value):
            return True
        if re.fullmatch(r"error connecting to \S+ \(no such file or directory\)", value):
            return True
        return operation == "session" and (re.fullmatch(r"can't find session: =?[a-z0-9_-]{1,200}", value)
                                            or re.fullmatch(r"no such session: =?[a-z0-9_-]{1,200}", value))
