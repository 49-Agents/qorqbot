"""Phase A item 1: explicit init/open, catalog, validation, snapshot import."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from qorqut_agentic import AgenticError, AgenticLayer
from tests.conftest import ATTR, EXAMPLES, import_example, make_layer, sha


def test_importing_the_package_is_inert(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    code = "import sys, threading; import qorqut_agentic; print(threading.active_count())"
    env = {"HOME": str(home), "PATH": os.defpath, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    done = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "1"  # no background threads
    assert list(home.iterdir()) == [] and list(cwd.iterdir()) == []


def test_open_never_creates_state_or_guesses_a_location(tmp_path):
    root, state = tmp_path / "root", tmp_path / "state"
    root.mkdir()
    with pytest.raises(AgenticError) as caught:
        AgenticLayer.open(root, state)
    assert caught.value.code == "store_not_initialized"
    assert not state.exists()
    with pytest.raises(AgenticError) as caught:
        AgenticLayer.open(tmp_path / "missing-root", state)
    assert caught.value.code == "invalid_root"
    assert not (tmp_path / "missing-root").exists() and not state.exists()


def test_initialize_validates_paths_and_refuses_reinitialization(tmp_path):
    layer = make_layer(tmp_path)
    assert (layer.root / ".git").exists() and (layer.state / "state.sqlite3").is_file()
    with pytest.raises(AgenticError) as caught:
        make_layer(tmp_path)
    assert caught.value.code == "already_initialized"
    # state inside the root, or a root that is a non-empty plain directory, are refused
    with pytest.raises(AgenticError) as caught:
        AgenticLayer.initialize(tmp_path / "r2", tmp_path / "r2" / "state")
    assert caught.value.code == "invalid_state_path"
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "notes.txt").write_text("keep")
    with pytest.raises(AgenticError) as caught:
        AgenticLayer.initialize(tmp_path / "plain", tmp_path / "s3")
    assert caught.value.code == "invalid_root"
    assert not (tmp_path / "s3").exists() and not (tmp_path / "plain" / ".git").exists()
    # a state store belongs to one root
    other = tmp_path / "other-root"
    other.mkdir()
    with pytest.raises(AgenticError) as caught:
        AgenticLayer.open(other, layer.state)
    assert caught.value.code == "state_mismatch"


def test_catalog_routing_loads_skill_and_referenced_preferences(email_layer):
    listing = email_layer.catalog.list(query="email")
    assert {e["ref"] for e in listing["entries"]} == {"email-composition/compose", "email-composition/style"}
    entry = email_layer.catalog.read_entry("email-composition/compose", include_dependency_bodies=True)
    assert entry["body"].startswith("---\nname: compose-email\n")  # frontmatter preserved verbatim
    assert entry["frontmatter"]["name"] == "compose-email"
    assert [d["entry"]["ref"] for d in entry["dependencies"]] == ["email-composition/style"]
    assert "em dash" in entry["dependencies"][0]["body"]
    assert {r["path"] for r in entry["resources"]} == {"skills/compose/assets/checklist.txt", "skills/compose/assets/logo.png"}
    assert entry["entry"]["revision"] == sha(entry["body"])


def test_listing_never_depends_on_a_keyword_match(email_layer):
    everything = email_layer.catalog.list()
    assert everything["total"] == 2
    assert email_layer.catalog.list(query="zzz-no-match")["total"] == 0
    assert email_layer.catalog.list(kinds=["preference"])["total"] == 1


def test_import_copies_every_byte_and_records_provenance(email_layer, tmp_path):
    source = EXAMPLES / "email-composition"
    for path in source.rglob("*"):
        if path.is_file() and path.name != "agentic.yaml":
            copied = email_layer.root / "plugins" / "email-composition" / path.relative_to(source)
            assert copied.read_bytes() == path.read_bytes()
            info = copied.lstat()
            assert stat.S_ISREG(info.st_mode) and info.st_ino != path.stat().st_ino  # a copy, not a link
    resource = email_layer.catalog.read_resource("email-composition", "skills/compose/assets/logo.png")
    assert resource["encoding"] == "base64" and resource["size"] == (source / "skills/compose/assets/logo.png").stat().st_size

    other = make_layer(tmp_path / "second")
    result = import_example(other, "email-composition", provenance={"kind": "example", "commit": "abc123"})
    source_meta = other.catalog.list()["entries"][0]["source"]
    assert source_meta["commit"] == "abc123" and source_meta["snapshot_digest"] == result["snapshot_digest"]
    assert source_meta["note"].startswith("Synthetic example")  # existing provenance merged, not replaced


def test_import_from_in_memory_files_and_manifest(layer):
    files = [
        {"path": "skills/s/SKILL.md", "content": "---\nname: s\n---\nbody\n"},
        {"path": "skills/s/bin/run.sh", "content": "#!/bin/sh\n", "executable": True},
    ]
    manifest = {
        "schema_version": 1, "id": "inline-pkg", "name": "Inline", "description": "d",
        "entries": [{"id": "s", "kind": "skill", "description": "d", "when_to_use": "w", "path": "skills/s/SKILL.md"}],
    }
    layer.authoring.import_package(ATTR, "inline-pkg", {"files": files}, manifest=manifest)
    assert os.access(layer.root / "plugins/inline-pkg/skills/s/bin/run.sh", os.X_OK)
    assert layer.catalog.read_entry("inline-pkg/s")["resources"][0]["path"] == "skills/s/bin/run.sh"


def test_reimport_requires_the_current_package_revision_and_prunes_removed_files(email_layer, tmp_path):
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    for path in (EXAMPLES / "email-composition").rglob("*"):
        if path.is_file():
            target = snapshot / path.relative_to(EXAMPLES / "email-composition")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    (snapshot / "skills/compose/assets/checklist.txt").unlink()
    with pytest.raises(AgenticError) as caught:
        email_layer.authoring.import_package(ATTR, "email-composition", {"directory": str(snapshot)})
    assert caught.value.code == "conflict"
    current = caught.value.details["conflicts"][0]["current_package_revision"]
    with pytest.raises(AgenticError) as caught:
        email_layer.authoring.import_package(ATTR, "email-composition", {"directory": str(snapshot)},
                                             expected_package_revision="0" * 64)
    assert caught.value.code == "conflict"
    result = email_layer.authoring.import_package(ATTR, "email-composition", {"directory": str(snapshot)},
                                                  expected_package_revision=current)
    assert [f["path"] for f in result["files"] if f["action"] == "delete"] == [
        "plugins/email-composition/skills/compose/assets/checklist.txt"]
    assert not (email_layer.root / "plugins/email-composition/skills/compose/assets/checklist.txt").exists()
    assert result["change"]["details"]["mode"] == "update"


def test_dry_run_reports_the_plan_and_writes_nothing(layer):
    before = layer.changes.list()["head"]
    plan = layer.authoring.import_package(ATTR, "email-composition", {"directory": str(EXAMPLES / "email-composition")},
                                          dry_run=True)
    assert plan["dry_run"] and not plan["changed"] and len(plan["files"]) == 5
    assert not (layer.root / "plugins").exists() and layer.changes.list()["head"] == before


def test_unsafe_snapshots_and_paths_fail(layer, tmp_path):
    snap = tmp_path / "snap"
    (snap / "skills").mkdir(parents=True)
    (snap / "agentic.yaml").write_text("x")
    os.symlink("/etc/hostname", snap / "skills" / "link")
    with pytest.raises(AgenticError) as caught:
        layer.authoring.import_package(ATTR, "p", {"directory": str(snap)})
    assert caught.value.code == "unsafe_path"
    os.unlink(snap / "skills" / "link")
    (snap / ".git").mkdir()
    with pytest.raises(AgenticError) as caught:
        layer.authoring.import_package(ATTR, "p", {"directory": str(snap)})
    assert caught.value.code == "unsafe_path"
    for bad in ("/etc/passwd", "../outside", "plugins/../../x", "a//b", ".git/config", "plugins/x/.GIT/y", "a\\b"):
        with pytest.raises(AgenticError) as caught:
            layer.authoring.apply(ATTR, [{"op": "create", "path": bad, "content": "x"}])
        assert caught.value.code == "unsafe_path", bad
    # a symlink inside the root that points outside is never followed
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, layer.root / "escape")
    with pytest.raises(AgenticError) as caught:
        layer.authoring.apply(ATTR, [{"op": "create", "path": "escape/pwned.txt", "content": "x"}])
    assert caught.value.code == "unsafe_path" and not (outside / "pwned.txt").exists()


def _create_package(layer, package_id, entries, extra_files=None):
    manifest = ("schema_version: 1\nid: {}\nname: {}\ndescription: d\nentries:\n".format(package_id, package_id)
                + "".join(entries))
    changes = [{"op": "create", "path": f"plugins/{package_id}/agentic.yaml", "content": manifest}]
    for path, content in (extra_files or {}).items():
        changes.append({"op": "create", "path": f"plugins/{package_id}/{path}", "content": content})
    return layer.authoring.apply(ATTR, changes)


def _entry(entry_id, path, requires=(), kind="instruction"):
    text = f"  - id: {entry_id}\n    kind: {kind}\n    description: d\n    when_to_use: w\n    path: {path}\n"
    if requires:
        text += "    requires: [" + ", ".join(requires) + "]\n"
    return text


def test_broken_references_duplicates_and_cycles_are_rejected_without_writing(layer):
    head = layer.changes.list()["head"]
    cases = {
        "broken_reference": ([_entry("a", "a.md", ["nope/missing"])], {"a.md": "a"}),
        "duplicate_entry_id": ([_entry("a", "a.md"), _entry("a", "b.md")], {"a.md": "a", "b.md": "b"}),
        "missing_entry_file": ([_entry("a", "gone.md")], {}),
        "unsafe_entry_path": ([_entry("a", "../x.md")], {}),
        "dependency_cycle": ([_entry("a", "a.md", ["bad/a"])], {"a.md": "a"}),
    }
    for code, (entries, files) in cases.items():
        with pytest.raises(AgenticError) as caught:
            _create_package(layer, "bad", entries, files)
        assert caught.value.code == "validation_failed", code
        assert code in {i["code"] for i in caught.value.details["issues"]}, (code, caught.value.details["issues"])
        assert not (layer.root / "plugins").exists()
    assert layer.changes.list()["head"] == head and not layer.recovery_required


def test_cross_package_cycle_is_rejected(layer):
    _create_package(layer, "two", [_entry("b", "b.md")], {"b.md": "b"})
    _create_package(layer, "one", [_entry("a", "a.md", ["two/b"])], {"a.md": "a"})
    manifest = layer.root / "plugins/two/agentic.yaml"
    with pytest.raises(AgenticError) as caught:
        layer.authoring.update_entry(ATTR, "two", {"id": "b", "requires": ["one/a"]}, sha(manifest.read_bytes()))
    assert "dependency_cycle" in {i["code"] for i in caught.value.details["issues"]}


def test_scoped_rule_is_absent_from_unrelated_email_work(full_layer):
    general = full_layer.catalog.list(selection={"work_type": "email"})
    assert "influencer-outreach/pricing-rule" not in {e["ref"] for e in general["entries"]}
    body = full_layer.catalog.read_entry("email-composition/compose", selection={"work_type": "email"},
                                         include_dependency_bodies=True)
    assert "Do not state any price" not in str(body)  # the outreach rule does not leak into general email work
    scoped = full_layer.catalog.list(selection={"work_type": "influencer-outreach"})
    refs = {e["ref"] for e in scoped["entries"]}
    assert {"influencer-outreach/pricing-rule", "email-composition/style"} <= refs  # global and scoped both visible
    hidden = full_layer.catalog.list(selection={"work_type": "email"}, include_inapplicable=True)
    rule = next(e for e in hidden["entries"] if e["ref"] == "influencer-outreach/pricing-rule")
    assert rule["applicable"] is False and "influencer-outreach" in rule["diagnostics"][0]
    # unspecified context does not hide anything
    assert full_layer.catalog.list(selection={})["total"] == full_layer.catalog.list()["total"]


def test_disabled_dependency_makes_dependent_unavailable_with_a_diagnostic(full_layer):
    selection = {"package_ids": ["influencer-outreach"], "work_type": "influencer-outreach"}
    visible = {e["ref"] for e in full_layer.catalog.list(selection=selection)["entries"]}
    assert visible == {"influencer-outreach/pricing-rule"}  # the skill needs email-composition/style, which is disabled
    shown = full_layer.catalog.list(selection=selection, include_unavailable=True)
    skill = next(e for e in shown["entries"] if e["ref"] == "influencer-outreach/outreach")
    assert skill["available"] is False and "email-composition/style" in skill["diagnostics"][0]
    with pytest.raises(AgenticError) as caught:
        full_layer.catalog.read_entry("influencer-outreach/outreach", selection=selection)
    assert caught.value.code == "unavailable" and caught.value.details["diagnostics"]
    with pytest.raises(AgenticError) as caught:
        full_layer.catalog.read_resource("email-composition", "preferences/style.md", selection=selection)
    assert caught.value.code == "unavailable"


def test_validate_reports_problems_introduced_outside_the_toolkit(email_layer):
    (email_layer.root / "plugins/email-composition/preferences/style.md").unlink()
    report = email_layer.catalog.validate()
    codes = {i["code"] for i in report["issues"]}
    assert not report["valid"] and {"missing_entry_file", "broken_reference"} <= codes
    # the broken entries are not offered to agents
    assert email_layer.catalog.list()["total"] == 0
    # a toolkit change that does not make things worse is still allowed
    email_layer.authoring.apply(ATTR, [{"op": "create", "path": "plugins/email-composition/NOTES.md", "content": "n"}])
