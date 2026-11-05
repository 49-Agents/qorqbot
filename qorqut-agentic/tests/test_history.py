"""Phase A item 3: non-destructive Git status/diff/commit and restore-as-new-change."""

from __future__ import annotations

import stat
import shutil

import pytest

from qorqut_agentic import AgenticError
from tests.conftest import ATTR, git, import_example, make_layer, reopen, sha

STYLE = "plugins/email-composition/preferences/style.md"
RULE = "plugins/influencer-outreach/preferences/pricing-rule.md"


def _edit(layer, path, text):
    current = (layer.root / path).read_bytes()
    return layer.authoring.apply(ATTR, [{"op": "edit", "path": path, "expected_sha256": sha(current), "content": text}])


def test_status_diff_and_log_are_read_only_and_include_untracked_files(email_layer):
    status = email_layer.history.status()
    assert status["branch"] == "agentic" and status["head"] is None and not status["clean"]
    assert {e["index"] for e in status["entries"]} == {"?"}
    email_layer.history.commit("Import email package", paths=["plugins/email-composition"])
    assert email_layer.history.status()["clean"]
    _edit(email_layer, STYLE, "changed\n")
    (email_layer.root / "scratch.txt").write_text("untracked\n")
    diff = email_layer.history.diff()
    by_path = {f["path"]: f for f in diff["files"]}
    assert by_path[STYLE]["status"] == "M" and by_path["scratch.txt"]["status"] == "untracked"
    assert "+changed" in diff["diff"] and "+untracked" in diff["diff"] and not diff["truncated"]
    assert email_layer.history.diff(paths=[STYLE], include_untracked=False)["files"][0]["path"] == STYLE
    assert email_layer.history.diff(max_bytes=1024)["truncated"] is False
    log = email_layer.history.log(include_files=True)
    assert [c["subject"] for c in log["commits"]] == ["Import email package"]
    assert len(log["commits"][0]["files"]) == 5
    assert email_layer.history.diff(from_revision="HEAD", to_revision="HEAD")["diff"] == ""
    with pytest.raises(AgenticError) as caught:
        email_layer.history.diff(from_revision="--output=/tmp/x")
    assert caught.value.code == "invalid_request"
    with pytest.raises(AgenticError) as caught:
        email_layer.history.diff(from_revision="no-such-rev")
    assert caught.value.code == "not_found"


def test_thematic_change_across_packages_commits_together(full_layer):
    _edit(full_layer, STYLE, "- theme: no exclamation marks anywhere\n")
    _edit(full_layer, RULE, "- theme: no exclamation marks in outreach either\n")
    commit = full_layer.history.commit("Ban exclamation marks in email and outreach", paths=[STYLE, RULE])
    assert sorted(f["path"] for f in commit["files"]) == sorted([RULE, STYLE])
    commits = [c for c in full_layer.history.log()["commits"]]
    assert len(commits) == 1
    # the two content events carry the commit; no new events were created
    events = full_layer.changes.list()["changes"]
    assert [e["commits"] for e in events[-2:]] == [[commit["commit"]]] * 2
    assert len(events) == 5  # three imports and two edits


def test_selected_path_commit_does_not_consume_unrelated_staged_work(full_layer):
    full_layer.history.commit("Baseline", paths=["plugins"])
    _edit(full_layer, STYLE, "selected change\n")
    _edit(full_layer, RULE, "unrelated staged change\n")
    git(full_layer, "add", RULE)
    commit = full_layer.history.commit("Only the style rule", paths=[STYLE])
    assert [f["path"] for f in commit["files"]] == [STYLE]
    assert git(full_layer, "diff", "--cached", "--name-only").stdout.split() == [RULE]  # still staged, not committed
    assert "unrelated staged change" in (full_layer.root / RULE).read_text()
    with pytest.raises(AgenticError) as caught:
        full_layer.history.commit("again", paths=[STYLE])
    assert caught.value.code == "nothing_to_commit"
    # without paths every change is committed
    full_layer.history.commit("Everything else")
    assert full_layer.history.status()["clean"]


def test_deleted_files_and_new_files_commit_by_path(email_layer):
    email_layer.history.commit("Baseline")
    manifest = "plugins/email-composition/agentic.yaml"
    email_layer.authoring.apply(ATTR, [
        {"op": "create", "path": "plugins/email-composition/preferences/extra.md", "content": "extra\n"},
        {"op": "delete", "path": "plugins/email-composition/skills/compose/assets/checklist.txt",
         "expected_sha256": sha((email_layer.root / "plugins/email-composition/skills/compose/assets/checklist.txt").read_bytes())},
    ])
    commit = email_layer.history.commit("Add and remove", paths=["plugins/email-composition/preferences/extra.md",
                                                                "plugins/email-composition/skills/compose/assets/checklist.txt"])
    assert {f["status"] for f in commit["files"]} == {"A", "D"} and commit["linked_changes"] == 2
    assert manifest not in [f["path"] for f in commit["files"]]


