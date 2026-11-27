"""Programs: launchers for our programs, and the command strings the clients and the agents are given."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from qorqut_system import programs


class Quoting(unittest.TestCase):
    def test_one_word_is_quoted_for_a_posix_shell(self):
        self.assertEqual(programs.quote("plain"), "plain")
        self.assertEqual(programs.quote("a b"), "'a b'")

    def test_one_command_is_quoted_for_a_posix_shell(self):
        self.assertEqual(programs.command(["prog", "a b"]), "prog 'a b'")

    def test_a_hook_is_quoted_for_the_shell_of_its_client(self):
        claude = programs.hook_command("claude-code", ["/w/.qorqut/codex-hook", "bootstrap"])
        self.assertEqual(claude, "/w/.qorqut/codex-hook bootstrap")
        codex = programs.hook_command("codex", ["/w with space/.qorqut/codex-hook", "bootstrap"])
        if sys.platform == "win32":
            self.assertEqual(codex, "& '/w with space/.qorqut/codex-hook' bootstrap")
        else:
            self.assertEqual(codex, "'/w with space/.qorqut/codex-hook' bootstrap")

    def test_an_unknown_client_is_refused(self):
        with self.assertRaises(ValueError):
            programs.hook_command("someone-else", ["prog"])

    def test_the_helper_an_agent_types_is_its_quoted_path(self):
        self.assertEqual(programs.helper_command("/w/.qorqut/agentic"), "/w/.qorqut/agentic")
        self.assertEqual(programs.helper_command("/w with space/.qorqut/agentic"), "'/w with space/.qorqut/agentic'")


class PowerShellQuoting(unittest.TestCase):
    def test_a_plain_program_is_written_as_it_is(self):
        self.assertEqual(programs.powershell_command(["C:\\x\\python.exe", "-m", "mod"]),
                         "C:\\x\\python.exe -m mod")

    def test_a_program_path_with_a_space_needs_the_call_operator(self):
        self.assertEqual(programs.powershell_command(["C:\\Program Files\\python.exe", "-m", "mod"]),
                         "& 'C:\\Program Files\\python.exe' -m mod")

    def test_a_quote_inside_a_word_is_doubled(self):
        self.assertEqual(programs.powershell_command(["prog", "it's"]), "prog 'it''s'")

    def test_no_parts_make_no_command(self):
        self.assertEqual(programs.powershell_command([]), "")


class Launchers(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def test_the_generated_text_is_todays(self):
        self.assertEqual(programs.launcher_text(["prog", "a b"]), "#!/bin/sh\nexec prog 'a b'\n")
        self.assertEqual(programs.launcher_text(["prog"], forward_arguments=True), '#!/bin/sh\nexec prog "$@"\n')
        self.assertEqual(programs.launcher_text(["prog"], header="# a comment\n"),
                         "#!/bin/sh\n# a comment\nexec prog\n")
        self.assertEqual(programs.launcher_text(["env", "A='b c'", "prog", '"$@"'], quoted=True, forward_arguments=True),
                         "#!/bin/sh\nexec env A='b c' prog \"$@\" \"$@\"\n")

    def test_a_launcher_is_written_with_todays_text(self):
        path = self.root / "helper"
        programs.write_launcher(path, ["prog", "a b"])
        self.assertEqual(path.read_text(encoding="utf-8"), programs.launcher_text(["prog", "a b"]))
        if sys.platform != "win32":
            self.assertTrue(os.access(path, os.X_OK))

    def test_an_older_launcher_is_replaced_and_nothing_else_is_left(self):
        path = self.root / "helper"
        path.write_text("old\n", encoding="utf-8")
        programs.write_launcher(path, ["prog"])
        self.assertEqual(path.read_text(encoding="utf-8"), "#!/bin/sh\nexec prog\n")
        self.assertEqual([name for name in os.listdir(self.root) if name != "helper"], [])

    def test_an_unchanged_launcher_is_left_alone_when_asked(self):
        path = self.root / "helper"
        programs.write_launcher(path, ["prog"])
        before = path.stat().st_mtime_ns
        programs.write_launcher(path, ["prog"], keep_unchanged=True)
        self.assertEqual(path.stat().st_mtime_ns, before)
        programs.write_launcher(path, ["other"], keep_unchanged=True)
        self.assertEqual(path.read_text(encoding="utf-8"), "#!/bin/sh\nexec other\n")
        if sys.platform != "win32":
            path.chmod(0o600)  # A launcher this user may not run is written again.
            programs.write_launcher(path, ["other"], keep_unchanged=True)
            self.assertTrue(os.access(path, os.X_OK))

    def test_a_launcher_runs_one_of_several_commands_by_its_argument(self):
        self.assertEqual(programs.mode_launcher({"bootstrap": ["prog", "a b"], "skills": ["other"]},
                                                header="# a comment\n"),
                         "#!/bin/sh\n# a comment\n[ \"$#\" -eq 1 ] || exit 64\ncase \"$1\" in\n"
                         "  bootstrap) exec prog 'a b' ;;\n  skills) exec other ;;\n  *) exit 64 ;;\nesac\n")
        self.assertEqual(programs.mode_launcher({}, header="# only a comment\n"),
                         "#!/bin/sh\n# only a comment\n[ \"$#\" -eq 1 ] || exit 64\ncase \"$1\" in\n"
                         "  *) exit 64 ;;\nesac\n")

    def test_the_owner_header_helper_is_todays_script(self):
        self.assertEqual(programs.headers_launcher(),
                         "#!/bin/sh\n# Prints the Qorqut owner-session MCP authorization header for this root.\n"
                         "set -e\nprintf '{\"Authorization\":\"Bearer %s\"}' "
                         "\"$(tr -d '[:space:]' < \"$(dirname \"$0\")/credential\")\"\n")


class ClientCommand(unittest.TestCase):
    def test_the_command_that_runs_a_launcher_is_todays(self):
        self.assertEqual(programs.launcher_command("/w/.qorqut/codex-hook"), ["/bin/sh", "/w/.qorqut/codex-hook"])

    def test_a_client_gets_the_command_of_this_system(self):
        command = programs.client_command("/opt/python3", ["-m", "qorqut.bootstrap.mcp_reconnect"],
                                          launcher="/w/.qorqut/mcp-bridge")
        if sys.platform == "win32":
            self.assertEqual(command, ["/opt/python3", "-m", "qorqut.bootstrap.mcp_reconnect"])
        else:
            self.assertEqual(command, ["/bin/sh", "/w/.qorqut/mcp-bridge"])


class Loading(unittest.TestCase):
    def test_the_module_names_its_operations(self):
        self.assertEqual(sorted(programs.__all__), sorted([
            "HOOK_CLIENTS", "client_command", "command", "headers_launcher", "helper_command", "hook_command",
            "launcher_command", "launcher_text", "mode_launcher", "powershell_command", "quote", "write_launcher"]))
        self.assertEqual(programs.HOOK_CLIENTS, ("claude-code", "codex"))


if __name__ == "__main__":
    unittest.main()