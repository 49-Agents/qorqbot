#!/usr/bin/env python3
"""Prepare owned local source copies, first-party wheels and Python environments from an exported source tree.

    prepare-local.py --source-root EXPORTED_DIRECTORY --output NEW_DIRECTORY [--with-content] [--with-paper] [--with-fastmail]
                     [--with-google-calendar] [--with-treg]

``--source-root`` is the output of ``export-unified.py`` (it must carry ``SOURCE-MANIFEST.json``) with this layout:

    qorqut-code/ (app/, with qorqut-operations/, qorqut-context/, biz-dashboard/ filled by their own components;
                  extensions/google-calendar/ is part of this component and is built only with --with-google-calendar;
                  extensions/treg/ likewise, only with --with-treg)
    qorqut-agentic/src/qorqut_agentic/resources/defaults/   dependencies/{document-history,email-management}/
    extensions/{qorqut-paper,qorqut-content,content-library,qorqut-fastmail}/ and services/{paper-service,email-engine}/
        (only for the selected extensions; a standard preparation needs neither the email engine nor the Fastmail package)

The export is copied to ``OUTPUT/src`` and never modified; symlinks that are absolute or resolve outside it are refused before
anything is copied. Unchanged pure-Python wheels are reused from a content-checked local cache; other wheels are built from
that copy into ``OUTPUT/wheels``. Use ``--no-wheel-cache`` for a cold build. Before anything is installed, each
environment's first-party requirements (extras and markers applied) are reconciled against the wheels selected for it: a
requirement that is a URL, has no wheel among the selected sources, belongs to another environment or is not satisfied by the
local version is an integration error. Each environment is then installed with its complete first-party set as explicit wheel
files, and every first-party distribution found afterwards must come from one of them. Third-party dependencies use the normal
package index and cache (pip configuration files and requirement/constraint/find-links/target/prefix/user overrides in the
ambient environment are ignored), installed only into the owned environments:

    OUTPUT/venv                  host (service extra, retained constraints) and selected extension wheels
    OUTPUT/email-engine-venv     the email engine, the transport of the Fastmail provider (--with-fastmail only)
    OUTPUT/paper-service-venv    the Paper service (--with-paper), from its hash-locked runtime requirements

``--with-fastmail`` adds the ``qorqut-fastmail`` bridge to the host environment (with the Fastmail package's own constraints for
its third-party dependencies) and prepares the separate email-engine environment. Neither is configured or started: the
extension still has to be selected in the installation configuration and given its engine URL and token file.

``--with-google-calendar`` adds the ``qorqut-google-calendar`` wheel, built from ``qorqut-code/extensions/google-calendar``, to the
host environment. Without the flag no Google wheel is built, installed or recorded. The wheel is not selected, configured or
connected: the extension still has to be named in the installation configuration (``qorqut_google_calendar.extension:create``)
and an account connected, and nothing here reads a credential or contacts Google.

``--with-treg`` adds the ``qorqut-treg`` wheel, built from ``qorqut-code/extensions/treg``, to the host environment. Without the
flag no treg wheel is built, installed or recorded. The wheel is not selected or given a key here: the extension is named in the
installation configuration (``qorqut_treg.extension:create``) and the owner adds a key in the dashboard; nothing here contacts treg.

The result is a preparation receipt (``OUTPUT/PREPARATION-RECEIPT.json``), not a configured or running instance: this tool
creates no owner identity or configuration, provisions no OS service or native CLI, builds no Node or Go component, starts
nothing and contacts no provider.
"""
from __future__ import annotations

import importlib.util
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path
from urllib.parse import unquote, urlparse

try:                                                   # the parser that ships with packaging, or pip's vendored copy of it
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:
    try:
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.utils import canonicalize_name
    except ImportError:
        Requirement = canonicalize_name = None

# (receipt key, source directory under the export) of the wheels the host environment needs before the host itself.
_spec = importlib.util.spec_from_file_location("qorqut_source_snapshot", Path(__file__).resolve().parents[1] / "app/qorqut/source_snapshot.py")
_snapshot = importlib.util.module_from_spec(_spec)
exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, "exec"), _snapshot.__dict__)

