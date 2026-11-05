import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from bazarlyk import codex_connectors, connectors
from qorqut_operations.contracts import DomainError

NOTION = "https://mcp.notion.com/mcp"


def listing(*servers):
    """`codex mcp list --json` output for (name, url, auth_status[, enabled]) tuples, plus one stdio server."""
    entries = [{"name": "local-tool", "enabled": True, "transport": {"type": "stdio", "command": "echo"},
                "auth_status": "unsupported"}]
    for server in servers:
        name, url, auth = server[:3]
        entries.append({"name": name, "enabled": server[3] if len(server) > 3 else True,
                        "transport": {"type": "streamable_http", "url": url}, "auth_status": auth})
    return json.dumps(entries)


class Completed(SimpleNamespace):
    pass


class FakeRun:
    """Stands in for subprocess.run: answers `codex mcp ...` from an in-memory server list and records each call."""

    def __init__(self, servers=()):
        self.servers, self.calls, self.fail = list(servers), [], set()

    def __call__(self, command, **kwargs):
        args = command[2:]
        self.calls.append(args)
        if args[0] in self.fail:
            return Completed(returncode=1, stdout="", stderr="Error: boom")
        if args[0] == "list":
            return Completed(returncode=0, stdout=listing(*self.servers), stderr="")
        if args[0] == "add":
            self.servers.append((args[1], args[3], "not_logged_in"))
        if args[0] == "remove":
            self.servers = [server for server in self.servers if server[0] != args[1]]
        return Completed(returncode=0, stdout="", stderr="")


class FakeLogin:
    """Stands in for the `codex mcp login --no-browser` process: prints the link, then exits with `code`."""

    def __init__(self, lines, code=0):
        self.stdout = io.StringIO("".join(lines))
        self.code, self.killed = code, False

    def wait(self):
        return self.code

    def kill(self):
        self.killed = True


class CodexTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        bin_directory = self.directory / "bin"
        bin_directory.mkdir()
        codex = bin_directory / "codex"
        codex.write_text("#!/bin/sh\nexit 1\n")
        codex.chmod(0o755)
        self.environ = {"PATH": str(bin_directory), "HOME": str(self.directory)}
        self.run = FakeRun()
        self.launched = []
        self.login = lambda: FakeLogin(["Authorize the MCP server by opening this URL in your browser: "
                                        "https://mcp.notion.com/authorize?client_id=x\n", "Logged in.\n"])
        self.codex = codex_connectors.Codex(run=self.run, popen=self._popen)

    def _popen(self, command, **kwargs):
        self.launched.append(command[2:])
        return self.login()


class ParseTest(unittest.TestCase):
    def test_parse_keeps_remote_servers_only(self):
        servers = codex_connectors.parse_list(listing(("notion", NOTION, "o_auth"), ("off", "https://x.dev/mcp", "unknown", False)))
        self.assertEqual(servers, [{"name": "notion", "url": NOTION, "auth": "o_auth", "enabled": True},
                                   {"name": "off", "url": "https://x.dev/mcp", "auth": "unknown", "enabled": False}])
        self.assertEqual(codex_connectors.parse_list("{}"), [])
        with self.assertRaises(DomainError):
            codex_connectors.parse_list("not json")

    def test_state_matches_by_address(self):
        servers = codex_connectors.parse_list(listing(("my-notion", NOTION + "/", "o_auth")))
        self.assertEqual(codex_connectors.state_for(NOTION, servers), {"status": "connected", "name": "my-notion"})
        for auth, expected in (("unknown", "needs_sign_in"), ("not_logged_in", "needs_sign_in"),
                               ("bearer_token", "connected"), ("unsupported", "connected")):
            servers = codex_connectors.parse_list(listing(("n", NOTION, auth)))
            self.assertEqual(codex_connectors.state_for(NOTION, servers)["status"], expected)
        disabled = codex_connectors.parse_list(listing(("n", NOTION, "o_auth", False)))
        self.assertEqual(codex_connectors.state_for(NOTION, disabled)["status"], "off")
        self.assertEqual(codex_connectors.state_for("https://mcp.linear.app/mcp", servers), {"status": "not_added"})


