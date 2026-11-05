"""Authorized file sources for outbound Telegram files and photos, against the real runtime.

One synthetic host (two businesses, the registered Ordubashy and a second Global agent, the real canonical readers, document
store, policy and explicit instance paths) under a pytest temporary directory. Documents are registered rows plus files so
every kind can be tried. No operational data, provider, model, native agent or network is used."""
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.application.plans import schema
from qorqut_operations.contracts import DomainError, Principal

from bazarlyk.file_viewer import files
from bazarlyk.instance_paths import InstancePaths
from bazarlyk.telegram import media_sources as ms
from bazarlyk.telegram.media_sources import (EXPECTED_SHA256_SCHEMA, SOURCE_SCHEMA, AuthorizedSource, FileSources,
                                             normalize_workspace_path, parse_source)
from bazarlyk.telegram.media_validation import validate_filename
from host_foundation_support import owner_global_context, set_agent_overrides
from telegram_media_source_support import (BUSINESS_NOTE, agent_context, build_host, register_document, set_archived,
                                           set_field)

UUID_A, UUID_B = "abcdef01-1111-4111-8111-111111111111", "22222222-2222-4222-8222-22222222222b"
PDF = b"%PDF-1.4 synthetic report"


@pytest.fixture(scope="module")
def shared(tmp_path_factory):
    host = build_host(tmp_path_factory.mktemp("media-sources"))
    yield host
    host.runtime.stop()


@pytest.fixture
def fresh(tmp_path):
    host = build_host(tmp_path)
    yield host
    host.runtime.stop()


