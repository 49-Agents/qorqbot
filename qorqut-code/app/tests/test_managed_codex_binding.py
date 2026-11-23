"""A Codex session driven by a managed host: how the hook, the MCP bridge, the service-side probe and the process probe accept it.

The rule under test is the one that already holds for the managed claude, with the Codex client's shape: a process counts as agent X's
session only when it is the ``codex app-server --listen stdio://`` that agent X's managed host (host record, pid plus start ticks, the
launcher's command line) started, serving the thread the hook names (its writer lock). Everything else keeps the answer it always had.

Pure rules run against a fake process table. The strictness runs against real processes: a stub host with the launcher's argument shape,
and below it a process named ``codex`` (a copy of the Python interpreter) that takes a real ``flock`` on ``thread-writer-locks/<thread>.lock``
exactly as the real app-server does and runs, on request, a command as its child (a "hook"). No model, no network, no real Codex.
"""
import isolated_cli_environment  # before any Qorqbot import: tests never touch the real user's CLI files

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from qorqut_operations.contracts import DomainError

from qorqut.bootstrap import codex_session, native_binding
from qorqut.bootstrap.bridge_binding import BridgeBinding
from qorqut.bootstrap.native_binding import MANAGED_PROBE, NativeBindingAdapter, managed_host
from qorqut.bootstrap.terminal_chat import process_fields
from qorqut.native_chat.probe import SessionProbe

AGENT = "agent-1"
HOST_ARGV = [b"/usr/bin/python3", b"-B", b"-m", b"qorqut.managed_runtime.host", b"--state", b"/runtime/managed-agents/a"]


def ticks(pid):
    return process_fields(pid)[19]


class FakeProcesses:
    """A process table for ``managed_host``: pid -> (state, parent, ticks, comm, argv)."""

    def __init__(self, table):
        self.table = table

    def fields(self, pid):
        state, parent, started, _, _ = self.table[pid]
        return [state, str(parent)] + ["0"] * 17 + [started]

    def comm(self, pid):
        return self.table[pid][3]

    def argv(self, pid):
        return self.table[pid][4]


def process(parent, started="100", comm="bash", argv=(), state="S"):
    return (state, parent, started, comm, list(argv))


class RuntimeWithIdentity:
    """The managed runtime as the binding adapter reaches it: ``identity`` is the service-held, token-verified view of the agent's host."""

    def __init__(self, identity=None, error=None):
        self._identity, self._error, self.asked = identity, error, []
        self.managed_runtime = self   # it also stands for the service runtime that holds it (``managed_of`` finds it there)

    def identity(self, agent_id):
        self.asked.append(agent_id)
        if self._error:
            raise self._error
        return dict(self._identity) if self._identity else None

THREAD = "01a1f4b2-0d13-7680-9196-09c80e40aacf"
OTHER_THREAD = "01a1f4b2-0d13-7680-9196-09c80e40aab0"
APP_SERVER = [b"codex", b"-c", b"check_for_update_on_startup=false", b"app-server", b"--listen", b"stdio://"]
LINUX = sys.platform.startswith("linux")


def host_table(overrides=None):
    table = {500: process(1, "555", "python3", HOST_ARGV), 600: process(500, "601", "codex", APP_SERVER)}
    table.update(overrides or {})
    return FakeProcesses(table)


# ----- the argument vector -------------------------------------------------------------------------------------------------

class AppServerShapeTests(unittest.TestCase):
    CASES = {
        (b"codex", b"app-server", b"--listen", b"stdio://"): True,
        (b"/x/codex", b"-c", b"a=1", b"-c", b"hooks.state={}", b"app-server", b"--listen", b"stdio://", b""): True,
        (b"codex", b"app-server", b"--listen", b"ws://127.0.0.1:1"): False,
        (b"codex", b"app-server"): False,
        (b"codex", b"app-server", b"--listen", b"stdio://", b"--other"): False,
        (b"codex", b"exec", b"app-server", b"--listen", b"stdio://"): False,
        (b"codex", b"queue", b"x", b"app-server", b"--listen", b"stdio://"): False,
        (b"codex",): False,
        (b"app-server", b"--listen", b"stdio://"): False,   # no executable in front of it
        (): False,
    }

    def test_only_the_launchers_vector_is_an_app_server_a_host_can_have(self):
        for argv, expected in self.CASES.items():
            with self.subTest(argv=argv):
                self.assertIs(codex_session.runs_app_server(argv), expected)

    def test_owner_process_is_the_process_part_of_either_owner_form(self):
        self.assertEqual(codex_session.owner_process("12:345"), "12:345")
        self.assertEqual(codex_session.owner_process("12:345:9:2049:77"), "12:345")
        for bad in ("12", "12:3:4", "host:1:2", "12:x", "", None, 5, "1:2:3:4:x"):
            with self.subTest(owner=bad):
                self.assertIsNone(codex_session.owner_process(bad))