def test_restore_applies_an_old_revision_as_a_new_event_and_moves_nothing_in_git(email_layer):
    original = (email_layer.root / STYLE).read_bytes()
    first = email_layer.history.commit("Baseline")["commit"]
    _edit(email_layer, STYLE, "worse\n")
    email_layer.history.commit("Make it worse")
    head = email_layer.history.status()["head"]
    events_before = email_layer.changes.list()["head"]

    plan = email_layer.history.restore(ATTR, first, [STYLE], dry_run=True)
    assert plan["dry_run"] and (email_layer.root / STYLE).read_text() == "worse\n"
    result = email_layer.history.restore(ATTR, first, [STYLE])
    assert (email_layer.root / STYLE).read_bytes() == original
    event = result["change"]
    assert event["kind"] == "restore" and event["sequence"] == events_before + 1
    assert event["details"]["revision"] == first and f"git:{first}" in event["attribution"]["source_refs"]
    assert event["files"][0]["before_sha256"] == sha("worse\n") and event["files"][0]["after_sha256"] == sha(original)
    assert email_layer.history.status()["head"] == head  # no branch movement, no reset
    assert not email_layer.history.status()["clean"]  # the restore is an ordinary uncommitted change
    # caller-pinned hashes make a concurrent edit a conflict
    _edit(email_layer, STYLE, "edited again\n")
    with pytest.raises(AgenticError) as caught:
        email_layer.history.restore(ATTR, first, [STYLE], expected_sha256={STYLE: sha(original)})
    assert caught.value.code == "conflict"
    # restoring a deleted file re-creates it; a path absent from the revision is not found
    (email_layer.root / STYLE).unlink()
    email_layer.changes.scan()
    email_layer.history.restore(ATTR, first, [STYLE])
    assert (email_layer.root / STYLE).read_bytes() == original
    with pytest.raises(AgenticError) as caught:
        email_layer.history.restore(ATTR, first, ["plugins/email-composition/nothing.md"])
    assert caught.value.code == "not_found"


def test_repository_hooks_and_fsmonitor_never_run(email_layer, tmp_path):
    marker = tmp_path / "hook-ran"
    hook = email_layer.root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    monitor = tmp_path / "fsmonitor.sh"
    monitor.write_text(f"#!/bin/sh\ntouch {marker}\n")
    monitor.chmod(0o755)
    git(email_layer, "config", "core.fsmonitor", str(monitor))
    git(email_layer, "config", "core.hooksPath", str(hook.parent))
    email_layer.history.status()
    email_layer.history.diff()
    email_layer.history.commit("Hooked repo")
    assert not marker.exists()
    assert git(email_layer, "log", "--format=%s").stdout.strip() == "Hooked repo"


def test_commit_identity_must_be_supplied_or_configured(tmp_path):
    layer = make_layer(tmp_path, git_user=None)
    import_example(layer, "email-composition")
    with pytest.raises(AgenticError) as caught:
        layer.history.commit("no identity")
    assert caught.value.code == "git_identity_required"
    commit = layer.history.commit("explicit identity", author={"name": "Someone", "email": "someone@example.invalid"})
    assert git(layer, "log", "-1", "--format=%an <%ae>").stdout.strip() == "Someone <someone@example.invalid>"
    assert commit["linked_changes"] == 5


def test_history_is_refused_while_recovery_is_pending_and_missing_git_is_reported(email_layer, tmp_path):
    from tests.conftest import SimulatedCrash, crash_at

    crash_at(email_layer, "after_journal")
    with pytest.raises(SimulatedCrash):
        _edit(email_layer, STYLE, "interrupted\n")
    reopened = reopen(email_layer)
    with pytest.raises(AgenticError) as caught:
        reopened.history.commit("must not capture a half-applied batch")
    assert caught.value.code == "recovery_required"
    assert reopened.history.status()["recovery_required"] is True
    broken = type(email_layer).open(email_layer.root, email_layer.state, git_executable=str(tmp_path / "no-git"))
    with pytest.raises(AgenticError) as caught:
        broken.history.status()
    assert caught.value.code == "git_unavailable"
    assert broken.catalog.list()["total"] == 2  # everything else keeps working without Git