ENGINES = [("qorqut-agentic", "qorqut-agentic"), ("qorqut-operations", "qorqut-code/qorqut-operations"), ("qorqut-context", "qorqut-code/qorqut-context"),
           ("document-history", "dependencies/document-history")]
HOST = ("host", "qorqut-code/app")
EMAIL_ENGINE = ("email-engine", "services/email-engine")
FASTMAIL_BRIDGE = ("qorqut-fastmail", "extensions/qorqut-fastmail")
GOOGLE_CALENDAR = ("qorqut-google-calendar", "qorqut-code/extensions/google-calendar")
TREG = ("qorqut-treg", "qorqut-code/extensions/treg")
PAPER_SERVICE = ("paper-service", "services/paper-service")
PAPER_BRIDGE = ("qorqut-paper", "extensions/qorqut-paper")
CONTENT = [("content-library", "extensions/content-library"), ("qorqut-content", "extensions/qorqut-content")]

REQUIRED = ["qorqut-agentic/pyproject.toml", "qorqut-code/app/pyproject.toml", "qorqut-code/app/service-constraints.txt", "qorqut-code/app/package-sources.json",
            "qorqut-code/qorqut-operations/pyproject.toml", "qorqut-code/qorqut-context/pyproject.toml", "qorqut-code/biz-dashboard/index.html",
            "qorqut-agentic/src/qorqut_agentic/resources/defaults/roles/imported-roles", "dependencies/document-history/pyproject.toml",
            "qorqut-agentic/src/qorqut_agentic/resources/defaults/operations/source-instructions/email/plugin.yaml"]
REQUIRED_PAPER = ["extensions/qorqut-paper/pyproject.toml", "services/paper-service/pyproject.toml", "services/paper-service/requirements/runtime.lock"]
REQUIRED_FASTMAIL = ["extensions/qorqut-fastmail/pyproject.toml", "extensions/qorqut-fastmail/constraints.txt",
                     "services/email-engine/pyproject.toml"]
REQUIRED_CONTENT = ["extensions/content-library/pyproject.toml", "extensions/qorqut-content/pyproject.toml"]
REQUIRED_GOOGLE_CALENDAR = ["qorqut-code/extensions/google-calendar/pyproject.toml"]
REQUIRED_TREG = ["qorqut-code/extensions/treg/pyproject.toml"]

NOT_INCLUDED = ["owner identity and installation configuration", "operating-system services", "native coding CLIs", "browser runtime (npm)",
                "WhatsApp tool (Go build)", "Paper web UI artifact (npm build)", "any started process or provider contact"]

# Lists installed distributions of one environment: name, version, direct-URL of explicit file installs.
INVENTORY = """
import importlib.metadata as m, json, sys
rows = []
for d in m.distributions():
    name = d.metadata["Name"]
    if name:
        direct = d.read_text("direct_url.json")
        rows.append([name, d.version, json.loads(direct).get("url") if direct else None])
print(json.dumps({"python": sys.version.split()[0], "distributions": sorted(rows, key=lambda row: row[0].lower())}))
"""


class PrepareError(Exception):
    pass


FIRST_PARTY = re.compile(r"qorqut-")
HOST_EXTRAS = ("service",)
# The only pip settings taken from the ambient environment: index, network and cache selection for third-party packages.
PIP_KEEP = {"PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST", "PIP_CERT", "PIP_CLIENT_CERT", "PIP_PROXY", "PIP_CACHE_DIR",
            "PIP_NO_CACHE_DIR", "PIP_TIMEOUT", "PIP_RETRIES"}


def inside(path, parent):
    return path == parent or parent in path.parents


