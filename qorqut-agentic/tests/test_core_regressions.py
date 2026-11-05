"""Core contract regressions; no host, providers or workflow stubs."""
import shutil

import pytest

from qorqut_agentic import AgenticError, AgenticLayer
from tests.conftest import ATTR, file_sha, git, reopen, sha

STYLE = "plugins/email-composition/preferences/style.md"
COMPOSE = "plugins/email-composition/skills/compose/SKILL.md"


def test_open_and_reads_never_recreate_runtime_paths(email_layer):
    shutil.rmtree(email_layer.state / "scratch")
    (email_layer.state / "authoring.lock").unlink()
    opened = reopen(email_layer)
    assert not (opened.state / "scratch").exists()
    assert not (opened.state / "authoring.lock").exists()
    assert opened.changes.list()["head"] == 1
    with pytest.raises(AgenticError) as caught:
        opened.catalog.list()
    assert caught.value.code == "store_not_initialized"
    assert not (opened.state / "authoring.lock").exists()


def test_lost_database_is_not_recreated_by_an_existing_handle(email_layer):
    (email_layer.state / "state.sqlite3").unlink()
    with pytest.raises(AgenticError):
        email_layer.changes.list()
    assert not (email_layer.state / "state.sqlite3").exists()


def test_readers_cannot_write_the_database(email_layer):
    with email_layer._ctx.store.reader() as conn:
        with pytest.raises(Exception, match="readonly"):
            conn.execute("DELETE FROM changes")


def test_external_edit_after_journaling_is_never_overwritten(email_layer):
    original = file_sha(email_layer, STYLE)
    def edit(**_):
        (email_layer.root / STYLE).write_text("outside edit\n")
    email_layer._ctx.hooks["after_journal"] = edit
    with pytest.raises(AgenticError) as caught:
        email_layer.authoring.apply(ATTR, [{"op": "edit", "path": STYLE,
            "expected_sha256": original, "content": "toolkit edit\n"}])
    assert caught.value.code == "recovery_required"
    assert (email_layer.root / STYLE).read_text() == "outside edit\n"
    assert email_layer.recovery_required and not email_layer.recovery_status()["resolvable"]
    assert email_layer.changes.list()["head"] == 1


def test_metadata_only_change_lists_affected_entry(email_layer):
    result = email_layer.authoring.update_entry(ATTR, "email-composition", {"id": "style", "when_to_use": "Any outgoing email"},
                                               file_sha(email_layer, "plugins/email-composition/agentic.yaml"))
    assert "email-composition/style" in result["change"]["entry_refs"]
    assert result["entries"][0]["state"] == "changed"


def test_duplicate_entries_are_diagnostic_and_unavailable(email_layer):
    import yaml
    path = email_layer.root / "plugins/email-composition/agentic.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["entries"].append(dict(manifest["entries"][1]))
    path.write_text(yaml.safe_dump(manifest))
    listed = email_layer.catalog.list()
    assert any(i["code"] == "duplicate_entry_id" for i in listed["issues"])
    assert not any(e["ref"] == "email-composition/style" for e in listed["entries"])


def test_commit_does_not_invoke_clean_filters(email_layer, tmp_path):
    marker = tmp_path / "filter-ran"
    script = tmp_path / "filter.sh"
    script.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat\n')
    script.chmod(0o755)
    git(email_layer, "config", "filter.untrusted.clean", str(script))
    (email_layer.root / ".gitattributes").write_text("*.md filter=untrusted\n")
    email_layer.history.commit("Safe local commit", paths=["plugins/email-composition"])
    assert not marker.exists()


def test_edit_before_finalize_leaves_recovery_conflict(email_layer):
    original = file_sha(email_layer, STYLE)
    def edit(**_):
        (email_layer.root / STYLE).write_text("late outside edit\n")
    email_layer._ctx.hooks["before_finalize"] = edit
    with pytest.raises(AgenticError) as caught:
        email_layer.authoring.apply(ATTR, [{"op": "edit", "path": STYLE,
            "expected_sha256": original, "content": "toolkit edit\n"}])
    assert caught.value.code == "recovery_required"
    assert email_layer.changes.list()["head"] == 1
    assert not reopen(email_layer).recovery_status()["resolvable"]


def test_management_resources_import_and_have_reachable_skill(layer):
    from importlib.resources import as_file, files
    with as_file(files("qorqut_agentic").joinpath("resources", "defaults", "operations", "agentic-management")) as snapshot:
        result = layer.authoring.import_package(ATTR, "agentic-management", {"directory": str(snapshot)})
    assert result["changed"]
    assert layer.catalog.validate()["valid"]
    skill = layer.catalog.read_entry("agentic-management/manage")
    assert skill["frontmatter"]["name"] == "manage-agentic-layer"
    assert "Keep proposed changes out of active files" in skill["body"]
    assert layer.catalog.read_entry("agentic-management/bootstrap")["body"]
    assert layer._workflows is None  # no workflow module is imported for core operations
