from __future__ import annotations

import json
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
INSTALLER = ROOT / "tools/release_worker/install.py"


def synthetic_docs(tmp_path: Path, name: str = "source-docs") -> Path:
    """A minimal release-guide source: the two required guides and one neutral nested document."""
    docs = tmp_path / name
    if not docs.exists():
        (docs / "guides").mkdir(parents=True)
        (docs / "README.md").write_text("Synthetic release guide index.\n")
        (docs / "INSTALL.md").write_text("Synthetic install guide.\n")
        (docs / "guides/example.md").write_text("Synthetic nested guide.\n")
    return docs


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def fixture_repo(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "trusted"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "fixture")
    shutil.copytree(ROOT / "bazarlyk", repo / "bazarlyk", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # Keep the fixture dependency-free. Provenance tests exercise the complete
    # worker-file set and launcher validation; lifecycle behavior is covered by
    # the release-worker suite.
    (repo / "bazarlyk/release_worker/__init__.py").write_text("__all__ = []\n")
    (repo / "bazarlyk/release_worker/cli.py").write_text("def main(argv=None):\n    return 0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "fixture")
    commit = git(repo, "rev-parse", "HEAD")
    artifact = tmp_path / "release-artifact"
    artifact.mkdir()
    shutil.copytree(repo / "bazarlyk", artifact / "bazarlyk", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (artifact / ".venv/bin").mkdir(parents=True)
    (artifact / ".venv/bin/python").symlink_to(sys.executable)
    return repo, artifact, commit


def run_install(tmp_path: Path, repo: Path, artifact: Path, commit: str, *, version: str = "fixture-1", extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "installed"
    args = [
        sys.executable, str(INSTALLER), "--source", str(artifact), "--python", sys.executable, "--version", version,
        "--commit", commit, "--provenance-repository", str(repo), "--trusted-ref", "HEAD",
        "--docs", str(synthetic_docs(tmp_path)), "--root", str(root), "--config", str(tmp_path / "active.json"),
        "--bin", str(tmp_path / "bin/release-worker"), "--activate",
    ]
    if extra:
        args.extend(extra)
    return subprocess.run(args, text=True, capture_output=True)


def test_install_exposes_stable_docs_and_provenance_checked_launcher(tmp_path: Path) -> None:
    repo, artifact, commit = fixture_repo(tmp_path)
    result = run_install(tmp_path, repo, artifact, commit)
    assert result.returncode == 0, result.stderr
    launcher = tmp_path / "bin/release-worker"
    assert subprocess.run([str(launcher), "--docs-path"], text=True, capture_output=True).stdout.strip().endswith("/docs/fixture-1")
    installer_path = subprocess.run([str(launcher), "--installer-path"], text=True, capture_output=True, check=True).stdout.strip()
    assert Path(installer_path).is_file()
    version = subprocess.run([str(launcher), "--version"], text=True, capture_output=True, check=True).stdout
    assert commit in version and "worker_digest:" in version
    assert (tmp_path / "installed/docs/fixture-1/INSTALL.md").is_file()


def test_fabricated_commit_and_unmerged_commit_are_rejected(tmp_path: Path) -> None:
    repo, artifact, commit = fixture_repo(tmp_path)
    fabricated = run_install(tmp_path, repo, artifact, "0" * 40)
    assert fabricated.returncode != 0
    assert "trusted provenance" in fabricated.stderr
    git(repo, "checkout", "-q", "-b", "unmerged")
    (repo / "unmerged.txt").write_text("unmerged")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "unmerged")
    unmerged = git(repo, "rev-parse", "HEAD")
    result = run_install(tmp_path, repo, artifact, unmerged, extra=["--trusted-ref", commit])
    assert result.returncode != 0
    assert "not an ancestor" in result.stderr


def test_relocated_v12_app_requires_explicit_provenance_subdirectory(tmp_path: Path) -> None:
    repo, artifact, _ = fixture_repo(tmp_path)
    (repo / "app").mkdir()
    git(repo, "mv", "bazarlyk", "app/bazarlyk")
    git(repo, "commit", "-q", "-m", "relocate host")
    commit = git(repo, "rev-parse", "HEAD")
    rejected = run_install(tmp_path, repo, artifact, commit)
    assert rejected.returncode != 0 and "no first-party Python" in rejected.stderr
    installed = run_install(tmp_path, repo, artifact, commit, extra=["--source-subdirectory", "app"])
    assert installed.returncode == 0, installed.stderr
    manifest = json.loads((tmp_path / "active.json").read_text())
    assert manifest["source_subdirectory"] == "app"
    assert subprocess.run([str(tmp_path / "bin/release-worker"), "--version"], capture_output=True).returncode == 0


@pytest.mark.parametrize("relative", ["bazarlyk/release_worker/worker.py", "bazarlyk/release_worker/cli.py"])
def test_modified_worker_file_is_rejected_even_when_main_is_unchanged(tmp_path: Path, relative: str) -> None:
    repo, artifact, commit = fixture_repo(tmp_path)
    target = artifact / relative
    target.write_text(target.read_text() + "\n# tampered\n")
    result = run_install(tmp_path, repo, artifact, commit)
    assert result.returncode != 0
    assert "does not match trusted commit" in result.stderr


def test_importable_package_shadowing_is_rejected(tmp_path: Path) -> None:
    repo, artifact, commit = fixture_repo(tmp_path)
    shadow = artifact / "bazarlyk/release_worker/cli"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("def main(argv=None): print('UNVERIFIED_PACKAGE_EXECUTED')\n")
    result = run_install(tmp_path, repo, artifact, commit)
    assert result.returncode != 0 and "unexpected bazarlyk/release_worker/cli/__init__.py" in result.stderr


def test_sourceless_bytecode_shadowing_is_rejected_before_and_after_install(tmp_path: Path) -> None:
    repo, artifact, commit = fixture_repo(tmp_path)
    external = tmp_path / "shadow.py"
    external.write_text("def main(argv=None): print('UNVERIFIED_BYTECODE_EXECUTED')\n")
    shadow = artifact / "bazarlyk/release_worker/cli/__init__.pyc"
    shadow.parent.mkdir(exist_ok=True)
    py_compile.compile(str(external), cfile=str(shadow), doraise=True)
    rejected = run_install(tmp_path, repo, artifact, commit)
    assert rejected.returncode != 0 and "sourceless bytecode" in rejected.stderr

    shadow.unlink()
    installed = run_install(tmp_path, repo, artifact, commit)
    assert installed.returncode == 0, installed.stderr
    shadow = tmp_path / "installed/versions/fixture-1/code/bazarlyk/release_worker/cli/__init__.pyc"
    shadow.parent.mkdir(exist_ok=True)
    py_compile.compile(str(external), cfile=str(shadow), doraise=True)
    launcher = subprocess.run([str(tmp_path / "bin/release-worker"), "--help"], text=True, capture_output=True)
    assert launcher.returncode != 0 and "sourceless bytecode" in launcher.stderr


def test_active_manifest_mismatch_and_missing_inputs_fail(tmp_path: Path) -> None:
    repo, artifact, commit = fixture_repo(tmp_path)
    result = run_install(tmp_path, repo, artifact, commit)
    assert result.returncode == 0, result.stderr
    installed_code = tmp_path / "installed/versions/fixture-1/code"
    (installed_code / "bazarlyk/release_worker/worker.py").write_text((installed_code / "bazarlyk/release_worker/worker.py").read_text() + "\n# runtime tamper\n")
    tampered = subprocess.run([str(tmp_path / "bin/release-worker"), "--version"], text=True, capture_output=True)
    assert tampered.returncode != 0 and "failed provenance check" in tampered.stderr
    # Restore the fixture before exercising active-manifest mismatch.
    shutil.copy2(repo / "bazarlyk/release_worker/worker.py", installed_code / "bazarlyk/release_worker/worker.py")
    config = tmp_path / "active.json"
    manifest_path = Path(json.loads(config.read_text())["manifest"])
    manifest = json.loads(manifest_path.read_text())
    manifest["source_commit"] = "0" * 40
    manifest_path.write_text(json.dumps(manifest))
    launcher_result = subprocess.run([str(tmp_path / "bin/release-worker"), "--version"], text=True, capture_output=True)
    assert launcher_result.returncode != 0
    assert "does not match manifest" in launcher_result.stderr
    missing = run_install(tmp_path, repo, tmp_path / "missing", commit, version="missing")
    assert missing.returncode != 0 and "does not exist" in missing.stderr
    missing_docs = run_install(tmp_path, repo, artifact, commit, version="missing-docs", extra=["--docs", str(tmp_path / "missing-docs")])
    assert missing_docs.returncode != 0 and "release docs are missing" in missing_docs.stderr
    no_python = artifact / ".venv/bin/python"
    no_python.unlink()
    result = run_install(tmp_path, repo, artifact, commit, version="no-python", extra=["--python", str(no_python)])
    assert result.returncode != 0 and "Python" in result.stderr


def test_existing_version_cannot_be_repointed_and_new_version_updates_explicitly(tmp_path: Path) -> None:
    repo, artifact, commit = fixture_repo(tmp_path)
    first = run_install(tmp_path, repo, artifact, commit)
    assert first.returncode == 0
    updater = Path(json.loads((tmp_path / "active.json").read_text())["installer_path"])
    artifact_two = tmp_path / "release-artifact-two"
    shutil.copytree(artifact, artifact_two, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    updated = subprocess.run([
        str(updater), "--source", str(artifact_two), "--python", sys.executable, "--version", "fixture-2", "--commit", commit,
        "--provenance-repository", str(repo), "--trusted-ref", "HEAD", "--root", str(tmp_path / "installed"),
        "--config", str(tmp_path / "active.json"), "--bin", str(tmp_path / "bin/release-worker"), "--activate",
    ], text=True, capture_output=True)
    assert updated.returncode == 0, updated.stderr
    docs = Path(subprocess.run([str(tmp_path / "bin/release-worker"), "--docs-path"], text=True, capture_output=True, check=True).stdout.strip())
    assert docs.name == "fixture-2" and (docs / "guides/example.md").is_file()


def test_conflicting_version_does_not_replace_active_docs_or_updater(tmp_path: Path) -> None:
    repo, artifact, commit = fixture_repo(tmp_path)
    first = run_install(tmp_path, repo, artifact, commit)
    assert first.returncode == 0
    config = tmp_path / "active.json"
    before = json.loads(config.read_text())
    docs_path = Path(before["docs_path"])
    before_docs = (docs_path / "INSTALL.md").read_bytes()
    before_updater = Path(before["installer_path"]).read_bytes()
    alternate_docs = synthetic_docs(tmp_path, "alternate-docs")
    (alternate_docs / "INSTALL.md").write_text("REJECTED UPDATE DOCS\n")
    artifact_two = tmp_path / "release-artifact-two"
    shutil.copytree(artifact, artifact_two, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    rejected = run_install(tmp_path, repo, artifact_two, commit, extra=["--docs", str(alternate_docs)])
    assert rejected.returncode != 0 and "different metadata" in rejected.stderr
    assert (docs_path / "INSTALL.md").read_bytes() == before_docs
    assert Path(before["installer_path"]).read_bytes() == before_updater
    assert json.loads(config.read_text()) == before


def test_incomplete_target_assets_are_repaired_before_activation(tmp_path: Path) -> None:
    repo, artifact, commit = fixture_repo(tmp_path)
    first = run_install(tmp_path, repo, artifact, commit)
    assert first.returncode == 0
    root = tmp_path / "installed"
    stale_docs = root / "docs/fixture-2"
    stale_docs.mkdir(parents=True)
    (stale_docs / "README.md").write_text("stale partial docs")
    updated = run_install(tmp_path, repo, artifact, commit, version="fixture-2")
    assert updated.returncode == 0, updated.stderr
    assert (stale_docs / "guides/example.md").is_file()
    assert subprocess.run([str(tmp_path / "bin/release-worker"), "--help"], capture_output=True).returncode == 0

    # An identical manifest with damaged assets is repaired before the active
    # pointer is refreshed.
    manifest = root / "versions/fixture-2/manifest.json"
    installer = root / "versions/fixture-2/installer.py"
    (stale_docs / "INSTALL.md").unlink()
    installer.unlink()
    repaired = run_install(tmp_path, repo, artifact, commit, version="fixture-2")
    assert repaired.returncode == 0, repaired.stderr
    assert (stale_docs / "INSTALL.md").is_file() and installer.is_file()
    assert subprocess.run([str(tmp_path / "bin/release-worker"), "--version"], capture_output=True).returncode == 0


def test_source_backed_cache_payload_is_never_executed(tmp_path: Path) -> None:
    import importlib.util
    import marshal
    import py_compile

    repo, artifact, commit = fixture_repo(tmp_path)

    def poison(relative: str, payload: str) -> None:
        source = artifact / relative
        cache = Path(importlib.util.cache_from_source(str(source)))
        py_compile.compile(str(source), cfile=str(cache), doraise=True)
        header = cache.read_bytes()[:16]
        cache.write_bytes(header + marshal.dumps(compile(payload, str(source), "exec")))

    marker = tmp_path / "unverified-cache-ran"
    poison("bazarlyk/release_worker/__init__.py",
           f"from pathlib import Path; Path({str(marker)!r}).touch()")
    first = run_install(tmp_path, repo, artifact, commit)
    assert first.returncode == 0, first.stderr
    assert not marker.exists(), "installation imported cached code instead of checked source"
    poison("bazarlyk/release_worker/cli.py",
           f"def main(argv=None):\n from pathlib import Path\n Path({str(marker)!r}).touch()\n return 19\n")
    result = subprocess.run([str(tmp_path / "bin/release-worker"), "--help"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "launcher executed an unverified cache payload"
