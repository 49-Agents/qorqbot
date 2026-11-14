import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("changes", Path(__file__).resolve().parents[1] / "changes.py")
changes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changes)


class ChangeSelection(unittest.TestCase):
    def enabled(self, *paths):
        return {key for key, value in changes.select(paths).items() if value}

    def test_dashboard_and_application_edits_do_not_build_installers(self):
        self.assertEqual(self.enabled("qorqut-code/biz-dashboard/app.css"), {"dashboard"})
        self.assertEqual(self.enabled("qorqut-code/app/qorqut/agents/attention.py"), {"backend"})
        self.assertEqual(self.enabled("qorqut-code/app/qorqut/service/web.py"), {"backend", "dashboard"})

    def test_bundled_extensions_run_the_backend_and_build_the_installer(self):
        for path in ("qorqut-code/extensions/google-calendar/qorqut_google_calendar/calendar.py",
                     "qorqut-code/extensions/treg/qorqut_treg/operations.py"):
            with self.subTest(path=path):
                self.assertEqual(self.enabled(path), {"backend", "distribution"})

    def test_docs_skip_but_managed_instructions_and_prompts_run(self):
        self.assertEqual(self.enabled("RELEASES.md", "qorqut-code/docs/INSTALLATION.md"), set())
        for path in ("qorqut-agentic/src/qorqut_agentic/resources/defaults/roles/owner.md",
                     "qorqut-code/app/qorqut/agentic/assets/reply.md",
                     "qorqut-code/qorqut-context/qorqut_context/prompts/fill.md"):
            with self.subTest(path=path):
                self.assertIn("backend", self.enabled(path))

    def test_shared_installation_contracts_qualify_connectors(self):
        for path in ("qorqut-code/app/qorqut/extensions.py",
                     "qorqut-code/app/qorqut/bootstrap/extension_install.py",
                     "qorqut-code/app/qorqut/composition/assembly.py",
                     "qorqut-code/app/service-constraints.txt",
                     "qorqut-code/qorqut-operations/pyproject.toml",
                     "qorqut-code/tools/prepare-local.py",
                     "qorqut-code/extensions/whatsapp/native/main.go"):
            with self.subTest(path=path):
                self.assertTrue({"backend", "distribution", "connectors"} <= self.enabled(path))

    def test_unknown_paths_and_ci_changes_fail_closed(self):
        for path in ("new-build-input", ".github/workflows/checks.yml"):
            self.assertEqual(self.enabled(path), set(changes.CHECKS))
        self.assertTrue(all(changes.select([], full=True).values()))

    def test_connector_changes_do_not_rebuild_unrelated_go_helper(self):
        self.assertEqual(changes.connector_selection(["qorqut-code/extensions/telegram/pyproject.toml"]), "telegram")
        self.assertEqual(changes.connector_selection(["extensions/qorqut-fastmail/provider.py"]), "fastmail")
        self.assertEqual(changes.connector_selection(["qorqut-code/biz-dashboard/app.css"]), "none")
        self.assertEqual(changes.connector_selection(["qorqut-code/app/qorqut/extensions.py"]), "all")
        self.assertEqual(changes.connector_selection([], full=True), "all")
        self.assertEqual(changes.connector_selection(["qorqut-code/extensions/telegram/provider.py",
            "qorqut-code/extensions/whatsapp/native/main.go"]), "telegram,whatsapp")

    def test_moves_keep_both_path_owners(self):
        with tempfile.TemporaryDirectory() as folder:
            def git(*args):
                return subprocess.check_output(["git", "-C", folder, *args], text=True).strip()
            git("init", "-q")
            git("config", "user.name", "CI test")
            git("config", "user.email", "ci@example.invalid")
            root = Path(folder)
            old = root / "qorqut-code/app/qorqut/extensions.py"
            old.parent.mkdir(parents=True)
            old.write_text("contract\n")
            git("add", ".")
            git("commit", "-qm", "before")
            base = git("rev-parse", "HEAD")
            new = root / "qorqut-code/docs/removed.txt"
            new.parent.mkdir(parents=True)
            old.rename(new)
            git("add", "-A")
            git("commit", "-qm", "after")
            run = subprocess.run
            with patch.object(changes.subprocess, "run", side_effect=lambda args, **kw: run(args, cwd=folder, **kw)):
                paths = changes.changed_paths({"pull_request": {"base": {"sha": base}}}, "pull_request", "HEAD")
            self.assertIn("connectors", self.enabled(*paths))


if __name__ == "__main__":
    unittest.main()