# ----- the hook-side filter: managed_host(runtime='codex') ------------------------------------------------------------------

class ManagedCodexHostFilterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name).resolve() / "agent"
        (self.workspace / ".qorqut").mkdir(parents=True)
        self.record = {"host_id": "host:500:555", "host_pid": 500, "host_ticks": "555", "agent_id": AGENT}
        self.write(self.record)

    def write(self, value):
        (self.workspace / ".qorqut/managed-host.json").write_text(json.dumps(value))

    def host(self, processes, pid=600, agent=AGENT, runtime="codex"):
        return managed_host(self.workspace, pid, agent, processes=processes, runtime=runtime)

    def test_the_app_server_that_is_the_child_of_the_recorded_host_is_accepted(self):
        self.assertEqual(self.host(host_table()), {"host_id": "host:500:555", "host_pid": 500, "host_ticks": "555"})

    def test_intermediate_processes_are_allowed_but_not_another_codex(self):
        table = {550: process(500, "560", "env"), 600: process(550, "601", "codex", APP_SERVER)}
        self.assertEqual(self.host(host_table(table))["host_pid"], 500)
        table = {550: process(500, "560", "codex", APP_SERVER), 600: process(550, "601", "codex", APP_SERVER)}
        self.assertIsNone(self.host(host_table(table)))

    def test_a_codex_started_from_inside_the_managed_session_is_refused(self):
        # host -> app-server -> bash -> codex (exec, or its own app-server): the nearest codex ancestor is not a child of the host.
        table = {650: process(600, "651", "bash"), 700: process(650, "701", "codex", [b"codex", b"exec", b"hi"]),
                 710: process(650, "711", "codex", APP_SERVER)}
        processes = host_table(table)
        self.assertIsNotNone(self.host(processes, 600))
        self.assertIsNone(self.host(processes, 700))
        self.assertIsNone(self.host(processes, 710))

    def test_only_the_launchers_command_line_counts(self):
        for name, argv in {"exec": [b"codex", b"exec", b"hi"], "queue": [b"codex", b"queue", b"x"], "terminal client": [b"codex"],
                           "resume": [b"codex", b"resume", b"x"], "websocket server": [b"codex", b"app-server", b"--listen", b"ws://0.0.0.0:1"],
                           "server with extras": APP_SERVER + [b"--x"], "claude style": [b"codex", b"-p"]}.items():
            with self.subTest(name):
                self.assertIsNone(self.host(host_table({600: process(500, "601", "codex", argv)})))

    def test_the_other_clients_process_never_counts(self):
        self.assertIsNone(self.host(host_table({600: process(500, "601", "claude", [b"claude", b"-p"])})))
        self.assertIsNone(self.host(host_table(), runtime="claude-code"))   # a codex under the host is not the managed claude
        claude = host_table({600: process(500, "601", "claude", [b"claude", b"-p"])})
        self.assertIsNotNone(self.host(claude, runtime="claude-code"))
        self.assertIsNone(self.host(claude, runtime="other"))

    def test_a_process_that_is_not_the_hosts_child_is_refused(self):
        table = {800: process(1, "801", "codex", APP_SERVER), 900: process(1500, "901", "codex", APP_SERVER)}
        processes = host_table(table)
        self.assertIsNone(self.host(processes, 800))
        self.assertIsNone(self.host(processes, 900))   # a parent that is not even in the table
        self.assertIsNone(self.host(processes, 601))

    def test_a_host_for_another_agent_a_reused_pid_and_a_stale_record_are_refused(self):
        self.assertIsNone(self.host(host_table(), agent="someone-else"))
        self.write({**self.record, "agent_id": "someone-else"})
        self.assertIsNone(self.host(host_table()))
        self.write(self.record)
        cases = {
            "recycled host pid (other start ticks)": {500: process(1, "556", "python3", HOST_ARGV)},
            "zombie host": {500: process(1, "555", "python3", HOST_ARGV, state="Z")},
            "pid now runs something else": {500: process(1, "555", "python3", [b"python3", b"-m", b"http.server"])},
            "zombie client": {600: process(500, "601", "codex", APP_SERVER, state="Z")},
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assertIsNone(self.host(host_table(change)))
        for name, value in {"stale ticks": {**self.record, "host_ticks": "999"}, "other pid": {**self.record, "host_pid": 501},
                            "no record fields": {"agent_id": AGENT}, "text": "x"}.items():
            with self.subTest(name):
                self.write(value)
                self.assertIsNone(self.host(host_table()))
        (self.workspace / ".qorqut/managed-host.json").unlink()
        self.assertIsNone(self.host(host_table()))


# ----- the hook's attestation, with the recognition mocked ---------------------------------------------------------------

class AttestCodexTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name).resolve() / "agent"
        (self.workspace / ".qorqut").mkdir(parents=True)
        self.registration = {"agent_id": AGENT, "registration_id": "reg-1", "workspace": str(self.workspace)}
        self.hook = {"session_id": THREAD, "cwd": str(self.workspace), "hook_event_name": "SessionStart", "source": "startup"}
        patches = (mock.patch.object(native_binding, "load_registration", return_value=self.registration),
                   mock.patch.object(native_binding, "write_json_private"), mock.patch.object(native_binding, "local_instance", return_value={}))
        self.written = patches[1].start()
        patches[0].start()
        patches[2].start()
        self.addCleanup(mock.patch.stopall)

    def attest(self):
        return native_binding.attest(self.hook, "registration.json", "codex")

    def test_the_hosts_app_server_is_bound_by_its_process_alone_and_the_host_is_recorded(self):
        host = {"host_id": "host:5:6", "host_pid": 5, "host_ticks": "6"}
        with mock.patch.object(codex_session, "hosted_session_process", return_value=("42:777", host)):
            value = self.attest()
        self.assertEqual((value["owner"], value["pid"], value["started"], value["runtime"], value["managed_host"]),
                         ("42:777", 42, "777", "codex", host))
        self.assertEqual(self.written.call_args.args[1]["managed_host"], host)

    def test_nothing_the_hook_can_identify_is_a_refusal(self):
        with mock.patch.object(codex_session, "hosted_session_process", return_value=None):
            with self.assertRaises(DomainError) as refused:
                self.attest()
        self.assertEqual(refused.exception.code, "inbox_delivery_unavailable")
        self.written.assert_not_called()

    def test_a_subagent_hook_is_refused_even_under_the_hosts_app_server(self):
        for key in ("agent_id", "parent_session_id"):
            hook = {**self.hook, key: "sub"}
            with mock.patch.object(codex_session, "hosted_session_process", return_value=("42:777", {"host_id": "h"})) as hosted:
                with self.subTest(key=key), self.assertRaises(DomainError):
                    native_binding.attest(hook, "registration.json", "codex")
            hosted.assert_not_called()


