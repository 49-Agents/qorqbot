"""Acceptance case 1: one accepted Operations engine, bound by exact source, with no second authority in the host.

The receipt names the exact sources the checkpoint was built against; these checks compare it with the checked-out
children, the parent's committed gitlinks and the *installed* engine artifact, read by git object (never a moving
branch). The engine must also import on its own, and the host's removed engine modules must be gone with nothing
importing them."""

import hashlib
import importlib
import importlib.metadata
import importlib.util
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import qorqut_operations
from host_foundation_support import APP, CODE, legacy_roles_source

RECEIPT = json.loads((CODE / "docs" / "v1.2" / "HOST_FOUNDATION_SOURCE_RECEIPT.json").read_text())
ENGINE = RECEIPT["components"]["operations_engine"]


def git(*arguments, cwd=CODE):
    return subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True).stdout


def gitlink(path):
    return git("ls-tree", "HEAD", path).split()[2].decode()


def test_the_receipt_matches_the_checked_out_children_and_the_committed_gitlinks():
    git("merge-base", "--is-ancestor", RECEIPT["parent"]["base_commit"], "HEAD")
    for name in ("operations_engine", "context_engine", "dashboard"):
        component = RECEIPT["components"][name]
        assert git("rev-parse", "HEAD", cwd=CODE / component["path"]).decode().strip() == component["commit"], name
        assert gitlink(component["path"]) == component["commit"], name
    # The dashboard gitlink is the accepted launcher, which descends from the deployed base and carries its launcher change.
    dashboard = RECEIPT["components"]["dashboard"]
    for earlier in (dashboard["base_commit"], dashboard["launcher_commit"], dashboard["test_retarget_commit"], dashboard["foundation_commit"]):
        assert git("merge-base", "--is-ancestor", earlier, dashboard["commit"], cwd=CODE / dashboard["path"]) == b""
    # Every child gitlink descends from its recorded base and contains each reviewed input that the receipt names for it.
    for name in ("context_engine", "dashboard"):
        component = RECEIPT["components"][name]
        child = CODE / component["path"]
        assert git("merge-base", "--is-ancestor", component["base_commit"], component["commit"], cwd=child) == b"", name
        for input_ in component["accepted_inputs"]:
            assert git("merge-base", "--is-ancestor", input_["commit"], component["commit"], cwd=child) == b"", (name, input_["name"])


def test_the_receipt_does_not_certify_the_combined_features():
    """The foundation schema, catalog and installed-host results do not cover the combined feature sources: the receipt says
    so until the actual final assembly is generated and checked."""
    verification = RECEIPT["verification"]
    assert verification["runnable"] is False
    for key in ("schema_and_catalog_receipts", "installed_host_gate", "final_composition"):
        assert verification[key].startswith("pending"), key


def test_every_accepted_input_is_part_of_the_recorded_history():
    """The receipt names the reviewed inputs and the merge commits that carry them; each must be in the history of the
    commit that records it (the parent's HEAD, the pinned Operations commit)."""
    for input_ in RECEIPT["parent"]["accepted_inputs"]:
        git("merge-base", "--is-ancestor", input_["commit"], "HEAD")
        if "merge_commit" in input_:
            git("merge-base", "--is-ancestor", input_["merge_commit"], "HEAD")
            parents = git("rev-list", "--parents", "-n", "1", input_["merge_commit"]).decode().split()[1:]
            assert input_["commit"] in parents, input_["name"]
    engine = CODE / ENGINE["path"]
    for input_ in ENGINE["accepted_inputs"]:
        git("merge-base", "--is-ancestor", input_["commit"], ENGINE["commit"], cwd=engine)
        if "merge_commit" in input_:
            git("merge-base", "--is-ancestor", input_["merge_commit"], ENGINE["commit"], cwd=engine)
            assert input_["commit"] in git("rev-list", "--parents", "-n", "1", input_["merge_commit"], cwd=engine).decode().split()[1:]
    for fix in ENGINE["integration_fixes"]:
        assert fix["commit"] == ENGINE["commit"]


def test_the_role_source_repository_is_the_receipt_commit_and_never_a_code_component():
    roles = legacy_roles_source()
    expected = RECEIPT["components"]["roles"]["commit"]
    # The role repository is separate and its source checkout stays where its owner left it; the receipt commit is checked in the
    # repository's own integration branch (a ref, not a floating checkout).
    assert git("merge-base", "--is-ancestor", expected, "refs/heads/" + RECEIPT["components"]["roles"]["branch"], cwd=roles) == b""
    assert git("merge-base", "--is-ancestor", RECEIPT["components"]["roles"]["base_commit"], expected, cwd=roles) == b""
    assert not (CODE / "qorqut-roles").exists()