class Preparer:
    def __init__(self, source, output, content, paper, fastmail=False, google_calendar=False, wheel_cache=None, treg=False):
        self.source, self.output, self.content, self.paper, self.fastmail = source, output, content, paper, fastmail
        self.google_calendar, self.treg = google_calendar, treg
        self.wheel_cache = wheel_cache
        self.wheel_inputs, self.reused = {}, set()
        self.src, self.wheels, self.logs = output / "src", output / "wheels", output / "logs"
        self.work, self.tmp = output / "build", output / "build" / "tmp"
        self.built, self.meta, self.step = {}, {}, 0
        self.env = {key: value for key, value in os.environ.items()
                    if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} and not key.startswith("GIT_")
                    and (not key.startswith("PIP_") or key in PIP_KEEP)}
        self.env.update(PYTHONNOUSERSITE="1", PIP_CONFIG_FILE=os.devnull, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1",
                        TMPDIR=str(self.tmp))

    # --- inputs -----------------------------------------------------------------------------------------------------
    def check_links(self):
        """No symlink anywhere in the export may be absolute or resolve outside it (nothing is built through an external link)."""
        escaping = []
        for directory, names, files in os.walk(self.source):         # links are never followed: a linked directory is listed, not entered
            for entry in (*names, *files):
                path = Path(directory) / entry
                if path.is_symlink() and (os.path.isabs(os.readlink(path)) or not inside(Path(os.path.realpath(path)), self.source)):
                    escaping.append(path.relative_to(self.source).as_posix())
        if escaping:
            raise PrepareError("the source export contains symlinks that are absolute or resolve outside it: "
                               + ", ".join(escaping[:5]) + (f" (and {len(escaping) - 5} more)" if len(escaping) > 5 else ""))

    def check_source(self):
        self.check_links()
        required = (REQUIRED + (REQUIRED_PAPER if self.paper else []) + (REQUIRED_CONTENT if self.content else [])
                    + (REQUIRED_FASTMAIL if self.fastmail else []) + (REQUIRED_GOOGLE_CALENDAR if self.google_calendar else [])
                    + (REQUIRED_TREG if self.treg else []))
        missing = [name for name in required if not (self.source / name).exists()]
        if missing:
            empty = [name for name in ("qorqut-operations", "qorqut-context", "biz-dashboard")
                     if (self.source / "qorqut-code" / name).is_dir() and not any((self.source / "qorqut-code" / name).iterdir())]
            hint = f"; empty gitlink locations that need their own source component: {', '.join(empty)}" if empty else ""
            raise PrepareError("the source export is incomplete, missing: " + ", ".join(missing) + hint)
        try:
            manifest, self.manifest_digest = _snapshot.validate_export(self.source)
            self.components = manifest["components"]
        except (OSError, ValueError) as exc:
            raise PrepareError(str(exc)) from exc
        for _, relative in self.wheel_sources():
            if (self.source / relative / ".git").exists():
                raise PrepareError(f"{relative} is a Git checkout, not an export; use export-unified.py")

    def wheel_sources(self):
        sources = [*ENGINES, HOST]
        if self.paper:
            sources += [PAPER_SERVICE, PAPER_BRIDGE]
        if self.content:
            sources += CONTENT
        if self.fastmail:
            sources += [EMAIL_ENGINE, FASTMAIL_BRIDGE]
        if self.google_calendar:
            sources.append(GOOGLE_CALENDAR)
        if self.treg:
            sources.append(TREG)
        return sources

    # --- subprocess and files ---------------------------------------------------------------------------------------
    def run(self, name, command):
        self.step += 1
        log = self.logs / f"{self.step:02d}-{name}.log"
        print(f"==> {name}", flush=True)
        with open(log, "wb") as handle:
            code = subprocess.run([str(part) for part in command], cwd=self.work, env=self.env, stdout=handle,
                                  stderr=subprocess.STDOUT).returncode
        if code:
            tail = "\n".join(log.read_text(errors="replace").splitlines()[-15:])
            raise PrepareError(f"{name} failed (exit {code}); full log: {log}\n{tail}")

    def check_lock(self, path):
        """The hash-locked third-party requirements of the Paper service must not name a first-party package."""
        for line in path.read_text().splitlines():
            match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if match and FIRST_PARTY.match(canonicalize_name(match.group(1))):
                raise PrepareError(f"paper-service-venv: {path.name} lists the first-party package {match.group(1)}")

    def make_environment(self, directory):
        self.run(f"create {directory.name}", [sys.executable, "-m", "venv", directory])
        return directory / "bin" / "python"

    def build(self, python, key, relative):
        # Hash bytes/modes/links, not Git SHA or timestamps: an unrelated commit
        # must not rebuild this package, and a changed resource must invalidate it.
        inputs = {"source": _snapshot.snapshot_sha256(self.src / relative),
                  "python": sys.version, "platform": sysconfig.get_platform(),
                  "builder": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  "snapshot": hashlib.sha256(Path(_spec.origin).read_bytes()).hexdigest()}
        fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
        self.wheel_inputs[key] = fingerprint
        cached = self.cached_wheel(fingerprint)
        if cached is not None:
            print(f"==> reuse wheel {key}", flush=True)
            wheel = self.wheels / cached.name
            shutil.copy2(cached, wheel)
            self.reused.add(key)
        else:
            wheel = self.build_wheel(python, key, relative)
            self.cache_wheel(fingerprint, wheel)
        self.built[key] = wheel
        with zipfile.ZipFile(wheel) as archive:
            member = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
            self.meta[key] = Parser().parsestr(archive.read(member).decode())
        return wheel

    def cached_wheel(self, fingerprint):
        if self.wheel_cache is None:
            return None
        entry = self.wheel_cache / fingerprint
        try:
            record = json.loads((entry / "wheel.json").read_text())
            name = record["file"]
            if (record["inputs"] != fingerprint or Path(name).name != name
                    or not name.endswith("-py3-none-any.whl")):
                return None
            wheel = entry / name
            if wheel.is_symlink() or hashlib.sha256(wheel.read_bytes()).hexdigest() != record["sha256"]:
                return None
            with zipfile.ZipFile(wheel) as archive:
                if archive.testzip() is not None:
                    return None
            return wheel
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
            return None  # Missing, partial or corrupt caches are misses, never evidence.

    def cache_wheel(self, fingerprint, wheel):
        if self.wheel_cache is None or not wheel.name.endswith("-py3-none-any.whl"):
            return
        # Atomic immutable entries: concurrent preparations never observe a
        # half-written wheel. Cache problems do not make preparation unavailable.
        try:
            self.wheel_cache.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".partial-", dir=self.wheel_cache) as folder:
                stage = Path(folder) / "entry"
                stage.mkdir()
                shutil.copy2(wheel, stage / wheel.name)
                (stage / "wheel.json").write_text(json.dumps({"inputs": fingerprint, "file": wheel.name,
                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}) + "\n")
                stage.rename(self.wheel_cache / fingerprint)
        except OSError:
            pass

    def build_wheel(self, python, key, relative):
        build_source = self.work / "sources" / key
        shutil.copytree(self.src / relative, build_source, symlinks=True)
        before = set(self.wheels.iterdir())
        self.run(f"build wheel {key}", [python, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", self.wheels, build_source])
        new = [path for path in set(self.wheels.iterdir()) - before if path.suffix == ".whl"]
        if len(new) != 1:
            raise PrepareError(f"building {relative} produced {len(new)} wheels; expected exactly one")
        return new[0]

    # --- first-party rule, applied before anything is installed ---------------------------------------------------------
    def plan_first_party(self):
        """Normalized distribution name -> build key of every local wheel; the same distribution twice is an error."""
        self.local = {}
        for key, message in self.meta.items():
            name = canonicalize_name(message["Name"])
            if name in self.local:
                raise PrepareError(f"two selected sources build the first-party distribution {name}: {self.local[name]} and {key}")
            self.local[name] = key

    def check_first_party(self, environment, keys, extras=None):
        """Reconcile every active first-party requirement of one environment's wheels with the wheels selected for it."""
        members = {canonicalize_name(self.meta[key]["Name"]): key for key in keys}
        selected = {key: set((extras or {}).get(key, ())) for key in keys}
        changed = True
        while changed:                                       # extras named by a first-party dependency activate its requirements
            changed = False
            for key in keys:
                for line in self.meta[key].get_all("Requires-Dist") or []:
                    requirement = Requirement(line)
                    if requirement.marker and not any(requirement.marker.evaluate({"extra": extra}) for extra in ("", *selected[key])):
                        continue
                    name = canonicalize_name(requirement.name)
                    if not (name in self.local or FIRST_PARTY.match(name)):
                        continue
                    who = f"{environment}: {self.meta[key]['Name']} requires {requirement.name}"
                    if requirement.url:
                        raise PrepareError(f"{who} by URL; first-party packages come only from locally built wheels")
                    if name not in members:
                        where = (f"it is built from {dict(self.wheel_sources()).get(self.local[name], self.local[name])} for a separate environment"
                                 if name in self.local else "no selected source builds it")
                        raise PrepareError(f"{who}, which this environment does not include: {where} (integration error)")
                    version = self.meta[members[name]]["Version"]
                    if not requirement.specifier.contains(version, prereleases=True):
                        raise PrepareError(f"{who}{requirement.specifier}, but the local wheel is version {version}")
                    new = set(requirement.extras) - selected[members[name]]
                    if new:
                        selected[members[name]] |= new
                        changed = True

    # --- the run ----------------------------------------------------------------------------------------------------
    def prepare(self):
        for directory in (self.output, self.logs, self.work, self.tmp, self.wheels):
            directory.mkdir(parents=True, exist_ok=True)
        print("==> copy source export", flush=True)
        shutil.copytree(self.source, self.src, symlinks=True)
        try:
            copied, copied_digest = _snapshot.validate_export(self.src)
            if copied["components"] != self.components or copied_digest != self.manifest_digest:
                raise PrepareError("Copied export differs from the validated source")
        except (OSError, ValueError) as exc:
            raise PrepareError(str(exc)) from exc
        venv, email_venv, paper_venv = (self.output / name for name in ("venv", "email-engine-venv", "paper-service-venv"))
        python = self.make_environment(venv)
        for key, relative in self.wheel_sources():
            self.build(python, key, relative)

        # Every environment's first-party set is fixed and checked here, before any install can reach for a package.
        wheel = self.built
        self.plan_first_party()
        host = ([key for key, _ in ENGINES] + [key for key, _ in CONTENT if self.content] + (["qorqut-paper"] if self.paper else [])
                + (["qorqut-fastmail"] if self.fastmail else []) + (["qorqut-google-calendar"] if self.google_calendar else [])
                + (["qorqut-treg"] if self.treg else []))
        self.check_first_party("venv", [*host, "host"], {"host": HOST_EXTRAS})
        if self.fastmail:
            self.check_first_party("email-engine-venv", ["email-engine"])
        lock = self.src / "services/paper-service/requirements/runtime.lock"
        if self.paper:
            self.check_first_party("paper-service-venv", ["paper-service"])
            self.check_lock(lock)

        constraints = self.src / "qorqut-code/app/service-constraints.txt"
        pins = ["-c", constraints] + (["-c", self.src / "extensions/qorqut-fastmail/constraints.txt"] if self.fastmail else [])
        self.run("install host environment", [python, "-m", "pip", "install", *pins, *(wheel[key] for key in host),
                                              f"{wheel['host']}[{','.join(HOST_EXTRAS)}]"])
        environments = {"venv": self.inventory(python)}

        if self.fastmail:
            email_python = self.make_environment(email_venv)
            self.run("install email engine", [email_python, "-m", "pip", "install", wheel["email-engine"]])
            environments["email-engine-venv"] = self.inventory(email_python)

        if self.paper:
            paper_python = self.make_environment(paper_venv)
            self.run("install paper service requirements", [paper_python, "-m", "pip", "install", "--require-hashes", "-r", lock])
            self.run("install paper service", [paper_python, "-m", "pip", "install", "--no-deps", wheel["paper-service"]])
            environments["paper-service-venv"] = self.inventory(paper_python)
        self.write_receipt(environments)

    def inventory(self, python):
        result = subprocess.run([str(python), "-I", "-c", INVENTORY], cwd=self.work, env=self.env, capture_output=True, text=True)
        if result.returncode:
            raise PrepareError(f"cannot list the packages of {python}: {result.stderr.strip()}")
        data = json.loads(result.stdout)
        first_party = []
        for name, version, url in data["distributions"]:
            if not FIRST_PARTY.match(canonicalize_name(name)):
                continue
            installed_from = Path(unquote(urlparse(url).path)) if url and url.startswith("file://") else None
            if installed_from is None or installed_from.parent != self.wheels or installed_from not in self.built.values():
                raise PrepareError(f"first-party package {name} {version} in {python.parent.parent.name} was not installed from a "
                                   "locally built wheel (an index or another source supplied it)")
            first_party.append(name)
        return {"python": data["python"], "first_party": sorted(first_party),
                "packages": {name: version for name, version, _ in data["distributions"]}}

    def owner(self, relative):
        owners = [c for c in self.components if relative == c["destination"] or relative.startswith(c["destination"] + "/")]
        if not owners:
            raise PrepareError(f"{relative} is not covered by a component of SOURCE-MANIFEST.json")
        return max(owners, key=lambda c: len(c["destination"]))

    def write_receipt(self, environments):
        sources = dict(self.wheel_sources())
        wheels = []
        for key, path in self.built.items():
            metadata = self.meta[key]
            component = self.owner(sources[key])
            wheels.append({"key": key, "name": metadata["Name"], "version": metadata["Version"], "file": f"wheels/{path.name}",
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "source": sources[key],
                           "component": component["name"], "commit": component["commit"], "tree": component["tree"],
                           "build_inputs_sha256": self.wheel_inputs[key], "reused": key in self.reused})
        selection = {"content": self.content, "paper": self.paper, "fastmail": self.fastmail, "google_calendar": self.google_calendar,
                     "treg": self.treg}
        receipt = {"format": 1, "state": "prepared-not-configured", "prepared_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "python": sys.version.split()[0], "selection": selection,
                   "source_components": self.components, "source_manifest_sha256": self.manifest_digest, "wheels": wheels, "environments": environments, "not_included": NOT_INCLUDED}
        (self.output / "PREPARATION-RECEIPT.json").write_text(json.dumps(receipt, indent=2) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare local source copies, first-party wheels and Python environments from an export.")
    parser.add_argument("--source-root", required=True, help="directory written by export-unified.py")
    parser.add_argument("--output", required=True, help="new or empty output directory")
    parser.add_argument("--with-content", action="store_true", help="also install the content-creation extension wheels")
    parser.add_argument("--with-paper", action="store_true", help="also prepare the Paper service environment and host bridge")
    parser.add_argument("--with-fastmail", action="store_true",
                        help="also prepare the email engine environment and the Fastmail host bridge (nothing is configured or started)")
    parser.add_argument("--with-google-calendar", action="store_true",
                        help="also install the optional Google Calendar provider wheel (nothing is selected, configured or connected)")
    parser.add_argument("--with-treg", action="store_true",
                        help="also install the treg toolkit wheel (nothing is selected, no key is read and treg is not contacted)")
    cache = parser.add_mutually_exclusive_group()
    cache.add_argument("--wheel-cache", type=Path,
                       default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "qorqbot/wheels",
                       help="content-checked first-party wheel cache (default: user cache directory/qorqbot/wheels)")
    cache.add_argument("--no-wheel-cache", action="store_true", help="rebuild every first-party wheel")
    arguments = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 12):
            raise PrepareError(f"Python 3.12 or newer is required; this is {sys.version.split()[0]}")
        if Requirement is None:
            raise PrepareError("the 'packaging' module (installed with pip or setuptools) is needed to read wheel requirements")
        source, output = Path(arguments.source_root).resolve(), Path(os.path.abspath(arguments.output))
        if not source.is_dir():
            raise PrepareError("--source-root is not a directory")
        if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
            raise PrepareError("--output must be absent or an empty directory")
        if not output.exists() and not output.parent.is_dir():
            raise PrepareError("the parent directory of --output must exist")
        output = output.resolve() if output.exists() else output.parent.resolve() / output.name
        if inside(output, source) or inside(source, output):
            raise PrepareError("--output and --source-root must not contain one another")
        wheel_cache = None if arguments.no_wheel_cache else arguments.wheel_cache.resolve()
        if wheel_cache is not None and any(inside(wheel_cache, p) or inside(p, wheel_cache) for p in (source, output)):
            raise PrepareError("--wheel-cache must be separate from the source export and prepared output")
        preparer = Preparer(source, output, arguments.with_content, arguments.with_paper, arguments.with_fastmail,
                            arguments.with_google_calendar, wheel_cache, treg=arguments.with_treg)
        preparer.check_source()
        try:
            preparer.prepare()
        except PrepareError as exc:
            raise PrepareError(f"{exc}\nThe incomplete output remains at {output} for inspection; remove it before retrying.") from exc
    except PrepareError as exc:
        print(f"prepare-local: error: {exc}", file=sys.stderr)
        return 1
    print(f"prepared (not configured, nothing started): {output}\nreceipt: {output / 'PREPARATION-RECEIPT.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