class CodexTest(CodexTestCase):
    def test_servers_reports_missing_codex_without_running_anything(self):
        result = self.codex.servers({"PATH": str(self.directory / "nowhere")})
        self.assertEqual(result, {"available": False, "reason": "codex_missing", "servers": []})
        self.assertEqual(self.run.calls, [])

    def test_servers_are_cached_briefly(self):
        self.codex.servers(self.environ)
        self.codex.servers(self.environ)
        self.assertEqual(self.run.calls, [["list", "--json"]])
        self.codex.servers(self.environ, fresh=True)
        self.assertEqual(len(self.run.calls), 2)

    def test_failed_list_is_unavailable(self):
        self.run.fail.add("list")
        self.assertEqual(self.codex.servers(self.environ)["reason"], "codex_failed")

    def test_sign_in_returns_the_printed_link(self):
        state = self.codex.start_sign_in(self.environ, "notion", "notion")
        self.assertEqual(state["link"], "https://mcp.notion.com/authorize?client_id=x")
        self.assertEqual(self.launched, [["login", "notion", "--no-browser"]])

    def test_sign_in_without_a_link_fails_with_codex_message(self):
        self.login = lambda: FakeLogin(["Error: Registration failed: Dynamic client registration not supported\n"], code=1)
        with self.assertRaises(DomainError) as raised:
            self.codex.start_sign_in(self.environ, "notion", "notion")
        self.assertIn("Dynamic client registration", str(raised.exception))
        self.assertEqual(self.codex.sign_in("notion")["running"], False)

    def test_running_sign_in_is_reused(self):
        release = threading.Event()

        class Waiting(FakeLogin):
            def wait(inner):
                release.wait(5)
                return 0
        self.login = lambda: Waiting(["https://mcp.notion.com/authorize?a=1\n"])
        first = self.codex.start_sign_in(self.environ, "notion", "notion")
        second = self.codex.start_sign_in(self.environ, "notion", "notion")
        release.set()
        self.assertEqual(first["link"], second["link"])
        self.assertEqual(len(self.launched), 1)

    def test_names_and_addresses_are_validated(self):
        for name, url in (("Bad Name", NOTION), ("ok", "http://mcp.notion.com/mcp"), ("-x", NOTION)):
            with self.assertRaises(DomainError):
                self.codex.add(self.environ, name, url)
        with self.assertRaises(DomainError):
            self.codex.start_sign_in(self.environ, "notion", "--config")
        self.assertEqual(self.run.calls, [])

    def test_remove_logs_out_then_removes(self):
        self.run.servers.append(("notion", NOTION, "o_auth"))
        self.codex.remove(self.environ, "notion")
        self.assertEqual(self.run.calls, [["logout", "notion"], ["remove", "notion"]])