def archive_hashes(commit):
    """``{relative path: sha256}`` of the engine package as a git archive of the exact pinned object."""
    payload = git("archive", "--format=tar", commit, "qorqut_operations", cwd=CODE / ENGINE["path"])
    result = {}
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                result[member.name] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
    return result


def test_the_installed_engine_is_exactly_the_pinned_source_and_not_a_live_checkout():
    installed = Path(qorqut_operations.__file__).resolve().parent
    assert not installed.is_relative_to(CODE), "the engine must be an installed artifact, not the live child checkout"
    distribution = importlib.metadata.distribution(ENGINE["distribution"])
    assert distribution.version == ENGINE["version"]
    direct = distribution.read_text("direct_url.json")
    assert direct is None or not json.loads(direct).get("dir_info", {}).get("editable")

    pinned = archive_hashes(ENGINE["commit"])
    present = {path.relative_to(installed.parent).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in installed.rglob("*") if path.is_file() and "__pycache__" not in path.parts}
    assert present == pinned


def test_the_engine_imports_on_its_own_without_any_host_module():
    program = (
        "import importlib, pkgutil, sys, qorqut_operations\n"
        "for module in pkgutil.walk_packages(qorqut_operations.__path__, 'qorqut_operations.'):\n"
        "    importlib.import_module(module.name)\n"
        "host = sorted(name for name in sys.modules if name.split('.')[0] in {'bazarlyk', 'biz', 'bizdash'})\n"
        "assert not host, host\n"
        "print('engine-only-ok')\n")
    result = subprocess.run([sys.executable, "-I", "-c", program], cwd="/", capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "engine-only-ok" in result.stdout, result.stdout + result.stderr


REMOVED = ("application/__init__.py", "application/batches.py", "application/engine.py", "application/ports.py",
           "application/registry.py", "business_lifecycle.py", "contracts.py", "local_time.py", "policy/__init__.py",
           "policy/resolver.py", "signoffs/execution.py", "storage/database.py", "storage/files.py",
           "storage/receipts.py", "storage/repositories.py", "storage/safe_io.py")
REMOVED_MODULES = tuple("bazarlyk." + name[:-3].replace("/", ".").removesuffix(".__init__") for name in REMOVED)


def test_the_hosts_moved_engine_modules_are_gone_and_nothing_imports_them():
    package = APP / "bazarlyk"
    assert not [name for name in REMOVED if (package / name).exists()]
    for name in REMOVED_MODULES:
        try:
            spec = importlib.util.find_spec(name)
        except ModuleNotFoundError:  # the removed package itself is gone
            spec = None
        assert spec is None, name
    pattern = re.compile(r"^\s*(?:from\s+(?P<from>[\w.]+)\s+import|import\s+(?P<import>[\w.]+))", re.MULTILINE)
    offenders = []
    for source in package.rglob("*.py"):
        if "release_worker" in source.parts:
            # Deferred (remaining S2/S3): its migration probe is a script string run by a frozen candidate's own
            # interpreter, not an import of this host.
            continue
        for match in pattern.finditer(source.read_text()):
            module = match.group("from") or match.group("import")
            if any(module == removed or module.startswith(removed + ".") for removed in REMOVED_MODULES):
                offenders.append(f"{source.relative_to(APP)}: {module}")
    assert offenders == []


def test_one_engine_authority_one_database_and_no_second_policy_evaluator():
    engine_classes = {"Application", "Database", "PolicyResolver", "FileApplier", "OperationRegistry"}
    defined = []
    definition = re.compile(r"^class\s+(\w+)", re.MULTILINE)
    for source in (APP / "bazarlyk").rglob("*.py"):
        defined += [f"{source.relative_to(APP)}:{name}" for name in definition.findall(source.read_text())
                    if name in engine_classes]
    assert defined == []
    # The host reaches the engine's classes only through the engine package.
    for name in ("Application", "Database", "PolicyResolver", "FileApplier"):
        module = importlib.import_module({"Application": "qorqut_operations.application.engine",
                                          "Database": "qorqut_operations.storage.database",
                                          "PolicyResolver": "qorqut_operations.policy",
                                          "FileApplier": "qorqut_operations.storage.files"}[name])
        assert getattr(module, name).__module__.startswith("qorqut_operations")