def test_detached_authoring_follows_ordinary_git_behavior(email_layer):
    baseline = email_layer.history.commit("Baseline before detaching")["commit"]
    git(email_layer, "checkout", "--detach", baseline)
    assert email_layer.history.status()["detached"] is True
    _edit(email_layer, STYLE, "detached authoring edit\n")
    result = email_layer.history.commit("Detached authoring commit")
    assert result["commit"] != baseline
    assert email_layer.history.status()["detached"] is True
    assert git(email_layer, "rev-parse", "agentic").stdout.strip() == baseline
    assert email_layer.history.status()["clean"]


def _parent_snapshot(parent):
    return {path.relative_to(parent).as_posix(): path.read_bytes()
            for path in parent.rglob("*") if path.is_file() and "child" not in path.relative_to(parent).parts}


@pytest.mark.parametrize("damage", ["missing", "empty", "invalid", "foreign_worktree"])
def test_history_refuses_damaged_child_repository_without_touching_parent(tmp_path, damage):
    parent = tmp_path / "parent"
    parent.mkdir()
    git(parent, "init", "-b", "synthetic-parent")
    (parent / "parent.txt").write_text("committed parent file\n")
    git(parent, "add", "parent.txt")
    git(parent, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Parent fixture")
    (parent / "parent.txt").write_text("staged parent change\n")
    git(parent, "add", "parent.txt")
    (parent / "untracked.txt").write_text("untracked parent file\n")
    parent_before_init = _parent_snapshot(parent)
    layer = make_layer(parent / "child")
    assert _parent_snapshot(parent) == parent_before_init
    import_example(layer, "email-composition")
    baseline = layer.history.commit("Child baseline")["commit"]
    _edit(layer, STYLE, "child pending edit\n")
    if damage == "foreign_worktree":
        git(layer, "config", "core.worktree", str(parent))
    else:
        shutil.rmtree(layer.root / ".git")
        if damage != "missing":
            (layer.root / ".git").mkdir()
        if damage == "invalid":
            (layer.root / ".git" / "HEAD").write_text("not a valid Git HEAD\n")
    before = _parent_snapshot(parent)
    operations = [layer.history.status, layer.history.log, layer.history.diff,
                  lambda: layer.history.commit("Must not commit parent"),
                  lambda: layer.history.restore(ATTR, baseline, [STYLE])]
    for operation in operations:
        with pytest.raises(AgenticError, match="authoring root"):
            operation()
    assert _parent_snapshot(parent) == before  # Includes parent HEAD, index and all owned files.


def test_git_ceiling_applies_to_init_config_probe_and_caller_environment(tmp_path, monkeypatch):
    from qorqut_agentic import history
    from qorqut_agentic.history import GitRunner

    root = tmp_path / "child"
    root.mkdir()
    runner = GitRunner(shutil.which("git"), root, tmp_path / "home")
    calls = []
    run = history.subprocess.run
    def capture(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return run(command, **kwargs)
    monkeypatch.setattr(history.subprocess, "run", capture)
    runner.run(["init", "-q", "-b", "synthetic-child"])
    runner.run(["status", "--porcelain"], env={"GIT_CEILING_DIRECTORIES": "/"})
    assert any("config" in command for command, _ in calls)
    assert any("init" in command for command, _ in calls)
    assert all(environment["GIT_CEILING_DIRECTORIES"] == str(tmp_path) for _, environment in calls)


def test_linked_worktree_remains_an_owned_history_root(email_layer, tmp_path):
    from qorqut_agentic import AgenticLayer
    from tests.conftest import GIT_USER

    baseline = email_layer.history.commit("Linked baseline")["commit"]
    linked_root = tmp_path / "linked-root"
    git(email_layer, "worktree", "add", "-b", "synthetic-linked", str(linked_root), baseline)
    linked = AgenticLayer.initialize(linked_root, tmp_path / "linked-state", git_user=GIT_USER)
    assert (linked.root / ".git").is_file()
    assert linked.history.status()["branch"] == "synthetic-linked"
    _edit(linked, STYLE, "linked edit\n")
    changed = linked.history.commit("Linked edit")["commit"]
    assert changed != baseline and email_layer.history.status()["head"] == baseline
    linked.history.restore(ATTR, baseline, [STYLE])
    assert linked.history.status()["head"] == changed
    assert (linked.root / STYLE).read_bytes() == (email_layer.root / STYLE).read_bytes()