# ----- real processes -------------------------------------------------------------------------------------------------------

FAKE_CODEX = ("import fcntl,json,os,subprocess,sys,time\n"
              "home=os.environ['CODEX_HOME'];lock=os.path.join(home,'thread-writer-locks');os.makedirs(lock,exist_ok=True)\n"
              "fd=os.open(os.path.join(lock,os.environ['FAKE_THREAD']+'.lock'),os.O_CREAT|os.O_RDWR,0o600);fcntl.flock(fd,fcntl.LOCK_EX)\n"
              "cmd,out=os.environ['CMDFILE'],os.environ['OUTFILE']\n"
              "env={k:v for k,v in os.environ.items() if k not in('PYTHONHOME','FAKE_THREAD')}\n"
              "while True:\n"
              " if os.path.exists(cmd):\n"
              "  argv=json.load(open(cmd));os.unlink(cmd)\n"
              "  r=subprocess.run(argv,capture_output=True,text=True,timeout=40,env=env)\n"
              "  json.dump({'out':r.stdout,'err':r.stderr[-3000:],'code':r.returncode},open(out+'.tmp','w'));os.replace(out+'.tmp',out)\n"
              " time.sleep(0.03)\n")
# A host that has the launcher's argument shape and starts the fake app-server the way the Codex driver does.
STUB_CODEX_HOST = ("import os,subprocess,sys,time\n"
                   "e=dict(os.environ);e['PYTHONHOME']=os.environ['FAKE_HOME']\n"
                   "import json\n"
                   "child=subprocess.Popen([os.environ['FAKE_CODEX'],'-c',os.environ['FAKE_SCRIPT'],*json.loads(os.environ['FAKE_TAIL'])],env=e)\n"
                   "print(child.pid,flush=True)\n"
                   "time.sleep(90)\n")
# What a hook is: a child of the app-server (the fake runs it for the test), here running one of the attestation entry points.
HOOK = ("import json,sys\n"
        "from pathlib import Path\n"
        "from qorqut.bootstrap import native_binding,codex_session\n"
        "registration=json.loads(sys.argv[2]);what=sys.argv[1]\n"
        "native_binding.load_registration=lambda p:registration\n"
        "native_binding.local_instance=lambda:{}\n"
        "session=sys.argv[3]\n"
        "if what=='hosted':\n"
        "    print(json.dumps(codex_session.hosted_session_process(session,registration['workspace'],registration['agent_id'])))\n"
        "else:\n"
        "    try:\n"
        "        print(json.dumps(native_binding.attest({'session_id':session,'cwd':registration['workspace'],'hook_event_name':'SessionStart'},'r.json','codex')))\n"
        "    except Exception as error:\n"
        "        print(json.dumps({'refused':getattr(error,'code',type(error).__name__)}))\n")


