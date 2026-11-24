#!/usr/bin/env python3
"""Prepare a NEW installed email-engine environment from one pinned source commit.

    prepare.py --repo QORQBOT_PUBLIC_CHECKOUT --commit FULL_SHA --output NEW_DIRECTORY
                            [--node NODE22] [--python PYTHON312] [--pins FILE] [--expect-lock FILE]

Everything happens under NEW_DIRECTORY, which must not exist and must not be inside the checkout:

    src/email-engine/            exact export of --commit (git archive; the checkout is only read)
    wheelhouse/ requirements.lock  third-party wheels for --pins and their sha256 pins
    wheels/                      the engine wheel built from the export
    email-engine-venv/           venv holding the hash-pinned third-party set and the engine wheel
    ...site-packages/engine_email/node_modules   `npm ci` of the engine's package-lock.json, run IN the
                                 installed engine_email directory, i.e. adjacent to the installed outlook_bridge.mjs
                                 that outlook_web.py launches (Path(__file__).with_name("outlook_bridge.mjs"))
    logs/ SOURCE-MANIFEST.json BUILD-RECEIPT.json INSTALLED-ENGINE-EMAIL.sha256 NODE-MODULES.sha256 SYMLINKS.txt SHA256SUMS

It starts no service, reads no configuration or data, contacts no provider, opens no browser, runs no engine code,
test or readiness check, and touches no other environment. Network use is limited to package downloads (PyPI wheels
and setuptools for the build, the npm registry for the locked tarball), served from the local pip/npm caches when present.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

NPM_ARGS = ("ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund")
NODE_VERSION = "v22.22.0"
ENGINE_WHEEL = "qorqut_engine_email-0.2.0-py3-none-any.whl"
BRIDGE_FILES = ("outlook_bridge.mjs", "outlook_capture.mjs", "outlook_quota.mjs")
MANIFESTS = ("package.json", "package-lock.json")
PIP_KEEP = {"PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST", "PIP_CERT", "PIP_CLIENT_CERT", "PIP_PROXY",
            "PIP_CACHE_DIR", "PIP_NO_CACHE_DIR", "PIP_TIMEOUT", "PIP_RETRIES"}


class PrepareError(Exception):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk_files(root: Path, skip_dirs: tuple[str, ...] = ()) -> list[Path]:
    """Regular files under root (symlinks excluded, never followed), sorted by relative path."""
    found = []
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in skip_dirs)
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                found.append(path)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def manifest_lines(root: Path, skip_dirs: tuple[str, ...] = ()) -> list[str]:
    return [f"{sha256_file(path)}  ./{path.relative_to(root).as_posix()}" for path in walk_files(root, skip_dirs)]


def tree_digest(lines: list[str]) -> str:
    return hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest()


class Preparer:
    def __init__(self, args: argparse.Namespace):
        self.repo, self.commit = Path(args.repo).resolve(), args.commit
        self.out = Path(args.output).absolute()
        self.node, self.python = Path(args.node), Path(args.python)
        self.pins, self.expect_lock = Path(args.pins), Path(args.expect_lock) if args.expect_lock else None
        self.logs, self.tmp, self.build = self.out / "logs", self.out / "tmp", self.out / "build"
        self.commands, self.step = [], 0
        self.env = {key: value for key, value in os.environ.items()
                    if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} and not key.startswith(("GIT_", "NPM_CONFIG_", "npm_config_"))
                    and (not key.startswith("PIP_") or key in PIP_KEEP)}
        self.env.update(PYTHONNOUSERSITE="1", PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1",
                        TMPDIR=str(self.tmp), NPM_CONFIG_UPDATE_NOTIFIER="false")

    # --- process helper -----------------------------------------------------------------------------------------
    def run(self, name, argv, cwd=None, env=None):
        self.step += 1
        log = self.logs / f"{self.step:02d}-{name}.log"
        started = time.monotonic()
        with log.open("wb") as handle:
            result = subprocess.run([str(a) for a in argv], cwd=cwd, env=env or self.env, stdin=subprocess.DEVNULL,
                                    stdout=handle, stderr=subprocess.STDOUT, check=False)
        self.commands.append({"step": self.step, "name": name, "argv": [str(a) for a in argv],
                              "cwd": str(cwd) if cwd else None, "returncode": result.returncode,
                              "seconds": round(time.monotonic() - started, 1), "log": log.relative_to(self.out).as_posix()})
        if result.returncode:
            raise PrepareError(f"{name} failed with exit {result.returncode}; see {log}")
        return log

    def capture(self, argv, cwd=None, env=None) -> str:
        return subprocess.run([str(a) for a in argv], cwd=cwd, env=env or self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout.decode().strip()

    # --- inputs -------------------------------------------------------------------------------------------------
    def preflight(self):
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit):
            raise PrepareError("--commit must be a full 40-hex commit id")
        resolved = self.out.resolve(strict=False)
        for protected in (self.repo,):
            if resolved == protected or protected in resolved.parents:
                raise PrepareError(f"output may not be under {protected}")
        if self.out.exists() or self.out.is_symlink():
            raise PrepareError("output already exists; refusing to overwrite an artifact")
        version = self.capture([self.node, "--version"])
        if version != NODE_VERSION:
            raise PrepareError(f"node is {version}; this artifact is configured for {NODE_VERSION}")
        self.node_dir = self.node.resolve().parent
        self.npm = self.node_dir / "npm"
        if not self.npm.exists():
            raise PrepareError("npm is not beside the selected node")
        self.node_version, self.npm_version = version, self.capture([self.npm, "--version"], env={**self.env, "PATH": f"{self.node_dir}{os.pathsep}{self.env['PATH']}"})
        full = self.capture(["git", "-C", self.repo, "rev-parse", f"{self.commit}^{{commit}}"])
        if full != self.commit:
            raise PrepareError("commit does not resolve to itself in the checkout")
        for directory in (self.out, self.logs, self.tmp, self.build):
            directory.mkdir(parents=True, exist_ok=True)

    # --- source export ------------------------------------------------------------------------------------------
    def export_source(self):
        archive = self.out / "src" / f"email-engine-{self.commit[:7]}.tar"
        archive.parent.mkdir()
        self.run("git-archive", ["git", "-C", self.repo, "archive", "--format=tar", f"--output={archive}", f"{self.commit}:services/email-engine"])
        self.source = archive.parent / "email-engine"
        self.source.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(self.source, filter="data")
        self.archive_sha256 = sha256_file(archive)
        self.tree = self.capture(["git", "-C", self.repo, "rev-parse", f"{self.commit}:services/email-engine"])
        self.commit_time = int(self.capture(["git", "-C", self.repo, "log", "-1", "--format=%ct", self.commit]))
        self.commit_subject = self.capture(["git", "-C", self.repo, "log", "-1", "--format=%s", self.commit])
        self.parent = self.capture(["git", "-C", self.repo, "rev-parse", f"{self.commit}^"])
        self.source_files = manifest_lines(self.source)
        for required in ("pyproject.toml", "engine_email/package.json", "engine_email/package-lock.json",
                         "engine_email/outlook_bridge.mjs", "engine_email/outlook_dependencies.py"):
            if not (self.source / required).is_file():
                raise PrepareError(f"export lacks {required}")

    # --- python -------------------------------------------------------------------------------------------------
    def python_environment(self):
        self.venv = self.out / "email-engine-venv"
        self.run("venv", [self.python, "-m", "venv", self.venv])
        self.vpython = self.venv / "bin" / "python"
        wheelhouse = self.out / "wheelhouse"
        self.run("pip-download-pins", [self.vpython, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
                                       "--dest", wheelhouse, "-r", self.pins])
        wanted = {}
        for line in self.pins.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                name, version = line.split("==")
                wanted[re.sub(r"[-_.]+", "_", name).lower()] = (name, version)
        blocks = []
        for name, (display, version) in sorted(wanted.items()):
            matches = [w for w in wheelhouse.glob("*.whl") if re.sub(r"[-_.]+", "_", w.name.split("-")[0]).lower() == name]
            if len(matches) != 1 or matches[0].name.split("-")[1] != version:
                raise PrepareError(f"{display}=={version}: expected exactly one matching wheel, found {[m.name for m in matches]}")
            blocks.append(f"{display}=={version} \\\n    --hash=sha256:{sha256_file(matches[0])}\n")
        if len(list(wheelhouse.glob("*.whl"))) != len(wanted):
            raise PrepareError("wheelhouse holds wheels beyond the pins")
        self.lock_text = "".join(blocks)
        self.lock = self.out / "requirements.lock"
        self.lock.write_text(self.lock_text)
        if self.expect_lock is not None and self.expect_lock.read_text() != self.lock_text:
            raise PrepareError("produced requirements.lock differs from --expect-lock")
        self.run("pip-install-hashed-third-party", [self.vpython, "-m", "pip", "install", "--no-index", "--find-links", wheelhouse,
                                                     "--require-hashes", "--no-deps", "-r", self.lock])
        wheels = self.out / "wheels"
        wheels.mkdir()
        shutil.copytree(self.source, self.build / "email-engine", symlinks=True)
        build_env = {**self.env, "SOURCE_DATE_EPOCH": str(self.commit_time)}
        log = self.run("pip-wheel-engine", [self.vpython, "-m", "pip", "wheel", "-v", "--no-deps", "--wheel-dir", wheels,
                                            self.build / "email-engine"], env=build_env)
        built = sorted(wheels.glob("*.whl"))
        if [w.name for w in built] != [ENGINE_WHEEL]:
            raise PrepareError(f"building produced {[w.name for w in built]}")
        self.wheel = built[0]
        installed = re.findall(r"Successfully installed ([^\n]*setuptools[^\n]*)", log.read_text(errors="replace"))
        self.build_requires = installed[-1] if installed else "see logs/ pip-wheel-engine"
        self.run("pip-install-engine-wheel", [self.vpython, "-m", "pip", "install", "--no-index", "--no-deps", self.wheel])
        self.run("pip-list", [self.vpython, "-m", "pip", "list", "--format=json"])
        self.run("pip-check-metadata", [self.vpython, "-m", "pip", "check"])
        site = self.capture([self.vpython, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"])
        self.site = Path(site)
        self.installed_engine = self.site / "engine_email"

    # --- node ---------------------------------------------------------------------------------------------------
    def node_dependencies(self):
        env = {**self.env, "PATH": f"{self.node_dir}{os.pathsep}{self.env['PATH']}", "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1"}
        self.npm_registry = self.capture([self.npm, "config", "get", "registry"], env=env)
        self.run("npm-ci-installed-engine_email", [self.npm, *NPM_ARGS], cwd=self.installed_engine, env=env)

    # --- read-only inspection of what was installed ----------------------------------------------------------------
    def inspect(self):
        problems, facts = [], {}
        installed = self.installed_engine
        exported = self.source / "engine_email"
        for path in sorted(exported.iterdir()):
            if path.is_file() and (path.suffix in {".py", ".mjs"} or path.name in MANIFESTS):
                target = installed / path.name
                if not target.is_file() or sha256_file(target) != sha256_file(path):
                    problems.append(f"installed engine_email/{path.name} differs from the export")
        for extra in sorted(p.name for p in installed.iterdir() if p.is_file() and p.suffix in {".py", ".mjs"}):
            if not (exported / extra).is_file():
                problems.append(f"installed engine_email/{extra} is not in the export")
        facts["bridge_beside_node_modules"] = (installed / "outlook_bridge.mjs").is_file() and (installed / "node_modules").is_dir()
        facts["node_modules_is_real_directory"] = not (installed / "node_modules").is_symlink()
        package = json.loads((installed / "node_modules/playwright-core/package.json").read_text())
        if (package.get("name"), package.get("version")) != ("playwright-core", "1.63.0"):
            problems.append("installed playwright-core is not 1.63.0")
        facts["playwright_core"] = {"name": package.get("name"), "version": package.get("version"),
                                    "engines": package.get("engines"), "has_install_scripts": any(
                                        key in package.get("scripts", {}) for key in ("preinstall", "install", "postinstall"))}
        facts["declared_dependency"] = json.loads((installed / "package.json").read_text())["dependencies"]
        lock = json.loads((installed / "package-lock.json").read_text())["packages"]["node_modules/playwright-core"]
        hidden = json.loads((installed / "node_modules/.package-lock.json").read_text())["packages"]
        facts["node_modules_packages"] = sorted(hidden)
        recorded = hidden.get("node_modules/playwright-core", {})
        for field in ("version", "resolved", "integrity"):
            if recorded.get(field) != lock.get(field):
                problems.append(f"node_modules/.package-lock.json {field} differs from package-lock.json")
        facts["lock_integrity"] = lock.get("integrity")
        for target in ("main", "types"):
            value = package.get(target)
            if value and not (installed / "node_modules/playwright-core" / value).is_file():
                problems.append(f"playwright-core {target} target {value} is missing")
        exports = package.get("exports", {}).get(".", {})
        for condition in ("import", "require", "default"):
            value = exports.get(condition) if isinstance(exports, dict) else None
            if isinstance(value, str) and not (installed / "node_modules/playwright-core" / value).is_file():
                problems.append(f"playwright-core exports['.'].{condition} target {value} is missing")
        facts["playwright_core_exports"] = exports
        dist = {}
        for info in sorted(self.site.glob("*.dist-info")):
            fields = dict(line.split(": ", 1) for line in (info / "METADATA").read_text().splitlines()[:12] if ": " in line)
            dist[re.sub(r"[-_.]+", "_", fields["Name"]).lower()] = fields["Version"]
        wanted = {re.sub(r"[-_.]+", "_", n).lower(): v for n, v in
                  (line.split("#", 1)[0].strip().split("==") for line in self.pins.read_text().splitlines() if line.split("#", 1)[0].strip())}
        for name, version in wanted.items():
            if dist.get(name) != version:
                problems.append(f"{name}: installed {dist.get(name)} != pinned {version}")
        extra = sorted(set(dist) - set(wanted) - {"qorqut_engine_email", "pip"})
        if extra:
            problems.append(f"unpinned distributions installed: {extra}")
        facts["distributions"] = dict(sorted(dist.items()))
        if problems:
            raise PrepareError("inspection failed: " + "; ".join(problems))
        return facts

    # --- records ------------------------------------------------------------------------------------------------
    def write_records(self, facts):
        installed = self.installed_engine
        engine_lines = manifest_lines(installed, skip_dirs=("node_modules", "__pycache__"))
        node_lines = manifest_lines(installed / "node_modules")
        (self.out / "INSTALLED-ENGINE-EMAIL.sha256").write_text("\n".join(engine_lines) + "\n")
        (self.out / "NODE-MODULES.sha256").write_text("\n".join(node_lines) + "\n")
        links = []
        for directory, names, files in os.walk(self.out, followlinks=False):
            for entry in (*names, *files):
                path = Path(directory) / entry
                if path.is_symlink():
                    links.append(f"{path.relative_to(self.out).as_posix()} -> {os.readlink(path)}")
        (self.out / "SYMLINKS.txt").write_text("\n".join(sorted(links)) + "\n")
        bridge = installed / "outlook_bridge.mjs"
        check = (f"PYTHONDONTWRITEBYTECODE=1 PATH={self.node_dir}:$PATH {self.vpython} -m engine_email.outlook_dependencies check "
                 f"--bridge {bridge} --node {self.node} --require")
        manifest = {
            "format": 1, "artifact": "email-engine-prepared",
            "source": {"repository": str(self.repo), "subdirectory": "services/email-engine", "branch": "v1.3/paper-delivery-engine-stage-20261030", "commit": self.commit,
                       "tree": self.tree, "parent": self.parent, "subject": self.commit_subject,
                       "committed_at_epoch": self.commit_time, "archive_sha256": self.archive_sha256,
                       "export": "src/email-engine", "export_tree_sha256": tree_digest(self.source_files),
                       "exported_files": len(self.source_files)},
            "python_pins": {"input": str(self.pins), "input_sha256": sha256_file(self.pins),
                            "requirements_lock": "requirements.lock", "requirements_lock_sha256": sha256_file(self.lock)},
            "node_pin": {"package_lock": "engine_email/package-lock.json", "package_lock_sha256": sha256_file(installed / "package-lock.json"),
                         "package_json_sha256": sha256_file(installed / "package.json"),
                         "lock_integrity": facts["lock_integrity"]},
        }
        (self.out / "SOURCE-MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        receipt = {
            "format": 1, "state": "prepared-not-configured-not-tested",
            "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "build_prefix": str(self.out),
            "tools": {"python": self.capture([self.vpython, "--version"]), "python_executable": str(self.python),
                      "pip": self.capture([self.vpython, "-m", "pip", "--version"]).split(" from ")[0],
                      "engine_build_requires_installed": self.build_requires,
                      "node": self.node_version, "node_path": str(self.node), "node_sha256": sha256_file(self.node.resolve()),
                      "npm": self.npm_version, "npm_registry": self.npm_registry,
                      "source_date_epoch": self.commit_time},
            "npm_command": f"{self.npm} {' '.join(NPM_ARGS)}",
            "npm_environment": {"PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1", "PATH_first_entry": str(self.node_dir)},
            "engine_wheel": {"file": f"wheels/{self.wheel.name}", "sha256": sha256_file(self.wheel)},
            "third_party_wheels": {w.name: sha256_file(w) for w in sorted((self.out / "wheelhouse").glob("*.whl"))},
            "installed": {"site_packages": str(self.site), "engine_email": str(installed),
                          "bridge": str(installed / "outlook_bridge.mjs"), "node_modules": str(installed / "node_modules"),
                          "engine_email_tree_sha256": tree_digest(engine_lines), "engine_email_files": len(engine_lines),
                          "node_modules_tree_sha256": tree_digest(node_lines), "node_modules_files": len(node_lines)},
            "inspection": facts,
            "commands": self.commands,
            "not_run": ["tests", "linters/typechecks", "engine_email.outlook_dependencies check or stage (starts Node on the bridge)",
                        "any import or start of engine code", "service, systemd or migration steps", "browser/CDP/provider/network health checks",
                        "reads of /opt or any live data/config"],
            "operator_offline_dependency_check_not_run": check,
        }
        (self.out / "BUILD-RECEIPT.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    def finish(self):
        shutil.rmtree(self.build)
        shutil.rmtree(self.tmp)
        lines = []
        for path in walk_files(self.out):
            if path.name != "SHA256SUMS":
                lines.append(f"{sha256_file(path)}  ./{path.relative_to(self.out).as_posix()}")
        (self.out / "SHA256SUMS").write_text("\n".join(lines) + "\n")
        return len(lines)

    def prepare(self):
        self.preflight()
        self.export_source()
        self.python_environment()
        self.node_dependencies()
        facts = self.inspect()
        self.write_records(facts)
        return self.finish()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    here = Path(__file__).resolve().parent
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--node", default=str(Path.home() / ".nvm/versions/node" / NODE_VERSION / "bin/node"))
    parser.add_argument("--python", default="/usr/bin/python3.12")
    parser.add_argument("--pins", type=Path, default=here / "python-pins.txt")
    parser.add_argument("--expect-lock", type=Path, default=here / "requirements.lock", help="fail unless the produced requirements.lock is byte-identical to this recorded lock")
    args = parser.parse_args(argv)
    try:
        count = Preparer(args).prepare()
    except PrepareError as error:
        print(json.dumps({"prepared": False, "error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps({"prepared": True, "output": str(args.output), "commit": args.commit, "hashed_files": count}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