def code(call, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        call(*args, **kwargs)
    return caught.value.code


def sources_of(host):
    return FileSources(host.runtime)


def authorize(host, source, *, agent=None, ctx=None, sources=None):
    ctx = ctx or agent_context(host, agent or host.ordubashy)
    with host.database.read() as uow:
        return (sources or sources_of(host)).authorize(ctx, source, uow)


def recheck(host, binding, *, agent=None, ctx=None, sources=None):
    ctx = ctx or agent_context(host, agent or host.ordubashy)
    with host.database.read() as uow:
        return (sources or sources_of(host)).recheck(ctx, binding, uow)


def authorize_code(host, source, **kwargs):
    return code(authorize, host, source, **kwargs)


def recheck_code(host, binding, **kwargs):
    return code(recheck, host, binding, **kwargs)


def workspace_source(path):
    return {"type": "workspace", "path": path}


def document_source(document, business="unset"):
    return {"type": "document", "document_id": document.id,
            "business_id": document.business_id if business == "unset" else business}


def workspace(host, agent=None):
    return host.paths.agent_workspace(agent or host.ordubashy)


def put(host, relative, data=PDF, agent=None):
    path = workspace(host, agent) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def read_all(host, authorized, *, limit=1_000_000, expected_sha256=None):
    sink = io.BytesIO()
    with sources_of(host).open_source(authorized) as opened:
        copied = opened.copy_to(sink, limit=limit, expected_sha256=expected_sha256)
    return copied, sink.getvalue()


# ----- schema and lexical rules --------------------------------------------------------------------------------------


def test_the_source_schema_is_closed_and_a_valid_operation_input():
    validator = Draft202012Validator(SOURCE_SCHEMA)
    Draft202012Validator.check_schema(SOURCE_SCHEMA)
    Draft202012Validator.check_schema(EXPECTED_SHA256_SCHEMA)
    good = [{"type": "workspace", "path": "exports/a.pdf"}, {"type": "document", "document_id": UUID_A, "business_id": UUID_B},
            {"type": "document", "document_id": UUID_A, "business_id": None}]
    bad = [{}, {"type": "workspace"}, {"type": "workspace", "path": "exports/a.pdf", "root": "/"},
           {"type": "workspace", "path": 5}, {"type": "workspace", "path": ""}, {"type": "document", "document_id": UUID_A},
           {"type": "document", "document_id": UUID_A.upper(), "business_id": None},
           {"type": "document", "document_id": UUID_A, "business_id": "global"},
           {"type": "document", "document_id": UUID_A, "business_id": None, "path": "x"},
           {"type": "url", "url": "https://example.com/a"}, {"type": "document", "path": "exports/a.pdf"}, "exports/a.pdf", None]
    assert all(validator.is_valid(value) for value in good) and not any(validator.is_valid(value) for value in bad)
    digest = Draft202012Validator(EXPECTED_SHA256_SCHEMA)
    assert digest.is_valid("a" * 64) and not any(digest.is_valid(value) for value in ("A" * 64, "a" * 63, "a" * 65, "x" * 64, None, 5))
    # The pieces compose into a registered closed operation schema.
    descriptor = OperationDescriptor(
        "send_file_schema_probe", schema({"source": SOURCE_SCHEMA, "expected_sha256": EXPECTED_SHA256_SCHEMA}, ("source", "expected_sha256")),
        supports_signoff=False, prepare=lambda *a: None, apply=lambda *a: None)
    assert descriptor.input_schema["properties"]["source"]["oneOf"]


def test_parse_source_returns_the_closed_normalized_form_and_refuses_everything_else():
    assert parse_source({"type": "workspace", "path": "exports/a.pdf"}) == {"type": "workspace", "path": "exports/a.pdf"}
    assert parse_source({"type": "document", "document_id": UUID_A, "business_id": None}) == {
        "type": "document", "document_id": UUID_A, "business_id": None}
    for bad in (None, "exports/a.pdf", [], {}, {"type": "file"}, {"type": "workspace"}, {"type": "workspace", "path": "exports/a", "x": 1},
                {"type": "document", "document_id": UUID_A}, {"type": "document", "business_id": None},
                {"type": "document", "document_id": UUID_A.upper(), "business_id": None},
                {"type": "document", "document_id": "{" + UUID_A + "}", "business_id": None},
                {"type": "document", "document_id": UUID_A, "business_id": UUID_B.upper()},
                {"type": "document", "document_id": UUID_A, "business_id": ""},
                {"type": "document", "document_id": UUID_A, "business_id": None, "path": "exports/a"}):
        assert code(parse_source, bad) == "validation_error", bad
    assert ms.validate_expected_sha256("0" * 64) == "0" * 64
    assert code(ms.validate_expected_sha256, "0" * 63) == "validation_error"


def test_workspace_paths_are_lexically_confined_to_exports_and_downloads():
    for good in ("exports/a.pdf", "downloads/abc123/report.csv", "exports/deep/er/file.txt", "exports/Hesabat Əlavə 📎.pdf",
                 "exports/100% done.pdf", "exports/a b (1).pdf", "downloads/x/文件.png", "exports/é.txt", "exports/a" + "x" * 240):
        assert normalize_workspace_path(good) == good
    for bad in ("/exports/a.pdf", "/etc/passwd", "exports/../a.pdf", "../exports/a.pdf", "exports/./a.pdf", "./exports/a.pdf",
                "exports//a.pdf", "exports/", "exports", "downloads", "exports/a.pdf/", "exports\\a.pdf", "exports/a\\b.pdf",
                "exports/a\x00.pdf", "exports/a\nb.pdf", "exports/a\tb.pdf", "exports/a\x7f.pdf", "exports/‮txt.exe",
                "exports/.hidden", "exports/.secret/a.pdf", ".exports/a.pdf", "downloads/x/.receipt.json", "downloads/x/.upload.lock",
                "exports/a b.pdf", "files/a.pdf", "notes/a.md", "export/a.pdf", "Exports/a.pdf", "agents/x/exports/a.pdf",
                "businesses/acme/hq/a.pdf", "https://example.com/a.pdf", "file:///etc/passwd", "exports/" + "x" * 256,
                "exports/" + "ə" * 128, "exports/" + "a/" * 600 + "b", "exports/a\"b.pdf", "exports/ leading.pdf", "exports/trailing.pdf ",
                "exports/..", "exports/a/..", "exports/\ud800.pdf", 5, None, b"exports/a.pdf", ""):
        assert code(normalize_workspace_path, bad) == "validation_error", repr(bad)
    # The upload file name contract alone accepts these final names; a workspace path stays stricter.
    for name in (" leading.pdf", "trailing.pdf ", "fdp.\u202eexe", "a\u2066b.pdf"):
        assert validate_filename(name) == name
        assert code(normalize_workspace_path, f"exports/{name}") == "validation_error", repr(name)


# ----- workspace sources -----------------------------------------------------------------------------------------------


def test_workspace_authorization_names_the_resolved_workspace_and_reads_no_file(shared, monkeypatch):
    def forbidden_read(*args, **kwargs):
        raise AssertionError("authorize read a file")

    monkeypatch.setattr(files, "open_regular", forbidden_read)
    monkeypatch.setattr(type(shared.runtime.domains.documents.store), "read_bytes", forbidden_read)
    # Nothing exists at this path: authorization is about authority and location, not the file.
    authorized = authorize(shared, workspace_source("exports/not-yet.pdf"))
    assert isinstance(authorized, AuthorizedSource) and authorized.kind == "workspace"
    assert authorized.root == str(workspace(shared)) and authorized.relative_path == "exports/not-yet.pdf"
    assert authorized.filename == "not-yet.pdf"
    assert authorized.binding == {"version": 1, "type": "workspace", "principal_id": shared.ordubashy, "scope": {"kind": "global"},
                                  "workspace": f"agents/{shared.ordubashy}", "workspace_root": str(workspace(shared)),
                                  "path": "exports/not-yet.pdf"}
    assert json.loads(json.dumps(authorized.binding)) == authorized.binding
    with pytest.raises(dataclasses.FrozenInstanceError):
        authorized.root = "/"
    unicode_name = "downloads/ab12/Hesabat Əlavə 📎.pdf"
    assert authorize(shared, workspace_source(unicode_name)).filename == "Hesabat Əlavə 📎.pdf"


def test_a_malformed_source_is_refused_before_any_authority_is_consulted(shared):
    assert authorize_code(shared, {"type": "workspace", "path": "../x"}) == "validation_error"
    assert authorize_code(shared, {"type": "document", "document_id": "nope", "business_id": None}) == "validation_error"
    assert authorize_code(shared, "exports/a.pdf") == "validation_error"


def test_a_workspace_file_is_opened_copied_hashed_and_bounded(shared):
    put(shared, "exports/report.pdf")
    authorized = authorize(shared, workspace_source("exports/report.pdf"))
    digest = hashlib.sha256(PDF).hexdigest()
    copied, data = read_all(shared, authorized, expected_sha256=digest)
    assert (copied.size, copied.sha256, data) == (len(PDF), digest, PDF)
    with sources_of(shared).open_source(authorized) as opened:
        assert opened.size == len(PDF) and opened.filename == "report.pdf"
        sink = io.BytesIO()
        assert code(opened.copy_to, sink, limit=len(PDF) - 1) == "file_too_large"
        assert code(opened.copy_to, io.BytesIO(), limit=len(PDF), expected_sha256="0" * 64) == "checksum_mismatch"
        assert code(opened.copy_to, io.BytesIO(), limit=len(PDF), expected_sha256="NOT-HEX") == "validation_error"
        # A second copy from the same descriptor starts from the beginning again.
        assert opened.copy_to(io.BytesIO(), limit=len(PDF), chunk_size=3).sha256 == digest
        with pytest.raises(ValueError):
            opened.copy_to(io.BytesIO(), limit=0)
    put(shared, "exports/empty.pdf", b"")
    with sources_of(shared).open_source(authorize(shared, workspace_source("exports/empty.pdf"))) as opened:
        assert code(opened.copy_to, io.BytesIO(), limit=100) == "file_empty"
    assert code(read_all, shared, authorize(shared, workspace_source("exports/absent.pdf"))) == "not_found"


def test_a_file_that_grows_during_the_copy_is_never_read_past_its_opened_size(shared):
    path = put(shared, "exports/grows.bin", b"x" * 64)
    authorized = authorize(shared, workspace_source("exports/grows.bin"))
    with sources_of(shared).open_source(authorized) as opened:
        class Growing(io.BytesIO):
            grown = False

            def write(self, data):
                if not self.grown:
                    self.grown = True
                    with path.open("ab") as extra:
                        extra.write(b"y" * 100_000)
                return super().write(data)

        sink = Growing()
        assert code(opened.copy_to, sink, limit=100, chunk_size=16) == "source_changed"
        assert len(sink.getvalue()) <= 64, "Copy never passes on more than the size the file had when opened"
    path.write_bytes(b"x" * 64)


def test_an_edit_during_the_copy_is_detected_and_never_sealed(shared):
    path = put(shared, "exports/edited.bin", b"a" * 64)
    authorized = authorize(shared, workspace_source("exports/edited.bin"))

    def during(edit):
        with sources_of(shared).open_source(authorized) as opened:
            class Editing(io.BytesIO):
                done = False

                def write(self, data):
                    if not self.done:
                        self.done = True
                        edit()
                    return super().write(data)

            return code(opened.copy_to, Editing(), limit=1000, chunk_size=16)

    def shrink():
        with path.open("r+b") as stream:
            stream.truncate(8)

    def rewrite_same_size():
        stat = path.stat()
        path.write_bytes(b"b" * 64)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    assert during(shrink) == "source_changed"
    path.write_bytes(b"a" * 64)
    assert during(rewrite_same_size) == "source_changed"
    path.write_bytes(b"a" * 64)
    # Replacing the name while the old file is open also changes the opened file's inode times: it is reported, not sealed.
    with sources_of(shared).open_source(authorized) as opened:
        time.sleep(0.05)  # Past the filesystem's timestamp granularity, so the unlink's inode change time is distinguishable.
        replacement = path.with_name("replacement.bin")
        replacement.write_bytes(b"z" * 64)
        os.replace(replacement, path)
        assert code(opened.copy_to, io.BytesIO(), limit=1000) == "source_changed"
    # A replacement made before the file is opened is simply the file that is read.
    assert read_all(shared, authorized)[1] == b"z" * 64
    path.write_bytes(b"a" * 64)


def test_workspace_refuses_symlinks_special_files_directories_and_hardlinks(shared, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    secret = outside / "secret.txt"
    secret.write_text("private")
    base = put(shared, "exports/regular.pdf").parent
    (base / "link-final.pdf").symlink_to(secret)
    (base / "link-dir").symlink_to(outside, target_is_directory=True)
    (base / "subdir").mkdir(exist_ok=True)
    if not (base / "pipe").exists():
        os.mkfifo(base / "pipe")
    if not (base / "second.pdf").exists():
        os.link(base / "regular.pdf", base / "second.pdf")
    if not (base / "outside-link.txt").exists():
        os.link(secret, base / "outside-link.txt")

    def opened_code(path):
        # The lexical rules and the authority accept every one of these; only the opener sees the filesystem.
        return code(read_all, shared, authorize(shared, workspace_source(path)))

    assert opened_code("exports/link-final.pdf") == "symlink_not_followed"
    assert opened_code("exports/link-dir/secret.txt") == "symlink_not_followed"
    assert opened_code("exports/subdir") == "not_a_regular_file"
    assert opened_code("exports/pipe") == "not_a_regular_file"
    assert opened_code("exports/regular.pdf") == "source_hardlinked", "second.pdf is another name for the same file"
    assert opened_code("exports/second.pdf") == "source_hardlinked"
    assert opened_code("exports/outside-link.txt") == "source_hardlinked"
    assert secret.read_text() == "private"


def test_a_symlinked_subtree_root_or_a_swapped_ancestor_is_refused_at_open(shared, tmp_path_factory):
    outside = tmp_path_factory.mktemp("swap-outside")
    (outside / "file.txt").write_text("outside")
    put(shared, "downloads/swap/file.txt", b"inside")
    put(shared, "exports/regular.pdf")
    authorized = authorize(shared, workspace_source("downloads/swap/file.txt"))
    assert read_all(shared, authorized)[1] == b"inside"
    swap = workspace(shared) / "downloads" / "swap"
    (workspace(shared) / "downloads" / "swap-moved").mkdir(exist_ok=True)
    os.replace(swap, workspace(shared) / "downloads" / "swap-moved" / "swap")
    swap.symlink_to(outside, target_is_directory=True)
    # Authorized before the swap; the descriptor-relative no-follow walk still refuses the link.
    assert code(read_all, shared, authorized) == "symlink_not_followed"
    swap.unlink()
    exports = workspace(shared) / "exports"
    exports_real = workspace(shared) / "exports-real"
    os.replace(exports, exports_real)
    exports.symlink_to(exports_real, target_is_directory=True)
    assert code(read_all, shared, authorize(shared, workspace_source("exports/regular.pdf"))) == "symlink_not_followed"
    exports.unlink()
    os.replace(exports_real, exports)


@contextlib.contextmanager
def racing(monkeypatch, name, action):
    """Run ``action`` once, immediately before ``os.open`` is asked for the component ``name`` below a directory descriptor.

    That is the exact window between an authorization or a path check and the open it was meant to protect."""
    real, fired = os.open, []

    def open_after(path, flags, *args, **kwargs):
        if path == name and kwargs.get("dir_fd") is not None and not fired:
            fired.append(path)
            action()
        return real(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_after)
        yield fired


def swapped_for_a_link(target, replacement):
    """``(swap, restore)``: replace the directory ``target`` by a symlink to ``replacement``, and put it back."""
    moved = target.with_name(f"{target.name}-moved")

    def swap():
        os.rename(target, moved)
        os.symlink(replacement, target, target_is_directory=True)

    def restore():
        os.unlink(target)
        os.rename(moved, target)

    return swap, restore


def other_tree(base, target, relative, data):
    """A directory shaped like ``target`` for the file at ``relative`` below it, holding ``data`` instead."""
    replacement = base / f"other-{target.name}"
    (replacement / relative).parent.mkdir(parents=True)
    (replacement / relative).write_bytes(data)
    return replacement


@pytest.mark.parametrize("depth", [0, 1], ids=["exact_root", "absolute_ancestor"])
def test_a_root_or_ancestor_swapped_between_authorization_and_open_is_refused_never_read(fresh, tmp_path_factory, monkeypatch, depth):
    host = fresh
    put(host, "exports/raced.txt", b"authorized tree")
    authorized = authorize(host, workspace_source("exports/raced.txt"))
    root = Path(authorized.root)
    target = root if depth == 0 else root.parent
    assert Path(authorized.root).parts.count(target.name) == 1, "The race must name one unambiguous component"
    replacement = other_tree(tmp_path_factory.mktemp("race"), target, root.relative_to(target) / "exports/raced.txt", b"OTHER TREE")
    swap, restore = swapped_for_a_link(target, replacement)
    try:
        with racing(monkeypatch, target.name, swap) as fired:
            assert code(read_all, host, authorized) == "symlink_not_followed"
        assert fired, "The swap must have happened inside the open"
    finally:
        if target.is_symlink():
            restore()
    assert read_all(host, authorized)[1] == b"authorized tree", "Restored, the authorized file is read again"


def test_every_absolute_ancestor_of_the_root_is_opened_without_following_a_swapped_link(tmp_path_factory, monkeypatch):
    base = tmp_path_factory.mktemp("deep")
    root = base / "level1" / "level2" / "level3" / "workspace"
    (root / "exports").mkdir(parents=True)
    (root / "exports" / "file.txt").write_bytes(b"authorized tree")
    authorized = AuthorizedSource("workspace", str(root), "exports/file.txt", "file.txt", {})
    sources = FileSources(SimpleNamespace(application=None))  # Opening a file consults no authority.

    def read():
        sink = io.BytesIO()
        with sources.open_source(authorized) as opened:
            opened.copy_to(sink, limit=1000)
        return sink.getvalue()

    assert read() == b"authorized tree"
    for target in (root, root.parent, root.parent.parent, root.parent.parent.parent, base):
        replacement = other_tree(tmp_path_factory.mktemp("elsewhere"), target, root.relative_to(target) / "exports/file.txt", b"OTHER TREE")
        swap, restore = swapped_for_a_link(target, replacement)
        try:
            with racing(monkeypatch, target.name, swap) as fired:
                assert code(read) == "symlink_not_followed", target
            assert fired, target
        finally:
            if target.is_symlink():
                restore()
        assert read() == b"authorized tree", target


def test_a_root_swapped_after_it_was_anchored_cannot_redirect_the_walk(fresh, tmp_path_factory, monkeypatch):
    host = fresh
    put(host, "exports/anchored.txt", b"authorized tree")
    authorized = authorize(host, workspace_source("exports/anchored.txt"))
    root = Path(authorized.root)
    replacement = other_tree(tmp_path_factory.mktemp("anchored"), root, "exports/anchored.txt", b"OTHER TREE")
    swap, restore = swapped_for_a_link(root, replacement)
    try:
        # The swap happens as the first relative component is opened: the root directory is already held by descriptor.
        with racing(monkeypatch, "exports", swap) as fired:
            _, data = read_all(host, authorized)
        assert fired and root.is_symlink()
        assert data == b"authorized tree", "The walk stays on the authorized directory it had anchored"
    finally:
        if root.is_symlink():
            restore()


def test_no_descriptor_leaks_when_an_open_fails_or_is_refused(shared, tmp_path_factory, monkeypatch):
    base = put(shared, "exports/fd-check/regular.txt", b"data").parent
    (base / "dir").mkdir(exist_ok=True)
    outside = tmp_path_factory.mktemp("fd-outside")
    (outside / "x.txt").write_text("x")
    (base / "link.txt").symlink_to(outside / "x.txt")
    (base / "linked-dir").symlink_to(outside, target_is_directory=True)
    if not (base / "pipe").exists():
        os.mkfifo(base / "pipe")
    os.link(base / "regular.txt", base / "second.txt") if not (base / "second.txt").exists() else None
    paths = ["exports/fd-check/link.txt", "exports/fd-check/linked-dir/x.txt", "exports/fd-check/dir", "exports/fd-check/pipe",
             "exports/fd-check/absent.txt", "exports/fd-check/absent-dir/a.txt", "exports/fd-check/regular.txt",
             "exports/fd-check/regular.txt/below-a-file"]

    def descriptors():
        return len(os.listdir("/proc/self/fd"))

    def attempts():
        for path in paths:
            try:
                read_all(shared, authorize(shared, workspace_source(path)))
            except DomainError:
                pass

    attempts()  # Warm any lazy imports or caches before counting.
    before = descriptors()
    attempts()
    forged = AuthorizedSource("workspace", f"{workspace(shared)}/no-such-root", "exports/a.txt", "a.txt", {})
    for _ in range(3):
        assert code(read_all, shared, forged) == "not_found"
    # A failure injected midway (after some directories were opened) closes the ones already held.
    real, calls = os.open, []

    def failing(path, flags, *args, **kwargs):
        calls.append(path)
        if path == "exports":
            raise PermissionError(13, "denied")
        return real(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", failing)
        assert code(read_all, shared, authorize(shared, workspace_source("exports/fd-check/regular.txt"))) == "file_unreadable"
    assert "exports" in calls
    assert descriptors() == before


def test_open_source_checks_its_root_and_its_input(shared, tmp_path_factory):
    real = put(shared, "exports/rooted.pdf")
    authorized = authorize(shared, workspace_source("exports/rooted.pdf"))
    sources = sources_of(shared)

    def entered(candidate):
        with sources.open_source(candidate):
            pass

    with pytest.raises(TypeError):
        entered({"root": authorized.root})
    forged = {"kind": "workspace", "filename": "x", "binding": authorized.binding}
    for malformed in ("relative/root", "", "/", "//double", f"{authorized.root}/../x", f"{authorized.root}/./", f"{authorized.root}/"):
        assert code(entered, AuthorizedSource(root=malformed, relative_path="exports/rooted.pdf", **forged)) == "invalid_configuration", malformed
    link_root = tmp_path_factory.mktemp("link") / "ws"
    link_root.symlink_to(workspace(shared), target_is_directory=True)
    assert code(entered, AuthorizedSource(root=str(link_root), relative_path="exports/rooted.pdf", **forged)) == "symlink_not_followed"
    for relative in ("../outside.txt", "exports/../../x", "/etc/passwd", "exports//a", ""):
        assert code(entered, AuthorizedSource(root=authorized.root, relative_path=relative, **forged)) == "invalid_configuration", relative
    missing_root = AuthorizedSource(root=str(workspace(shared) / "no-such-root"), relative_path="exports/a.pdf", **forged)
    assert code(entered, missing_root) == "not_found"
    entered(authorized)
    assert real.exists()


def test_an_agent_reaches_only_its_own_workspace(shared, tmp_path_factory):
    private = put(shared, "exports/ordubashy-only.pdf", b"ordubashy")
    other_file = put(shared, "exports/other-only.pdf", b"other", agent=shared.other)
    mine = authorize(shared, workspace_source("exports/other-only.pdf"), agent=shared.other)
    assert mine.root == str(workspace(shared, shared.other)) and read_all(shared, mine)[1] == b"other"
    # The same relative name never reaches another agent's workspace, and traversal into one is a lexical refusal.
    assert code(read_all, shared, authorize(shared, workspace_source("exports/other-only.pdf"))) == "not_found"
    assert authorize_code(shared, workspace_source(f"../{shared.other}/exports/other-only.pdf")) == "validation_error"
    assert authorize_code(shared, workspace_source(f"/{other_file}")) == "validation_error"
    # A link inside one's own workspace to another agent's file is a symlink, refused by the opener.
    (workspace(shared) / "exports" / "to-other.pdf").symlink_to(other_file)
    assert code(read_all, shared, authorize(shared, workspace_source("exports/to-other.pdf"))) == "symlink_not_followed"
    assert private.read_bytes() == b"ordubashy"


def test_a_workspace_path_that_is_a_registered_document_must_use_its_document_source(shared):
    note = register_document(shared, kind="agent_note", path=f"agents/{shared.ordubashy}/exports/registered-note.md",
                             data=b"# private note\n", owner=shared.ordubashy, metadata={"title": "t"})
    assert note.file.is_file()
    assert authorize_code(shared, workspace_source("exports/registered-note.md")) == "forbidden"
    assert authorize(shared, document_source(note)).kind == "document"


def test_workspace_recheck_rederives_authority_without_the_file(shared):
    path = put(shared, "exports/recheck.pdf")
    authorized = authorize(shared, workspace_source("exports/recheck.pdf"))
    binding = json.loads(json.dumps(authorized.binding))
    assert recheck(shared, binding).binding == authorized.binding
    path.unlink()
    assert recheck(shared, binding).binding == authorized.binding, "A removed generated file does not revoke the sealed snapshot"
    # Another principal's context cannot recheck this binding.
    assert recheck_code(shared, binding, agent=shared.other) == "source_changed"
    # Every declared fact must still be what authority derives now.
    for stale in ({**binding, "principal_id": shared.other}, {**binding, "workspace": "agents/ordubashy"},
                  {**binding, "workspace_root": str(Path(binding["workspace_root"]).parent / "elsewhere")},
                  {**binding, "scope": {"kind": "business", "business_id": shared.acme}}):
        assert recheck_code(shared, stale) == "source_changed", stale
    assert recheck(shared, {**binding, "path": "downloads/x/other.pdf"}).relative_path == "downloads/x/other.pdf"
    for broken in (None, {}, {**binding, "version": 2}, {**binding, "extra": 1}, {k: v for k, v in binding.items() if k != "path"},
                   {**binding, "type": "url"}, {**binding, "path": "../x"}, {**binding, "scope": "global"},
                   {k: v for k, v in binding.items() if k != "workspace_root"}, {**binding, "workspace_root": None},
                   {**binding, "workspace_root": 5}, {**binding, "workspace_root": "relative/workspace"},
                   {**binding, "workspace_root": "/a/../b"}, {**binding, "workspace_root": "/a//b"},
                   {**binding, "workspace_root": "//a/b"}, {**binding, "workspace_root": "/"},
                   {**binding, "workspace_root": binding["workspace_root"] + "/"}, {**binding, "workspace": 5}):
        assert recheck_code(shared, broken) == "validation_error", broken


def test_the_workspaces_canonical_location_is_part_of_what_was_authorized(fresh, tmp_path_factory):
    host = fresh
    path = put(host, "exports/relocated.pdf")
    before = authorize(host, workspace_source("exports/relocated.pdf"))
    assert before.binding["workspace_root"] == str(workspace(host)) == before.root
    assert json.loads(json.dumps(before.binding)) == before.binding
    # The same installation, principal and relative folder under another instance root is a different workspace.
    other_paths = InstancePaths(tmp_path_factory.mktemp("relocated-instance"), {})
    relocated = FileSources(SimpleNamespace(application=host.runtime.application, domains=host.runtime.domains, paths=other_paths))
    after = authorize(host, workspace_source("exports/relocated.pdf"), sources=relocated)
    assert after.binding["principal_id"] == before.binding["principal_id"] and after.binding["workspace"] == before.binding["workspace"]
    assert after.binding["workspace_root"] == str(other_paths.agent_workspace(host.ordubashy)) != before.binding["workspace_root"]
    assert recheck_code(host, before.binding, sources=relocated) == "source_changed"
    assert recheck_code(host, after.binding) == "source_changed"
    # Neither the file nor its inode is part of that authority: the sealed source survives its original being removed or edited.
    assert recheck(host, before.binding).binding == before.binding
    path.write_bytes(b"edited after the seal")
    assert recheck(host, before.binding).binding == before.binding
    path.unlink()
    assert recheck(host, before.binding).binding == before.binding
    # An unrelated profile revision (a permission override) does not change the authorized location either.
    set_agent_overrides(host.instance, host.ordubashy, {"assets_get": "allow"}, (host.acme, host.beta))
    assert recheck(host, before.binding).binding == before.binding


def test_the_named_global_workspace_mapping_is_the_identity_and_rebinding_cancels_recheck(fresh):
    host = fresh
    put(host, "exports/mapped.txt", b"by uuid")
    before = authorize(host, workspace_source("exports/mapped.txt"))
    assert before.binding["workspace"] == f"agents/{host.ordubashy}"
    config = host.paths.instance_root / "config"
    config.mkdir(parents=True, exist_ok=True)
    mapping = config / "global-agent-workspaces.json"
    named = host.paths.operations / "agents" / "ordubashy"

    def register_workspace(path):
        with host.database.transaction() as uow:
            uow.connection.execute("UPDATE agent_profiles SET workspace=? WHERE agent_id=?", (str(path), host.ordubashy))

    # The registered identity is mapped to a readable folder, as the four fixed global registrations are.
    mapping.write_text(json.dumps({"version": 1, "agents": {host.ordubashy: "ordubashy"}}))
    register_workspace(named)
    (named / "exports").mkdir(parents=True)
    (named / "exports" / "mapped.txt").write_bytes(b"by name")
    after = authorize(host, workspace_source("exports/mapped.txt"))
    assert after.root == str(named) and after.binding["workspace"] == "agents/ordubashy"
    assert read_all(host, after)[1] == b"by name"
    # A queued send authorized under the old location is cancelled by the rebinding, not redirected.
    assert recheck_code(host, before.binding) == "source_changed"
    assert recheck(host, after.binding).binding == after.binding
    mapping.unlink()
    register_workspace(workspace(host))
    assert recheck_code(host, after.binding) == "source_changed"
    assert recheck(host, before.binding).binding == before.binding
    # A directory called "ordubashy" is never an identity: without the mapping the identity folder is used.
    assert authorize(host, workspace_source("exports/mapped.txt")).root == str(workspace(host))


def test_a_symlinked_or_invalid_workspace_root_fails_authorization_and_recheck(fresh):
    host = fresh
    put(host, "exports/a.txt")
    authorized = authorize(host, workspace_source("exports/a.txt"))
    real = workspace(host)
    moved = real.with_name("moved-workspace")
    os.replace(real, moved)
    real.symlink_to(moved, target_is_directory=True)
    assert authorize_code(host, workspace_source("exports/a.txt")) == "invalid_configuration"
    assert recheck_code(host, authorized.binding) == "invalid_configuration"
    real.unlink()
    os.replace(moved, real)
    config = host.paths.instance_root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "global-agent-workspaces.json").write_text(json.dumps({"version": 1, "agents": {host.ordubashy: "../escape"}}))
    assert authorize_code(host, workspace_source("exports/a.txt")) == "invalid_configuration"


def test_only_the_active_global_agent_itself_may_use_file_sources(fresh):
    host = fresh
    put(host, "exports/a.txt")
    owner = owner_global_context(host.instance)
    assert authorize_code(host, workspace_source("exports/a.txt"), ctx=owner) == "forbidden", "No owner impersonation"
    delegated = replace(agent_context(host, host.ordubashy), delegation_assignment_id=UUID_A, delegation_binding_id=UUID_B,
                        delegation_generation=1)
    assert authorize_code(host, workspace_source("exports/a.txt"), ctx=delegated) == "forbidden"
    unknown = replace(agent_context(host, host.ordubashy), principal=Principal(UUID_A, "agent"))
    assert authorize_code(host, workspace_source("exports/a.txt"), ctx=unknown) == "invalid_principal"
    authorized = authorize(host, workspace_source("exports/a.txt"))
    held = agent_context(host, host.ordubashy)  # A context obtained while the agent was still active.
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE principals SET active=0 WHERE id=?", (host.ordubashy,))
    assert authorize_code(host, workspace_source("exports/a.txt"), ctx=held) == "invalid_principal"
    assert recheck_code(host, authorized.binding, ctx=held) == "invalid_principal"
    assert authorize_code(host, workspace_source("exports/a.txt")) == "inactive_agent"


# ----- document sources: kinds and canonical readers ---------------------------------------------------------------------


def allowed_documents(host):
    """One registered document of every supported kind, with the scope and file a reader would present."""
    agent = host.ordubashy
    return {
        "asset": register_document(host, kind="asset", path="assets/polished/logo.png", data=b"\x89PNG-ish", business_id=host.acme,
                                   metadata={"filename": "logo.png"}),
        "data_upload": register_document(host, kind="data_upload", path="data-uploads/sales-2026.csv", data=b"a,b\n1,2\n",
                                         business_id=host.acme, metadata={"filename": "Sales 2026 (ə).csv"}),
        "global_agent_note": register_document(host, kind="agent_note", path=f"agents/{agent}/notes/mine.md", data=b"# mine\n",
                                               owner=agent, metadata={"title": "mine"}),
        "business_agent_note": register_document(host, kind="agent_note", path="agents/%s/notes/biz.md" % agent, data=b"# biz\n",
                                                 business_id=host.acme, owner=agent, metadata={"title": "biz"}),
        "business_note": register_document(host, kind=BUSINESS_NOTE, path="notes/strategy-notes/2026-10-19-plan.md",
                                           data=b"# strategy\n", business_id=host.acme,
                                           metadata={"title": "plan", "category": "strategy-notes"}),
        "content": register_document(host, kind="content", path="content/post.md", data=b"# post\n", business_id=host.acme,
                                     metadata={"title": "post"}),
        "context_note": register_document(host, kind="context_note", path="context/topic/note.md", data=b"# ctx\n",
                                          business_id=host.acme, metadata={"title": "ctx"}),
    }


@pytest.fixture(scope="module")
def documents(shared):
    return allowed_documents(shared)


READER = {"asset": "assets_get", "data_upload": "data_uploads_get", "global_agent_note": "notes_get",
          "business_agent_note": "notes_get", "business_note": "notes_get", "content": "content_get", "context_note": "content_get"}


@pytest.mark.parametrize("name", sorted(READER))
def test_every_supported_kind_authorizes_through_its_canonical_reader_and_is_read_from_its_registered_file(shared, documents, name):
    document = documents[name]
    authorized = authorize(shared, document_source(document))
    assert authorized.kind == "document" and authorized.relative_path == document.path
    slug = "acme" if document.business_id == shared.acme else "beta"
    # The registered path's first segment, not its kind, chooses the root: agents/ below the scope root, all else below hq.
    expected_root = (shared.paths.operations if document.business_id is None else shared.paths.business_root(slug)
                     if document.path.startswith("agents/") else shared.paths.business_hq(slug))
    assert authorized.root == str(expected_root)
    scope = {"kind": "global"} if document.business_id is None else {"kind": "business", "business_id": document.business_id}
    assert authorized.binding == {"version": 1, "type": "document", "principal_id": shared.ordubashy, "scope": scope,
                                  "document_id": document.id, "document_kind": document.kind, "reader": READER[name]}
    assert json.loads(json.dumps(authorized.binding)) == authorized.binding
    copied, data = read_all(shared, authorized)
    assert data == document.data and copied.sha256 == hashlib.sha256(document.data).hexdigest()
    assert authorized.filename == (os.path.basename(document.path) if name != "data_upload" else "Sales 2026 (ə).csv")
    assert recheck(shared, authorized.binding).binding == authorized.binding


def test_hidden_and_unsupported_kinds_are_refused_even_when_the_row_exists(shared):
    agent = shared.ordubashy
    # Owner journal kinds and working memory are hidden by the document store itself: indistinguishable from absent.
    for kind in ("journal", "global_note", "business_note"):
        row = register_document(shared, kind=kind, path=f"journal/{kind}-hidden.md", business_id=None if kind != "business_note" else shared.acme)
        assert authorize_code(shared, document_source(row)) == "not_found", kind
    memory = register_document(shared, kind="working_memory", path=f"agents/{agent}/memory.md", owner=agent)
    assert authorize_code(shared, document_source(memory)) == "not_found"
    # Visible kinds with no canonical file reader in the allowlist are an explicit refusal.
    for kind, path, business in (("plan", f"agents/{agent}/PLAN.md", None), ("context_topic", "context/topic/_topic.md", shared.acme),
                                 ("context_person", "context/people/p.md", shared.acme), ("context_upload", "context/_inbox/u.md", shared.acme),
                                 ("context_dump", "context/_inbox/d.md", shared.acme), ("context_map", "context/map.md", shared.acme),
                                 ("context", "context/c.md", shared.acme), ("pillar", "context/p.md", shared.acme),
                                 ("email_message", "email/m.md", shared.acme), ("shared_instruction", "shared/i.md", None),
                                 ("something-new", "misc/x.md", shared.acme),
                                 # The retired separate Personal Context has no special kind any more: a leftover row is just unsupported.
                                 ("personal_context", "personal/context.md", None)):
        row = register_document(shared, kind=kind, path=path, business_id=business, owner=agent if business is None and kind == "plan" else None)
        assert authorize_code(shared, document_source(row)) == "source_unsupported", kind


def test_the_document_scope_is_explicit_and_never_rescoped(shared, documents):
    asset, note = documents["asset"], documents["global_agent_note"]
    assert authorize_code(shared, document_source(asset, shared.beta)) == "not_found", "Another business"
    assert authorize_code(shared, document_source(asset, None)) == "not_found", "Global does not mean any business"
    assert authorize_code(shared, document_source(note, shared.acme)) == "not_found", "A Global document is not in a business"
    assert authorize_code(shared, document_source(asset, UUID_A)) == "not_found", "An unknown business"
    beta_asset = register_document(shared, kind="asset", path="assets/polished/beta.png", business_id=shared.beta,
                                   metadata={"filename": "beta.png"})
    assert authorize_code(shared, document_source(beta_asset, shared.acme)) == "not_found"
    assert authorize(shared, document_source(beta_asset)).binding["scope"] == {"kind": "business", "business_id": shared.beta}
    assert authorize_code(shared, {"type": "document", "document_id": UUID_B, "business_id": None}) == "not_found"


def test_global_documents_need_a_canonical_reader_that_exists_in_global_scope(shared):
    for kind, path in (("asset", "assets/global.png"), ("data_upload", "data-uploads/global.csv"),
                       ("content", "content/global.md"), ("context_note", "context/global.md")):
        row = register_document(shared, kind=kind, path=path, business_id=None)
        assert authorize_code(shared, document_source(row)) == "source_unsupported", f"{kind}: its reader is business-only"
    # notes_get exists in the Global scope: a Global agent note, including one owned by another agent when Global is readable.
    other_note = register_document(shared, kind="agent_note", path=f"agents/{shared.other}/notes/other.md", data=b"# other\n",
                                   owner=shared.other, metadata={"title": "other"})
    assert authorize(shared, document_source(other_note)).binding["reader"] == "notes_get"


def test_archived_unfinished_and_unreachable_documents_are_refused(fresh):
    host = fresh
    asset = register_document(host, kind="asset", path="assets/polished/a.png", business_id=host.acme, metadata={"filename": "a.png"})
    note = register_document(host, kind="agent_note", path=f"agents/{host.ordubashy}/notes/a.md", owner=host.ordubashy)
    set_archived(host, asset)
    set_archived(host, note)
    assert authorize_code(host, document_source(asset)) == "source_archived"
    assert authorize_code(host, document_source(note)) == "source_archived"
    set_archived(host, asset, False)
    assert authorize(host, document_source(asset)).kind == "document"
    busy = register_document(host, kind="asset", path="assets/polished/busy.png", business_id=host.acme, availability="applying")
    with pytest.raises(DomainError) as unfinished:
        authorize(host, document_source(busy))
    assert unfinished.value.code == "busy" and unfinished.value.retryable
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at='2026-10-19T00:00:00Z' WHERE id=?", (host.acme,))
    assert authorize_code(host, document_source(asset)) == "not_found", "An archived business is not reachable"


def test_canonical_reader_permissions_are_authoritative_for_each_kind(fresh):
    host = fresh
    docs = allowed_documents(host)
    both = {host.acme, host.beta}
    set_agent_overrides(host.instance, host.ordubashy, {"assets_get": "deny", "notes_get": "deny"}, both)
    assert authorize_code(host, document_source(docs["asset"])) == "policy_denied"
    for name in ("global_agent_note", "business_agent_note", "business_note"):
        assert authorize_code(host, document_source(docs[name])) == "policy_denied", name
    for name in ("data_upload", "content", "context_note"):
        assert authorize(host, document_source(docs[name])).binding["reader"] == READER[name], name
    set_agent_overrides(host.instance, host.ordubashy, {"assets_get": "allow", "notes_get": "allow", "content_get": "deny",
                                                         "data_uploads_get": "deny"}, both)
    assert authorize(host, document_source(docs["asset"])).kind == "document"
    assert authorize_code(host, document_source(docs["content"])) == "policy_denied"
    assert authorize_code(host, document_source(docs["context_note"])) == "policy_denied"
    assert authorize_code(host, document_source(docs["data_upload"])) == "policy_denied"


def test_a_missing_reader_or_one_not_exposed_to_agents_refuses_only_its_kind(fresh, monkeypatch):
    host = fresh
    docs = allowed_documents(host)
    registry = host.runtime.application.registry
    real_get = registry.get

    def without_data_uploads(name):
        if name == "data_uploads_get":
            raise DomainError("operation_unavailable", "Operation is not installed")
        return real_get(name)

    monkeypatch.setattr(registry, "get", without_data_uploads)
    assert authorize_code(host, document_source(docs["data_upload"])) == "source_unsupported"
    assert authorize(host, document_source(docs["asset"])).kind == "document"
    dashboard_only = dataclasses.replace(real_get("content_get"), exposure=frozenset({"dashboard"}))
    monkeypatch.setattr(registry, "get", lambda name: dashboard_only if name == "content_get" else real_get(name))
    assert authorize_code(host, document_source(docs["content"])) == "forbidden", "Exposure restrictions are not bypassed"
    # An owner-only descriptor cannot also be exposed to agents (the engine's catalog entry refuses that combination), so a
    # reader that agents may not use is always one without the agent exposure checked above.
    with pytest.raises(ValueError, match="Owner-only"):
        dataclasses.replace(real_get("assets_get"), owner_only=True)


def test_document_recheck_follows_authority_not_content_revision_or_location(fresh):
    host = fresh
    docs = allowed_documents(host)
    asset = docs["asset"]
    authorized = authorize(host, document_source(asset))
    binding = json.loads(json.dumps(authorized.binding))
    # Unrelated edits, a moved registration and a removed file do not revoke the accepted source.
    set_field(host, asset, "metadata_json", json.dumps({"filename": "logo.png", "notes": "edited"}))
    set_field(host, asset, "content_hash", "f" * 64)
    assert recheck(host, binding).binding == binding
    asset.file.unlink()
    assert recheck(host, binding).binding == binding
    set_field(host, asset, "path", "assets/polished/moved-logo.png")
    assert recheck(host, binding).relative_path == "assets/polished/moved-logo.png"
    # Lost authority does.
    set_archived(host, asset)
    assert recheck_code(host, binding) == "source_archived"
    set_archived(host, asset, False)
    assert recheck(host, binding).binding == binding
    assert recheck_code(host, binding, agent=host.other) == "source_changed"
    set_field(host, asset, "kind", "context_note")
    assert recheck_code(host, binding) == "source_changed", "The kind and its reader are part of what was authorized"
    set_field(host, asset, "kind", "journal")
    assert recheck_code(host, binding) == "not_found"
    set_field(host, asset, "kind", "plan")
    assert recheck_code(host, binding) == "source_unsupported"
    set_field(host, asset, "kind", "asset")
    assert recheck(host, binding).binding == binding
    with host.database.transaction() as uow:
        uow.connection.execute("DELETE FROM documents WHERE id=?", (asset.id,))
    assert recheck_code(host, binding) == "not_found"
    for broken in ({**binding, "document_id": "nope"}, {**binding, "reader": 5, "extra": 1}, {**binding, "scope": {"kind": "business"}}):
        assert recheck_code(host, broken) == "validation_error", broken


def test_document_recheck_stops_at_a_revoked_grant_or_an_archived_business(fresh):
    host = fresh
    docs = allowed_documents(host)
    both = {host.acme, host.beta}
    bindings = {name: authorize(host, document_source(docs[name])).binding for name in ("asset", "global_agent_note", "content")}
    set_agent_overrides(host.instance, host.ordubashy, {"assets_get": "deny"}, both)
    assert recheck_code(host, bindings["asset"]) == "policy_denied"
    assert recheck(host, bindings["content"]).binding == bindings["content"]
    assert recheck(host, bindings["global_agent_note"]).binding == bindings["global_agent_note"]
    with host.database.transaction() as uow:
        uow.connection.execute("UPDATE businesses SET archived_at='2026-10-19T00:00:00Z' WHERE id=?", (host.acme,))
    assert recheck_code(host, bindings["content"]) == "not_found"
    assert recheck(host, bindings["global_agent_note"]).binding == bindings["global_agent_note"]


def test_registered_document_files_are_opened_without_following_links_and_unsafe_registrations_are_refused(fresh, tmp_path_factory):
    host = fresh
    outside = tmp_path_factory.mktemp("doc-outside")
    (outside / "secret.md").write_text("private")
    linked = register_document(host, kind="content", path="content/link.md", business_id=host.acme, write=False)
    linked.file.parent.mkdir(parents=True, exist_ok=True)
    linked.file.symlink_to(outside / "secret.md")
    assert code(read_all, host, authorize(host, document_source(linked))) == "symlink_not_followed"
    directory = register_document(host, kind="content", path="content/linked-dir/a.md", business_id=host.acme, write=False)
    directory.file.parent.symlink_to(outside, target_is_directory=True)
    assert code(read_all, host, authorize(host, document_source(directory))) == "symlink_not_followed"
    missing = register_document(host, kind="content", path="content/absent.md", business_id=host.acme, write=False)
    assert code(read_all, host, authorize(host, document_source(missing))) == "not_found"
    for path in ("../escape.md", "content/../../x.md", "/abs.md", "content//x.md", "content/./x.md"):
        unsafe = register_document(host, kind="content", path=path, business_id=host.acme, write=False)
        assert authorize_code(host, document_source(unsafe)) in {"invalid_configuration", "forbidden"}, path
    graph = register_document(host, kind="agent_note", path="context-graph/note.md", owner=host.ordubashy, write=False)
    assert authorize_code(host, document_source(graph)) == "forbidden", "A Global path never names the published Context"
    # A hard-linked registered document file is the owner's registration, not a workspace file: it is read like any document.
    real = register_document(host, kind="content", path="content/real.md", business_id=host.acme, data=b"real")
    os.link(real.file, real.file.with_name("alias.md"))
    assert read_all(host, authorize(host, document_source(real)))[1] == b"real"