@unittest.skipUnless(LINUX, "the writer lock is a Linux file lock; the process table is /proc")
class CodexRealCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.workspace = self.root / "agent"
        (self.workspace / ".qorqut").mkdir(parents=True)
        self.codex = self.root / "bin" / "codex"
        self.codex.parent.mkdir()
        shutil.copy(os.path.realpath(sys.executable), self.codex)   # a process whose name is "codex" and which can hold a real lock
        self.home = self.root / "codex-home"
        self.registration = {"agent_id": AGENT, "registration_id": "reg-1", "workspace": str(self.workspace)}
        stub = self.root / "stub/qorqut/managed_runtime"
        stub.mkdir(parents=True)
        for folder in (stub.parent, stub):
            (folder / "__init__.py").write_text("")
        (stub / "host.py").write_text(STUB_CODEX_HOST)
        self.counter = 0

    # -- processes ---------------------------------------------------------------------------------------------------
    def spawn(self, command, **environment):
        process_ = subprocess.Popen(command, stdout=subprocess.PIPE, text=True, start_new_session=True, cwd=self.root,
                                    env={**os.environ, **environment})
        self.addCleanup(self.reap, process_)
        return process_

    @staticmethod
    def reap(process_):
        import signal
        try:
            os.killpg(process_.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process_.wait()
        process_.stdout.close()

    def app_server_environment(self, thread, tail=("app-server", "--listen", "stdio://")):
        self.counter += 1
        return dict(CODEX_HOME=str(self.home), FAKE_THREAD=thread, FAKE_TAIL=json.dumps(list(tail)), FAKE_SCRIPT=FAKE_CODEX, FAKE_CODEX=str(self.codex),
                    FAKE_HOME=sys.base_prefix, CMDFILE=str(self.root / f"cmd{self.counter}"), OUTFILE=str(self.root / f"out{self.counter}"))

    def host_with_app_server(self, thread=THREAD, tail=("app-server", "--listen", "stdio://")):
        """``(host, app_server_pid, environment)``: a host of the launcher's argument shape and the fake app-server it started (``tail``
        is what follows the executable's own options: anything but the default is a client the Codex driver never starts)."""
        environment = self.app_server_environment(thread, tail)
        host = self.spawn([sys.executable, "-B", "-m", "qorqut.managed_runtime.host", "--state", str(self.root / "state")],
                          PYTHONPATH=str(self.root / "stub"), **environment)
        pid = int(host.stdout.readline())
        self.wait_for(lambda: self.lock_held(pid, thread), "the app-server never took its writer lock")
        return host, pid, environment

    def shared_app_server(self, thread=THREAD):
        """An app-server with no host above it (a desktop server) that holds the thread's lock."""
        environment = self.app_server_environment(thread)
        server = self.spawn([str(self.codex), "-c", FAKE_CODEX, "app-server", "--listen", "stdio://"],
                            **{**environment, "PYTHONHOME": sys.base_prefix})
        self.wait_for(lambda: self.lock_held(server.pid, thread), "the app-server never took its writer lock")
        return server, server.pid, environment

    def lock_held(self, pid, thread):
        try:
            name = os.path.join(str(self.home), "thread-writer-locks", thread + ".lock")
            return any(os.readlink(f"/proc/{pid}/fd/{fd}") == name for fd in os.listdir(f"/proc/{pid}/fd"))
        except OSError:
            return False

    @staticmethod
    def wait_for(condition, message, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.03)
        raise AssertionError(message)

    def record(self, pid, **extra):
        value = {"host_id": "h", "host_pid": pid, "host_ticks": ticks(pid), "agent_id": AGENT, **extra}
        (self.workspace / ".qorqut/managed-host.json").write_text(json.dumps(value))

    def run_as_child(self, environment, *arguments, python=None, extra=None):
        """Run ``arguments`` as a child of the fake app-server and return its output (the app-server runs it, like a hook; ``env``
        execs in place, so the parent of the python process is the app-server)."""
        out = Path(environment["OUTFILE"])
        out.unlink(missing_ok=True)
        variables = {"PYTHONPATH": os.pathsep.join(p for p in sys.path if p), **(extra or {})}
        Path(environment["CMDFILE"]).write_text(json.dumps(
            ["/usr/bin/env", *[f"{key}={value}" for key, value in variables.items()], python or sys.executable, *arguments]))
        self.wait_for(out.exists, "the app-server did not run the command")
        result = json.loads(out.read_text())
        if result["code"] != 0:
            self.fail(result["err"])
        return result["out"]

    def hook(self, environment, what, thread=THREAD):
        output = self.run_as_child(environment, "-B", "-c", HOOK, what, json.dumps(self.registration), thread)
        return json.loads(output.splitlines()[-1])


@unittest.skipUnless(LINUX, "the writer lock is a Linux file lock; the process table is /proc")
class HostedHookTests(CodexRealCase):
    def test_the_hook_under_the_hosts_app_server_is_recognised_with_the_process_identity(self):
        host, pid, environment = self.host_with_app_server()
        self.record(host.pid)
        identity, found = self.hook(environment, "hosted")
        self.assertEqual(identity, f"{pid}:{ticks(pid)}")
        self.assertEqual((found["host_pid"], found["host_ticks"]), (host.pid, ticks(host.pid)))

    def test_attest_writes_a_two_part_owner_the_host_and_the_codex_runtime(self):
        host, pid, environment = self.host_with_app_server()
        self.record(host.pid)
        value = self.hook(environment, "attest")
        self.assertEqual((value["owner"], value["pid"], value["started"], value["runtime"], value["session_id"]),
                         (f"{pid}:{ticks(pid)}", pid, ticks(pid), "codex", THREAD))
        self.assertEqual(value["managed_host"]["host_pid"], host.pid)
        written = json.loads((self.workspace / ".qorqut/native-binding.json").read_text())
        self.assertEqual(written["owner"], f"{pid}:{ticks(pid)}")

    def test_a_host_for_another_agent_a_stale_record_or_no_record_binds_nothing_new(self):
        host, pid, environment = self.host_with_app_server()
        record = self.workspace / ".qorqut/managed-host.json"
        for name, extra in {"another agent": {"agent_id": "someone-else"}, "recycled host pid": {"host_ticks": str(int(ticks(host.pid)) + 1)},
                            "record names another process": {"host_pid": pid, "host_ticks": ticks(pid)}}.items():
            with self.subTest(name):
                self.record(host.pid, **extra)
                self.assertIsNone(self.hook(environment, "hosted"))
        record.unlink()
        self.assertIsNone(self.hook(environment, "hosted"))

    def test_a_thread_the_app_server_does_not_hold_is_not_its_session(self):
        host, pid, environment = self.host_with_app_server()
        self.record(host.pid)
        self.assertIsNone(self.hook(environment, "hosted", thread=OTHER_THREAD))
        self.assertEqual(self.hook(environment, "attest", thread=OTHER_THREAD), {"refused": "inbox_delivery_unavailable"})

    def test_an_app_server_without_the_agents_host_above_it_is_never_bound(self):
        server, pid, environment = self.shared_app_server()
        self.record(pid)   # a record that names the server itself as the "host" proves nothing: it does not run the host module
        self.assertIsNone(self.hook(environment, "hosted"))
        self.assertEqual(self.hook(environment, "attest"), {"refused": "inbox_delivery_unavailable"})
        self.assertFalse((self.workspace / ".qorqut/native-binding.json").exists())

    def test_a_codex_the_agent_starts_from_its_shell_is_not_the_hosts_client(self):
        host, pid, environment = self.host_with_app_server()
        self.record(host.pid)
        # The nested codex runs the hook as ITS child (so it is the hook's nearest codex ancestor).
        check = ("import json,os\nfrom qorqut.bootstrap import codex_session\n"
                 "print(json.dumps(codex_session.hosted_session_process(os.environ['T'],os.environ['W'],os.environ['A'])))\n")
        nested = ("import os,subprocess,sys\nenv={k:v for k,v in os.environ.items() if k!='PYTHONHOME'}\n"
                  "r=subprocess.run([os.environ['REAL_PYTHON'],'-B','-c',os.environ['CHECK']],capture_output=True,text=True,env=env)\n"
                  "print(r.stdout if r.returncode==0 else 'FAILED '+r.stderr[-800:])\n")
        extra = {"PYTHONHOME": sys.base_prefix, "T": THREAD, "W": str(self.workspace), "A": AGENT, "REAL_PYTHON": sys.executable,
                 "CHECK": check}
        # app-server -> codex shaped like an exec client -> hook, and -> a second app-server: the nearest codex is not a child of the host.
        for tail in (["exec"], ["app-server", "--listen", "stdio://"], []):
            with self.subTest(tail=tail):
                output = self.run_as_child(environment, "-c", nested, *tail, python=str(self.codex), extra=extra)
                try:
                    found = json.loads(output.strip().splitlines()[-1])
                except (ValueError, IndexError):
                    self.fail(f"the nested hook did not answer: {output[-900:]!r}")
                self.assertIsNone(found)

    def test_a_client_the_driver_never_starts_is_not_recognised_even_below_the_host_and_holding_the_lock(self):
        for number, tail in enumerate((["exec"], ["app-server", "--listen", "ws://127.0.0.1:1"], ["resume", THREAD])):
            with self.subTest(tail=tail):
                thread = f"01a1f4b2-0d13-7680-9196-09c80e40a{number:03d}"   # one holder per lock: a second would wait for the first
                host, pid, environment = self.host_with_app_server(thread, tail=tail)
                self.record(host.pid)
                self.assertIsNone(self.hook(environment, "hosted", thread))
                self.assertIsNone(managed_host(self.workspace, pid, AGENT, runtime="codex"))


@unittest.skipUnless(LINUX, "the writer lock is a Linux file lock; the process table is /proc")
class ManagedCodexProbeScriptTests(CodexRealCase):
    """``MANAGED_PROBE`` and ``verify_managed`` for a Codex client."""

    def observation(self, pid, **extra):
        value = {"agent_id": AGENT, "registration_id": "reg-1", "session_id": THREAD, "runtime": "codex",
                 "workspace": str(self.workspace), "pid": pid, "started": ticks(pid), "owner": f"{pid}:{ticks(pid)}", **extra}
        (self.workspace / ".qorqut/native-binding.json").write_text(json.dumps(value))
        return value

    def script(self, client, host, *, runtime="codex", session=THREAD, client_ticks=None, host_ticks=None):
        return subprocess.check_output(
            [sys.executable, "-I", "-c", MANAGED_PROBE, str(self.workspace), session, AGENT, "reg-1", str(client),
             client_ticks or ticks(client), str(host), host_ticks or ticks(host), runtime], text=True, timeout=20, stderr=subprocess.DEVNULL)

    def refuses(self, *arguments, **keywords):
        with self.assertRaises(subprocess.CalledProcessError):
            self.script(*arguments, **keywords)

    def test_the_script_accepts_the_hosts_app_server_the_service_names(self):
        host, pid, _ = self.host_with_app_server()
        self.record(host.pid)
        self.observation(pid)
        self.assertEqual(json.loads(self.script(pid, host.pid))["pid"], pid)

    def test_the_script_refuses_whatever_disagrees(self):
        host, pid, _ = self.host_with_app_server()
        other_host, other_pid, _ = self.host_with_app_server(OTHER_THREAD)
        shared, shared_pid, _ = self.shared_app_server("01a1f4b2-0d13-7680-9196-09c80e40aab1")
        execlike, exec_pid, _ = self.host_with_app_server("01a1f4b2-0d13-7680-9196-09c80e40aab2", tail=("exec",))
        self.record(host.pid)
        stale = str(int(ticks(host.pid)) + 1)
        cases = {
            "service names another app-server": dict(target=pid, client=other_pid, host=host.pid),
            "service names another host": dict(target=pid, client=pid, host=other_host.pid),
            "recycled host in the identity": dict(target=pid, client=pid, host=host.pid, host_ticks=stale),
            "recycled client in the identity": dict(target=pid, client=pid, host=host.pid, client_ticks=str(int(ticks(pid)) + 1)),
            "the host does not hold this thread": dict(target=pid, client=pid, host=host.pid, session=OTHER_THREAD),
            "the identity says Claude": dict(target=pid, client=pid, host=host.pid, runtime="claude-code"),
            "a client that is not the launcher's app-server": dict(target=exec_pid, client=exec_pid, host=execlike.pid,
                                                                       session="01a1f4b2-0d13-7680-9196-09c80e40aab2"),
            "an app-server with no host between": dict(target=shared_pid, client=shared_pid, host=host.pid, session="01a1f4b2-0d13-7680-9196-09c80e40aab1"),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.observation(case["target"], session_id=case.get("session", THREAD))
                self.refuses(case["client"], case["host"], runtime=case.get("runtime", "codex"), session=case.get("session", THREAD),
                             client_ticks=case.get("client_ticks"), host_ticks=case.get("host_ticks"))
        self.observation(pid, runtime="claude-code")   # a marker that says another client
        self.refuses(pid, host.pid)
        self.observation(pid)
        self.record(host.pid, agent_id="someone-else")
        self.refuses(pid, host.pid)

    def test_a_claude_probe_without_the_ninth_argument_is_unchanged(self):
        host, pid, _ = self.host_with_app_server()
        self.record(host.pid)
        self.observation(pid)
        # No runtime argument: the Claude rule, which a ``codex`` is not.
        with self.assertRaises(subprocess.CalledProcessError):
            subprocess.check_output([sys.executable, "-I", "-c", MANAGED_PROBE, str(self.workspace), THREAD, AGENT, "reg-1", str(pid),
                                     ticks(pid), str(host.pid), ticks(host.pid)], text=True, timeout=20, stderr=subprocess.DEVNULL)

    def adapter(self, runtime):
        run = staticmethod(lambda script, arguments: subprocess.check_output(
            [sys.executable, "-I", "-c", script, *map(str, arguments)], text=True, timeout=20, stderr=subprocess.DEVNULL))
        patcher = mock.patch.object(NativeBindingAdapter, "run", run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return NativeBindingAdapter(runtime), {"workspace": str(self.workspace)}

    @staticmethod
    def identity(host_pid, client_pid, provider="codex", **extra):
        return {"agent_id": AGENT, "provider": provider, "conversation_id": THREAD, "pid": client_pid, "started": ticks(client_pid),
                "owner": f"host:{host_pid}:{ticks(host_pid)}", "host_pid": host_pid, "host_started": ticks(host_pid), "instance": "inst-1",
                "state": "ready", "transcript_path": None, **extra}

    def verify_managed(self, identity):
        adapter, profile = self.adapter(RuntimeWithIdentity(identity))
        return adapter.verify_managed(profile, AGENT, "reg-1", THREAD)

    def test_the_service_binds_the_process_its_identity_names_with_a_two_part_owner(self):
        host, pid, _ = self.host_with_app_server()
        self.record(host.pid)
        self.observation(pid, host="box", target="local")
        value = self.verify_managed(self.identity(host.pid, pid))
        self.assertEqual((value["pid"], value["started"], value["owner"], value["runtime"]), (pid, ticks(pid), f"{pid}:{ticks(pid)}", "codex"))
        self.assertEqual(value["managed_host"], {"host_id": "inst-1", "host_pid": host.pid, "host_ticks": ticks(host.pid)})

    def test_the_service_refuses_an_identity_for_another_conversation_a_claude_identity_and_a_forged_marker(self):
        host, pid, _ = self.host_with_app_server()
        self.record(host.pid)
        self.observation(pid)
        self.assertIsNone(self.verify_managed(self.identity(host.pid, pid, conversation_id=OTHER_THREAD)))   # a subagent's thread
        self.assertIsNone(self.verify_managed(self.identity(host.pid, pid, provider="claude")))
        other_host, other_pid, _ = self.host_with_app_server(OTHER_THREAD)
        self.assertIsNone(self.verify_managed(self.identity(host.pid, other_pid)))
        self.assertIsNone(self.verify_managed(self.identity(other_host.pid, pid)))
        self.assertIsNotNone(self.verify_managed(self.identity(host.pid, pid)))


@unittest.skipUnless(LINUX, "the writer lock is a Linux file lock; the process table is /proc")
class ManagedCodexBridgeTests(CodexRealCase):
    def bridge(self, target, **keywords):
        return BridgeBinding(self.registration, ancestor=lambda runtime: ({"pid": target, "started": ticks(target)} if runtime == "codex" else None),
                             **keywords)

    def test_the_bridge_binds_the_hosts_app_server_and_nothing_else(self):
        host, pid, _ = self.host_with_app_server()
        self.record(host.pid)
        shared, shared_pid, _ = self.shared_app_server("01a1f4b2-0d13-7680-9196-09c80e40aab1")
        self.assertEqual(self.bridge(pid).parent, ("codex", f"{pid}:{ticks(pid)}"))
        self.assertIsNone(self.bridge(shared_pid).parent)
        self.assertEqual(self.bridge(shared_pid).headers(), {"X-Qorqut-Binding-Reason": "shared_app_server_without_thread_provenance"})
        (self.workspace / ".qorqut/managed-host.json").unlink()
        unbound = self.bridge(pid)
        self.assertIsNone(unbound.parent)
        self.assertEqual(unbound.headers(), {"X-Qorqut-Binding-Reason": "shared_app_server_without_thread_provenance"})
        self.record(host.pid, agent_id="someone-else")
        self.assertIsNone(self.bridge(pid).parent)

    def test_a_bound_bridge_sends_the_session_and_generation_the_hook_and_the_service_recorded(self):
        host, pid, _ = self.host_with_app_server()
        self.record(host.pid)
        owner = f"{pid}:{ticks(pid)}"
        (self.workspace / ".qorqut/native-binding.json").write_text(json.dumps(
            {"registration_id": "reg-1", "session_id": THREAD, "owner": owner, "runtime": "codex"}))
        (self.workspace / ".qorqut/session-binding.json").write_text(json.dumps(
            {"registration_id": "reg-1", "session_id": THREAD, "owner": owner, "runtime": "codex", "generation": 7}))
        self.assertEqual(self.bridge(pid).headers(), {"X-Qorqut-Native-Session": THREAD, "X-Qorqut-Binding-Generation": "7"})

    def test_a_codex_that_runs_exec_or_queue_is_never_bound(self):
        host, pid, _ = self.host_with_app_server()
        self.record(host.pid)
        proc = self.root / "proc" / "42"
        proc.mkdir(parents=True)
        for argv in ([b"codex", b"exec"], [b"codex", b"queue"], [b"codex", b"app-server", b"--listen", b"ws://x"]):
            (proc / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
            with self.subTest(argv=argv), mock.patch("qorqut.process_info.cmdline", return_value=[a.decode() for a in argv]):
                self.assertIsNone(BridgeBinding(self.registration, ancestor=lambda r: {"pid": 42, "started": "9"} if r == "codex" else None,
                                                proc=proc.parent).parent)


# ----- the service-side process probe --------------------------------------------------------------------------------------------

class FakeManagedCodex:
    """The managed runtime as the probe reads it, for a Codex host."""

    def __init__(self, host_pid, host_ticks, client=None):
        self.owner = f"host:{host_pid}:{host_ticks}"
        self.client = client
        self.verify_result = {"transport": "managed", "owner": self.owner}
        self.state = {"phase": "idle", "activity": None, "pending": []}

    def identity(self, agent_id):
        if self.client is None:
            return None
        return {"agent_id": agent_id, "provider": "codex", "pid": self.client[0], "started": self.client[1], "owner": self.owner,
                "conversation_id": THREAD}

    def verify(self, profile, marker):
        return self.verify_result

    def snapshot(self, agent_id):
        return dict(self.state)


@unittest.skipUnless(LINUX, "the writer lock is a Linux file lock; the process table is /proc")
class ManagedCodexProbeTests(CodexRealCase):
    def probe(self, managed):
        return SessionProbe(managed=managed)

    def profile(self):
        return {"agent_id": AGENT, "workspace": str(self.workspace), "profile_json": json.dumps({"runtime": "codex"})}

    @staticmethod
    def marker(pid, session=THREAD):
        return {"agent_id": AGENT, "session_id": session, "runtime": "codex", "pid": pid, "started": ticks(pid)}

    def test_the_hosts_app_server_is_managed_before_the_shared_server_rule_is_asked(self):
        host, pid, _ = self.host_with_app_server()
        managed = FakeManagedCodex(host.pid, ticks(host.pid), (pid, ticks(pid)))
        self.assertEqual(self.probe(managed)._verify_process(self.profile(), self.marker(pid)),
                         {"transport": "managed", "owner": managed.owner})

    def test_every_disagreement_is_a_refusal(self):
        host, pid, _ = self.host_with_app_server()
        other, other_pid, _ = self.host_with_app_server(OTHER_THREAD)
        managed = FakeManagedCodex(host.pid, ticks(host.pid), (pid, ticks(pid)))
        probe = self.probe(managed)
        marker = self.marker(pid)
        # the marker's process is another app-server than the one the host reports (a thread the host does not serve)
        self.assertIsNone(probe._verify_process(self.profile(), self.marker(other_pid, OTHER_THREAD)))
        # the marker names the host's app-server but another thread (a subagent's): the app-server does not hold it
        self.assertIsNone(probe._verify_process(self.profile(), self.marker(pid, OTHER_THREAD)))
        # the host reports a different client
        managed.client = (other_pid, ticks(other_pid))
        self.assertIsNone(probe._verify_process(self.profile(), marker))
        managed.client = (pid, ticks(pid))
        # the managed runtime does not vouch for it, or knows no host
        managed.verify_result = None
        self.assertIsNone(probe._verify_process(self.profile(), marker))
        managed.verify_result = {"transport": "managed", "owner": managed.owner}
        managed.client = None
        self.assertIsNone(probe._verify_process(self.profile(), marker))
        managed.client = (pid, ticks(pid))
        # a probe with no managed runtime never reaches the managed rule
        self.assertIsNone(self.probe(None)._verify_process(self.profile(), marker))
        # a recycled process: the marker's start ticks are not the process's
        self.assertIsNone(probe._verify_process(self.profile(), dict(marker, started=str(int(marker["started"]) + 1))))

    def test_a_shared_app_server_is_never_bound(self):
        server, pid, _ = self.shared_app_server()
        managed = FakeManagedCodex(1, "1", (pid, ticks(pid)))
        managed.verify_result = None
        self.assertIsNone(self.probe(managed)._verify_process(self.profile(), self.marker(pid)))   # no pane displays it

    def test_the_managed_codex_is_working_by_its_activity_file_whatever_form_the_hook_wrote_its_owner_in(self):
        pid, started = 4242, "99"
        marker = {"agent_id": AGENT, "session_id": THREAD, "runtime": "codex", "pid": pid, "started": started}
        activity = self.workspace / ".qorqut/activity"
        activity.mkdir(parents=True)
        for owner, expected in ((f"{pid}:{started}", True), (f"{pid}:{started}:9:2049:77", True), (f"{pid}:100:9:2049:77", False),
                                (f"{pid + 1}:{started}", False), ("host:1:2", False), (None, False)):
            (activity / f"{THREAD}.json").write_text(json.dumps({"agent_id": AGENT, "session_id": THREAD, "runtime": "codex", "owner": owner,
                                                                 "state": "running"}))
            with self.subTest(owner=owner):
                self.assertIs(SessionProbe._working(self.profile(), marker), expected)


if __name__ == "__main__":
    unittest.main()
