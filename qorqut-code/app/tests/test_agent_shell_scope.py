"""The prepared agent shell's claude and codex wrappers change a launch only in the agent's own workspace root.

Everything runs in disposable directories with stand-in claude and codex executables that record their arguments, a
stand-in trust command that records that it ran, and the CLI configuration variables pointed at
a temporary directory (isolated_cli_environment). Nothing here reads or writes a real user's CLI files.
"""
import isolated_cli_environment

import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest

from qorqut import agent_trust
from qorqut.bootstrap.preparation import agent_shell, agent_zshrc

# Arguments the owner might type: spaces, an empty value, shell metacharacters and an option Qorqbot also sets.
USER_ARGUMENTS = ["--resume", "two words", "", "$HOME", "*", "x;y", "--settings", "mine.json"]
# A folder name that would run a command if it were ever left unquoted.
HOSTILE = "agent $(echo hacked >&2) `echo hacked >&2` ;&*?'q\" end"


class Scope:
    """The scenarios, run in one shell. ``SHELL``, ``generate`` and ``command`` say which."""
    SHELL = ""

    def setUp(self):
        if not shutil.which(self.SHELL):
            self.skipTest(f"{self.SHELL} is not installed")
        self.configuration = isolated_cli_environment.isolate(self)
        self.base = Path(tempfile.mkdtemp(prefix="scope root ")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.runtime = self.base / "runtime"
        self.records = self.base / "records"
        bin_dir = self.base / "bin"
        for directory in (self.runtime, self.records, bin_dir):
            directory.mkdir()
        for name in ("claude", "codex"):
            path = bin_dir / name
            path.write_text('#!/bin/sh\n{ for a in "$@"; do printf \'%s\\0\' "$a"; done; } > "$QORQBOT_TEST_RECORDS/'
                            + name + '.$$"\n')
            path.chmod(0o755)
        # Layout: an operations repository holding the agent folder, an unrelated project with its own repository, and a
        # sibling whose name is the workspace's plus a newline.
        self.operations = self.base / "instance" / "operations"
        (self.operations / ".git").mkdir(parents=True)
        self.workspace = self.operations / "agents" / HOSTILE
        self.subdirectory = self.workspace / "work" / "sub dir"
        self.subdirectory.mkdir(parents=True)
        self.project = self.base / "other project"
        (self.project / ".git").mkdir(parents=True)
        (self.project / "src").mkdir()
        self.lookalike = self.workspace.parent / (HOSTILE + "\n")
        self.lookalike.mkdir()
        self.link = self.base / "link to agent"
        self.link.symlink_to(self.workspace)
        self.subdirectory_link = self.base / "link to sub"
        self.subdirectory_link.symlink_to(self.subdirectory)
        (self.workspace / "out").symlink_to(self.project)
        self.launch_settings = self.workspace / ".qorqut" / "claude-launch-settings.json"
        self.shell_file = self.write_shell(self.workspace)

    def write_shell(self, workspace, name="agent-shell"):
        path = self.base / name
        path.write_text(self.generate(shlex.quote(str(self.launch_settings)), shlex.quote(str(agent_trust.marker_path(self.runtime))),
                                      shlex.quote(str(workspace)),
                                      "touch " + shlex.quote(str(self.records / "trust.flag"))))
        return path

    def consent(self, granted):
        agent_trust.consent(self.runtime, {"granted": granted})

    def run_in(self, cwd, command, *, shell_file=None, environ=None):
        """Run ``command`` after loading the wrappers from ``cwd``. Returns (argument lists the CLIs received, process)."""
        for entry in self.records.iterdir():
            entry.unlink()
        environment = {"PATH": str(self.base / "bin") + ":/usr/bin:/bin", "TERM": "dumb", "PWD": str(cwd),
                       "QORQBOT_TEST_RECORDS": str(self.records), **self.configuration, **(environ or {})}
        process = subprocess.run([*self.command, '. "$1"; ' + command, self.SHELL, str(shell_file or self.shell_file)],
                                 cwd=cwd, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        # The hostile folder name is echoed by some error messages; only a command substitution prints a line of just this.
        self.assertNotIn("hacked", process.stderr.splitlines())
        calls = [[part.decode() for part in entry.read_bytes().split(b"\0")[:-1]] for entry in sorted(self.records.glob("*.*"))
                 if entry.name.split(".")[0] in ("claude", "codex")]
        return calls, process

    def launch(self, cwd, client, *arguments, **options):
        calls, _ = self.run_in(cwd, shlex.join([client, *arguments]), **options)
        self.assertEqual(len(calls), 1, calls)
        return calls[0]

    def ran(self, name):
        return (self.records / (name + ".flag")).exists()

    def in_root_claude(self, *arguments):
        return ["--settings", str(self.launch_settings), *arguments]

    def in_root_codex(self, *arguments, bypass):
        return [*(["--dangerously-bypass-hook-trust"] if bypass else []), *arguments]

    # -- the exact workspace root keeps every Qorqbot behaviour --------------------------------------------------------

    def test_the_exact_root_applies_settings_and_the_consented_bypass(self):
        for granted in (False, True):
            with self.subTest(consent=granted):
                self.consent(granted)
                self.assertEqual(self.launch(self.workspace, "claude", *USER_ARGUMENTS), self.in_root_claude(*USER_ARGUMENTS))
                self.assertEqual(self.ran("trust"), granted)
                self.assertEqual(self.launch(self.workspace, "codex", *USER_ARGUMENTS),
                                 self.in_root_codex(*USER_ARGUMENTS, bypass=granted))

    def test_the_trust_refresh_runs_in_the_root_only_with_consent(self):
        self.launch(self.workspace, "claude", "hi")
        self.assertFalse(self.ran("trust"))
        self.consent(True)
        self.launch(self.workspace, "codex", "hi")
        self.assertTrue(self.ran("trust"))

    def test_a_bare_launch_in_the_root_opens_with_the_role_prompt_unless_start_sent_it(self):
        self.consent(True)
        self.assertEqual(self.launch(self.workspace, "claude"), self.in_root_claude("/role"))
        self.assertEqual(self.launch(self.workspace, "codex"), self.in_root_codex("$role", bypass=True))
        sent = {"QORQBOT_ROLE_SENT": "1"}
        self.assertEqual(self.launch(self.workspace, "claude", environ=sent), self.in_root_claude())
        self.assertEqual(self.launch(self.workspace, "codex", environ=sent), self.in_root_codex(bypass=True))
        calls, _ = self.run_in(self.workspace, "QORQBOT_ROLE_SENT=1 claude")
        self.assertEqual(calls, [self.in_root_claude()])
        self.assertEqual(self.launch(self.workspace, "claude", "go"), self.in_root_claude("go"))

    def test_the_root_reached_through_a_symlink_is_still_the_root(self):
        self.consent(True)
        self.assertEqual(self.launch(self.link, "claude", "hi"), self.in_root_claude("hi"))
        self.assertEqual(self.launch(self.link, "codex", "hi"), self.in_root_codex("hi", bypass=True))
        # A symlink into the workspace from anywhere else is the same physical folder.
        elsewhere = self.project / "link into agent"
        elsewhere.symlink_to(self.workspace)
        self.assertEqual(self.launch(elsewhere, "claude", "hi"), self.in_root_claude("hi"))

    def test_a_workspace_known_by_a_symlink_matches_the_physical_root(self):
        self.consent(True)
        by_link = self.write_shell(self.link, "agent-shell-by-link")
        for cwd in (self.workspace, self.link):
            self.assertEqual(self.launch(cwd, "claude", "hi", shell_file=by_link), self.in_root_claude("hi"))
        self.assertEqual(self.launch(self.subdirectory, "claude", "hi", shell_file=by_link), ["hi"])

    # -- every other folder gets the CLI exactly as typed --------------------------------------------------------------

    def outside(self):
        return {"an unrelated repository": self.project, "its subfolder": self.project / "src",
                "an agent subfolder": self.subdirectory, "an agent subfolder through a symlink": self.subdirectory_link,
                "a link out of the workspace": self.workspace / "out",
                "the workspace's parent": self.workspace.parent, "the operations repository": self.operations,
                "a folder named like the workspace plus a newline": self.lookalike, "the filesystem root": Path("/"),
                "the workspace's work folder": self.workspace / "work"}

    def test_other_folders_run_the_cli_with_only_the_owners_arguments(self):
        for granted in (True, False):
            self.consent(granted)
            for where, cwd in self.outside().items():
                for client in ("claude", "codex"):
                    with self.subTest(consent=granted, where=where, client=client):
                        self.assertEqual(self.launch(cwd, client, *USER_ARGUMENTS), USER_ARGUMENTS)
                        self.assertFalse(self.ran("trust"))
                        self.assertFalse(self.ran("trust"))

    def test_other_folders_add_no_role_prompt_to_a_bare_launch(self):
        self.consent(True)
        for where, cwd in self.outside().items():
            for client in ("claude", "codex"):
                with self.subTest(where=where, client=client):
                    self.assertEqual(self.launch(cwd, client), [])
                    self.assertFalse(self.ran("trust"))
                    self.assertEqual(self.launch(cwd, client, environ={"QORQBOT_ROLE_SENT": "1"}), [])

    def test_the_owners_own_settings_option_is_not_displaced_outside_the_root(self):
        self.consent(True)
        self.assertEqual(self.launch(self.project, "claude", "--settings", "mine.json", "-p", "x"),
                         ["--settings", "mine.json", "-p", "x"])
        self.assertEqual(self.launch(self.subdirectory, "codex", "resume", "--last"), ["resume", "--last"])

    def test_the_exit_status_of_the_cli_is_kept_outside_the_root(self):
        failing = self.base / "bin" / "claude"
        failing.write_text("#!/bin/sh\nexit 7\n")
        _, process = self.run_in(self.project, "claude hi; echo \"status=$?\"")
        self.assertIn("status=7", process.stdout)

    def test_the_owners_cd_and_pwd_cannot_move_the_boundary(self):
        self.consent(True)
        hijack = 'cd() { echo hijacked; return 1; }; pwd() { echo ' + shlex.quote(str(self.workspace)) + '; }; '
        calls, process = self.run_in(self.project, hijack + "claude hi")
        self.assertEqual(calls, [["hi"]])
        calls, process = self.run_in(self.workspace, hijack + "claude hi")
        self.assertEqual(calls, [self.in_root_claude("hi")])
        self.assertNotIn("hijacked", process.stdout)

    def test_a_folder_that_cannot_be_determined_runs_the_cli_ordinarily(self):
        self.consent(True)
        # The current folder is removed under the shell; so is the whole workspace in the second run.
        gone = self.subdirectory / "gone"
        gone.mkdir()
        calls, _ = self.run_in(gone, "rmdir " + shlex.quote(str(gone)) + "; claude hi; codex hi")
        self.assertEqual(sorted(calls), [["hi"], ["hi"]])
        self.assertFalse(self.ran("trust"))
        doomed = self.base / "doomed"
        doomed.mkdir()
        shell_file = self.write_shell(doomed, "agent-shell-doomed")
        calls, _ = self.run_in(doomed, "rmdir " + shlex.quote(str(doomed)) + "; claude hi", shell_file=shell_file)
        self.assertEqual(calls, [["hi"]])
        self.assertFalse(self.ran("trust"))

    def test_a_workspace_that_does_not_exist_or_is_not_absolute_never_matches(self):
        self.consent(True)
        for name, workspace in (("missing", self.base / "missing"), ("relative", Path("agent"))):
            with self.subTest(name):
                shell_file = self.write_shell(workspace, "agent-shell-" + name)
                (self.base / "agent").mkdir(exist_ok=True)
                self.assertEqual(self.launch(self.base / "agent", "claude", "hi", shell_file=shell_file), ["hi"])
                self.assertEqual(self.launch(self.base, "codex", "hi", shell_file=shell_file), ["hi"])


class BashScope(Scope, unittest.TestCase):
    SHELL, command = "bash", ["bash", "-c"]
    generate = staticmethod(agent_shell)


class ZshScope(Scope, unittest.TestCase):
    """Skipped where zsh is not installed (it is the macOS default shell)."""
    SHELL, command = "zsh", ["zsh", "-f", "-c"]
    generate = staticmethod(agent_zshrc)


if __name__ == "__main__":
    unittest.main()
