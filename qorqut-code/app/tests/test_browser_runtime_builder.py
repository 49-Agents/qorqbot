"""The private Node runtime builder: checksum first, safe extraction, its own outputs only, no host Node or npm."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import tarfile

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools/browser"
spec = importlib.util.spec_from_file_location("build_runtime", TOOLS / "build_runtime.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
TOP = builder.TOP

FAKE_NODE = """#!/bin/sh
echo "$0 $*" >> @RECORD@/executed
if [ "$1" = --version ]; then echo @VERSION@; exit 0; fi
case "$1" in
*/npm-cli.js)
  shift
  case "$1" in
  --version) echo @NPM@; exit 0;;
  ci)
    echo "$PATH" > @RECORD@/path
    pwd > @RECORD@/cwd
    [ @CI@ -eq 0 ] || { echo "ci failed" >&2; exit @CI@; }
    mkdir -p node_modules/agent-browser node_modules/proxy-chain
    printf '{"version": "0.38.1"}' > node_modules/agent-browser/package.json
    printf '{"version": "3.0.1"}' > node_modules/proxy-chain/package.json
    printf '{}' > node_modules/.package-lock.json
    exit 0;;
  esac;;
esac
echo "unexpected $*" >&2
exit 2
"""


def fake_node(record, *, version="v24.21.0", npm="11.19.0", ci=0):
    return (FAKE_NODE.replace("@RECORD@", str(record)).replace("@VERSION@", version)
            .replace("@NPM@", npm).replace("@CI@", str(ci))).encode()


def entry(name, kind=tarfile.REGTYPE, data=b"", mode=0o644, link=""):
    info = tarfile.TarInfo(name)
    info.type, info.mode, info.linkname = kind, mode, link
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    return info, data


def distribution(record, **options):
    return [
        entry(TOP, tarfile.DIRTYPE, mode=0o755), entry(TOP + "/bin", tarfile.DIRTYPE, mode=0o755),
        entry(TOP + "/bin/node", data=fake_node(record, **options), mode=0o755),
        entry(TOP + "/bin/npm", tarfile.SYMTYPE, link="../lib/node_modules/npm/bin/npm-cli.js"),
        entry(TOP + "/lib/node_modules/npm/bin", tarfile.DIRTYPE, mode=0o755),
        entry(TOP + "/lib/node_modules/npm/bin/npm-cli.js", data=b"// npm\n"),
    ]


def archive(path, entries):
    with tarfile.open(path, "w:xz") as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if info.type == tarfile.REGTYPE else None)
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    runtime, record = tmp_path / "runtime", tmp_path / "record"
    runtime.mkdir()
    record.mkdir()
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(TOOLS / name, runtime / name)
    # A conflicting host Node/npm first on the caller's PATH must never be used.
    host = tmp_path / "host-bin"
    host.mkdir()
    for name in ("node", "npm"):
        (host / name).write_text(f"#!/bin/sh\necho host {name} >> {record}/host\nexit 9\n")
        (host / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{host}:{os.environ['PATH']}")
    return runtime, record, tmp_path


def run_build(workspace, entries, *, sha256=None, **options):
    runtime, record, base = workspace
    path = base / "node.tar.xz"
    actual = archive(path, entries)
    calls = []
    def download(url, destination):
        calls.append(url)
        shutil.copyfile(path, destination)
    builder.build(runtime, download=download, url="https://example.invalid/node.tar.xz", sha256=sha256 or actual,
                  log=lambda *_: None)
    return calls


def state(runtime):
    return sorted(str(path.relative_to(runtime)) for path in runtime.rglob("*") if ".runtime-build-" not in str(path))


def leftovers(runtime):
    return list(runtime.glob(builder.WORK_PREFIX + "*"))


def occupy(runtime):
    """A previous generated build: marked node/ and an npm-generated node_modules/."""
    (runtime / "node/bin").mkdir(parents=True)
    (runtime / "node/bin/node").write_text("previous")
    (runtime / "node" / builder.MARKER).write_text('{"generator": "%s"}' % builder.GENERATOR)
    (runtime / "node_modules/previous").mkdir(parents=True)
    (runtime / "node_modules/.package-lock.json").write_text("{}")


def test_the_pinned_distribution_is_the_one_the_manager_fixed():
    assert builder.NODE_VERSION == "24.21.0" and builder.NPM_VERSION == "11.19.0"
    assert builder.ARCHIVE_URL == "https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz"
    assert builder.ARCHIVE_SHA256 == "fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6"
    assert builder.TOP == "node-v24.21.0-linux-x64"


def test_a_good_archive_publishes_the_bundled_node_and_locked_packages_with_a_child_only_path(workspace):
    runtime, record, _ = workspace
    assert run_build(workspace, distribution(record)) == ["https://example.invalid/node.tar.xz"]
    assert (runtime / "node/bin/node").is_file() and os.access(runtime / "node/bin/node", os.X_OK)
    assert os.readlink(runtime / "node/bin/npm") == "../lib/node_modules/npm/bin/npm-cli.js"
    assert '"generator": "tools/browser/build_runtime.py"' in (runtime / "node" / builder.MARKER).read_text()
    assert (runtime / "node_modules/agent-browser/package.json").is_file()
    assert not leftovers(runtime)
    # npm ci ran with only the bundled bin first and system dirs after it; the host node/npm never ran.
    assert (record / "path").read_text().strip().endswith("/" + TOP + "/bin:/usr/bin:/bin")
    assert not (record / "host").exists()
    assert (record / "cwd").read_text().strip().endswith("/application")
    assert not (runtime / "package.json").is_symlink() and (runtime / "package-lock.json").is_file()


def test_a_repeat_build_replaces_only_its_own_previous_outputs(workspace):
    runtime, record, _ = workspace
    occupy(runtime)
    run_build(workspace, distribution(record))
    assert (runtime / "node/bin/node").read_text().startswith("#!/bin/sh")
    assert not (runtime / "node_modules/previous").exists() and (runtime / "node_modules/agent-browser").is_dir()
    assert not leftovers(runtime)


def test_a_bad_checksum_is_never_extracted_or_executed_and_previous_outputs_survive(workspace):
    runtime, record, _ = workspace
    occupy(runtime)
    before = state(runtime)
    with pytest.raises(builder.BuildError, match="does not match the pinned"):
        run_build(workspace, distribution(record), sha256="0" * 64)
    assert state(runtime) == before and not leftovers(runtime)
    assert (runtime / "node/bin/node").read_text() == "previous"
    assert not (record / "executed").exists() and not (record / "host").exists()


def unsafe_cases():
    yield "traversal", [entry(TOP + "/../evil", data=b"x")]
    yield "outside-top", [entry("elsewhere/file", data=b"x")]
    yield "absolute", [entry("/tmp/evil", data=b"x")]
    yield "backslash", [entry(TOP + "\\..\\evil", data=b"x")]
    yield "absolute-link", [entry(TOP + "/link", tarfile.SYMTYPE, link="/etc/passwd")]
    yield "escaping-link", [entry(TOP + "/link", tarfile.SYMTYPE, link="../../../etc/passwd")]
    yield "link-then-dotdot", [entry(TOP + "/a", tarfile.SYMTYPE, link="."), entry(TOP + "/b", tarfile.SYMTYPE, link="a/..")]
    yield "member-through-link", [entry(TOP + "/d", tarfile.SYMTYPE, link="."), entry(TOP + "/d/evil", data=b"x")]
    yield "hard-link", [entry(TOP + "/hard", tarfile.LNKTYPE, link=TOP + "/bin/node")]
    yield "device", [entry(TOP + "/dev", tarfile.CHRTYPE)]
    yield "fifo", [entry(TOP + "/fifo", tarfile.FIFOTYPE)]
    yield "duplicate-file", [entry(TOP + "/lib/node_modules/npm/bin/npm-cli.js", data=b"// replaced\n")]


@pytest.mark.parametrize("name,extra", list(unsafe_cases()), ids=[name for name, _ in unsafe_cases()])
def test_an_unsafe_archive_is_never_published_or_executed(workspace, name, extra):
    runtime, record, _ = workspace
    occupy(runtime)
    before = state(runtime)
    with pytest.raises(builder.BuildError):
        run_build(workspace, distribution(record) + extra)
    assert state(runtime) == before and not leftovers(runtime)
    assert (runtime / "node/bin/node").read_text() == "previous"
    assert not (record / "executed").exists() and not (record / "host").exists()
    assert not (workspace[2] / "evil").exists()


def test_archive_size_limits_apply_after_the_checksum(workspace, monkeypatch):
    runtime, record, _ = workspace
    monkeypatch.setitem(builder.LIMITS, "unpacked", 10)
    with pytest.raises(builder.BuildError, match="more than the allowed size"):
        run_build(workspace, distribution(record))
    monkeypatch.setitem(builder.LIMITS, "unpacked", 1 << 30)
    monkeypatch.setitem(builder.LIMITS, "members", 2)
    with pytest.raises(builder.BuildError, match="too many entries"):
        run_build(workspace, distribution(record))
    assert not (runtime / "node").exists() and not leftovers(runtime) and not (record / "executed").exists()


def test_a_corrupt_archive_is_refused_without_a_traceback(workspace):
    runtime, record, base = workspace
    (base / "node.tar.xz").write_bytes(b"not an xz stream")
    data = (base / "node.tar.xz").read_bytes()
    with pytest.raises(builder.BuildError, match="Could not safely extract"):
        builder.build(runtime, download=lambda url, dest: Path(dest).write_bytes(data), url="https://example.invalid/x",
                      sha256=hashlib.sha256(data).hexdigest(), log=lambda *_: None)
    assert not (runtime / "node").exists() and not leftovers(runtime)


@pytest.mark.parametrize("options,message", [
    ({"version": "v24.20.0"}, "bundled Node reports v24.20.0"),
    ({"npm": "10.9.4"}, "bundled npm reports 10.9.4"),
    ({"ci": 1}, "npm ci failed"),
])
def test_a_wrong_version_or_failed_locked_install_publishes_nothing(workspace, options, message):
    runtime, record, _ = workspace
    occupy(runtime)
    before = state(runtime)
    with pytest.raises(builder.BuildError, match=message):
        run_build(workspace, distribution(record, **options))
    assert state(runtime) == before and not leftovers(runtime) and not (record / "host").exists()


def test_installed_packages_must_match_the_exact_pins(workspace):
    runtime, record, _ = workspace
    entries = distribution(record)
    entries[2] = entry(TOP + "/bin/node", mode=0o755, data=fake_node(record).replace(b'"0.38.1"', b'"0.38.0"'))
    with pytest.raises(builder.BuildError, match="installed agent-browser 0.38.0, expected 0.38.1"):
        run_build(workspace, entries)
    assert not (runtime / "node").exists() and not (runtime / "node_modules").exists()


@pytest.mark.parametrize("make", ["unmarked-node", "symlinked-node", "foreign-modules", "symlinked-modules"])
def test_an_output_this_builder_did_not_generate_is_never_replaced(workspace, make):
    runtime, record, base = workspace
    (base / "elsewhere").mkdir()
    (base / "elsewhere/keep").write_text("keep")
    if make == "unmarked-node":
        (runtime / "node").mkdir()
        (runtime / "node/keep").write_text("keep")
    elif make == "symlinked-node":
        (runtime / "node").symlink_to(base / "elsewhere")
    elif make == "foreign-modules":
        (runtime / "node_modules").mkdir()
        (runtime / "node_modules/keep").write_text("keep")
    else:
        (runtime / "node_modules").symlink_to(base / "elsewhere")
    before = state(runtime)
    downloaded = []
    with pytest.raises(builder.BuildError, match="not an output generated by"):
        builder.build(runtime, download=lambda *a: downloaded.append(a), log=lambda *_: None)
    assert state(runtime) == before and not downloaded and (base / "elsewhere/keep").read_text() == "keep"


def test_an_incompatible_host_is_refused_before_anything_is_downloaded(workspace, monkeypatch):
    runtime, *_ = workspace
    for system, machine in (("Darwin", "arm64"), ("Linux", "aarch64"), ("Windows", "AMD64")):
        with pytest.raises(builder.BuildError, match="linux-x64"):
            builder.check_host(system, machine)
    monkeypatch.setattr(builder.platform, "machine", lambda: "aarch64")
    def download(*_):
        raise AssertionError("downloaded on an incompatible host")
    with pytest.raises(builder.BuildError, match="linux-x64"):
        builder.build(runtime, download=download, log=lambda *_: None)
    assert not leftovers(runtime)


def test_the_download_is_https_only_including_redirects(tmp_path):
    with pytest.raises(builder.BuildError, match="HTTPS"):
        builder.fetch("http://nodejs.org/dist/node.tar.xz", tmp_path / "node.tar.xz")
    with pytest.raises(builder.BuildError, match="non-HTTPS redirect"):
        builder._HttpsOnly().redirect_request(None, None, 302, "Found", {}, "http://mirror.invalid/node.tar.xz")
    assert not (tmp_path / "node.tar.xz").exists()


def test_publish_restores_previous_outputs_when_a_swap_fails(workspace):
    runtime, record, base = workspace
    occupy(runtime)
    before = state(runtime)
    work = base / "work"
    work.mkdir()
    (work / "node").mkdir()
    with pytest.raises(builder.BuildError, match="Could not publish"):
        builder.publish(runtime, work, {"node": work / "node", "node_modules": work / "missing"},
                        [("node", True), ("node_modules", True)])
    assert state(runtime) == before and (runtime / "node/bin/node").read_text() == "previous"


def test_stale_work_directories_are_removed_only_when_they_are_real_directories(workspace):
    runtime, _, base = workspace
    stale = runtime / (builder.WORK_PREFIX + "old")
    stale.mkdir()
    (stale / "junk").write_text("x")
    (base / "keep").mkdir()
    (runtime / (builder.WORK_PREFIX + "link")).symlink_to(base / "keep")
    builder._remove_leftovers(runtime)
    assert not stale.exists() and (base / "keep").is_dir()