class ConnectorsCodexTest(CodexTestCase):
    def setUp(self):
        super().setUp()
        self.runtime_dir = self.directory / "runtime"
        self.runtime_dir.mkdir()
        self.runtime = SimpleNamespace(paths=SimpleNamespace(runtime=self.runtime_dir),
                                       resolver=SimpleNamespace(native_connections={}))
        claude = {"Gmail": "connected", "Notion": "needs_sign_in", "Zapier": "connected", "Miro": "needs_sign_in"}
        self.probe = SimpleNamespace(view=lambda environ, refresh=False, cache=None: {
            "checking": False, "result": {"available": True, "connectors": claude}})
        patches = (mock.patch.object(connectors, "PROBE", self.probe),
                   mock.patch.object(connectors, "_probe_environment", side_effect=lambda environ=None: dict(environ)),
                   mock.patch.object(connectors, "_browser_status", return_value={"status": "connected"}),
                   mock.patch.object(connectors, "chrome_extension_installed", return_value=False))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def items(self, result=None):
        result = result or connectors.view(self.runtime, environ=self.environ, codex=self.codex)
        return {item["id"]: item for item in result["connectors"]}

    def test_each_connector_has_a_claude_and_a_codex_status(self):
        self.run.servers.append(("linear", "https://mcp.linear.app/mcp", "o_auth"))
        result = connectors.view(self.runtime, environ=self.environ, codex=self.codex)
        items = self.items(result)
        self.assertEqual(items["gmail"]["clients"], {"claude": {"status": "connected"}, "codex": {"status": "not_available"}})
        self.assertEqual(items["notion"]["clients"]["codex"], {"status": "not_added", "managed": False})
        self.assertEqual(items["linear"]["clients"]["claude"], {"status": "not_added"})
        self.assertEqual(items["linear"]["status"], "connected")
        self.assertEqual(items["chrome"]["clients"]["codex"], {"status": "not_available"})
        self.assertEqual(items["playwright"]["clients"], {"claude": {"status": "off"}, "codex": {"status": "off"}})
        self.assertEqual(result["account_connectors"], [{"name": "Miro", "status": "needs_sign_in"},
                                                        {"name": "Zapier", "status": "connected"}])
        self.assertTrue(result["codex"]["available"])

    def test_every_codex_address_is_https_and_unique(self):
        urls = list(connectors.CODEX_URLS.values())
        self.assertTrue(all(url.startswith("https://") for url in urls))
        self.assertEqual(len(urls), len(set(urls)))
        for entry in connectors.CATALOG:
            self.assertEqual("codex" in entry["clients"], entry["kind"] != "claude" and entry["id"] != "chrome"
                             or "codex_url" in entry, entry["id"])

    def test_connect_adds_records_and_starts_sign_in(self):
        result = connectors.owner_view(self.runtime, {"id": "notion", "client": "codex", "action": "connect"},
                                       environ=self.environ, codex=self.codex)
        self.assertEqual(result["sign_in"]["link"], "https://mcp.notion.com/authorize?client_id=x")
        self.assertIn(["add", "notion", "--url", NOTION], self.run.calls)
        self.assertEqual(connectors.load(self.runtime_dir)["codex_added"], {"notion": "notion"})
        self.assertEqual(self.items(result)["notion"]["clients"]["codex"]["managed"], True)

    def test_connect_reuses_the_owners_own_entry(self):
        self.run.servers.append(("work-notion", NOTION, "unknown"))
        connectors.owner_view(self.runtime, {"id": "notion", "client": "codex", "action": "connect"},
                              environ=self.environ, codex=self.codex)
        self.assertFalse(any(call[0] == "add" for call in self.run.calls))
        self.assertEqual(self.launched, [["login", "work-notion", "--no-browser"]])
        self.assertEqual(connectors.load(self.runtime_dir)["codex_added"], {})

    def test_connect_picks_a_free_name(self):
        self.run.servers.append(("notion", "https://other.example/mcp", "o_auth"))
        connectors.owner_view(self.runtime, {"id": "notion", "client": "codex", "action": "connect"},
                              environ=self.environ, codex=self.codex)
        self.assertIn(["add", "notion-qorqbot", "--url", NOTION], self.run.calls)

    def test_connected_service_needs_no_sign_in(self):
        self.run.servers.append(("notion", NOTION, "o_auth"))
        result = connectors.owner_view(self.runtime, {"id": "notion", "client": "codex", "action": "connect"},
                                       environ=self.environ, codex=self.codex)
        self.assertIsNone(result["sign_in"])
        self.assertEqual(self.launched, [])

    def test_disconnect_removes_only_what_qorqbot_added(self):
        self.run.servers.append(("work-notion", NOTION, "o_auth"))
        with self.assertRaises(DomainError):
            connectors.owner_view(self.runtime, {"id": "notion", "client": "codex", "action": "disconnect"},
                                  environ=self.environ, codex=self.codex)
        self.assertFalse(any(call[0] == "remove" for call in self.run.calls))
        self.run.servers.clear()
        connectors.owner_view(self.runtime, {"id": "linear", "client": "codex", "action": "connect"},
                              environ=self.environ, codex=self.codex)
        connectors.owner_view(self.runtime, {"id": "linear", "client": "codex", "action": "disconnect"},
                              environ=self.environ, codex=self.codex)
        self.assertIn(["remove", "linear"], self.run.calls)
        self.assertEqual(connectors.load(self.runtime_dir)["codex_added"], {})

    def test_claude_only_services_and_bad_requests_are_refused(self):
        for change in ({"id": "gmail", "client": "codex", "action": "connect"},
                       {"id": "notion", "client": "codex", "action": "explode"},
                       {"id": "notion", "client": "claude", "action": "connect"}):
            with self.assertRaises(DomainError):
                connectors.owner_view(self.runtime, change, environ=self.environ, codex=self.codex)
        self.assertEqual(self.launched, [])

    def test_codex_missing_is_explained(self):
        missing = codex_connectors.Codex(run=self.run, popen=self._popen)
        environ = {"PATH": str(self.directory / "nowhere"), "HOME": str(self.directory)}
        notion = self.items(connectors.view(self.runtime, environ=environ, codex=missing))["notion"]
        self.assertEqual(notion["clients"]["codex"], {"status": "unavailable", "reason": "codex_missing"})
        with self.assertRaises(DomainError):
            connectors.owner_view(self.runtime, {"id": "notion", "client": "codex", "action": "connect"},
                                  environ=environ, codex=missing)

    def test_recorded_codex_entries_are_validated(self):
        connectors.state_path(self.runtime_dir).write_text(json.dumps({"version": 1, "enabled": {},
                                                                       "codex_added": {"gmail": "gmail"}}))
        with self.assertRaises(DomainError):
            connectors.load(self.runtime_dir)


if __name__ == "__main__":
    unittest.main()
