"""Verify a prepared installation against its own preparation receipt and write one new verification report.

    python tools/check_installed_host.py --prepared-root PREPARED --output NEW-REPORT.json

``PREPARED`` is the output directory of ``prepare-local.py``. Its ``PREPARATION-RECEIPT.json`` and the ``SOURCE-MANIFEST.json``
of its ``src`` copy are the only truth; nothing is read from sibling checkouts, Git history or an older source receipt. The
checker verifies, and never installs, repairs or modifies anything in the export, the prepared sources, the wheels or the
environments (every interpreter runs ``-I -B``), and contacts no service or provider:

* the manifest components equal the receipt's ``source_components`` and own each wheel's source directory, commit and tree;
* every receipt wheel exists under ``wheels/`` with the recorded SHA-256, name and version;
* in each environment the receipt lists (its owned ``bin/python``), every first-party distribution is one of the receipt's
  wheels, was installed from that exact local wheel file (a missing wheel, another file, a VCS/directory/URL origin or an
  index install is refused), and its installed Python and package resource files equal the wheel's bytes;
* the wheel's top-level packages import from inside the environment, and nothing on ``sys.path`` reaches outside it;
* ``pip check`` passes.

The report lists exactly the environments, wheels and components checked and every failure; the exit status is 1 when it holds
any. It runs no behavioral test: the installed behavior is accepted by the final assembly, not here.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import zipfile

PROBE = Path(__file__).with_name("installed_host_probe.py")
HEX40 = re.compile(r"[0-9a-f]{40}")
# The wheels every preparation builds, and the ones added by each selected extension (prepare-local.py's receipt keys).
BASE_WHEELS = ("qorqut-operations", "qorqut-context", "document-history", "learning-library", "host", "email-engine")
SELECTED_WHEELS = {"paper": ("paper-service", "qorqut-paper"), "content": ("content-library", "qorqut-content")}
BASE_ENVIRONMENTS = ("venv", "email-engine-venv")
PAPER_ENVIRONMENT = "paper-service-venv"
NOT_CHECKED = ["installed behavior (final assembly's acceptance)", "third-party package contents",
               "services, providers and live data (never contacted)"]


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def digest(content):
    return hashlib.sha256(content).hexdigest()


def inside(path, parent):
    return path == parent or parent in path.parents


class Verifier:
    def __init__(self, prepared):
        self.prepared, self.failures = prepared, []
        self.receipt, self.components, self.wheels, self.environments = {}, [], {}, {}

    def fail(self, message):
        self.failures.append(message)

    def read_json(self, path, what):
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            self.fail(f"{what} is missing or unreadable ({path}): {error}")
            return None
        if not isinstance(value, dict) or value.get("format") != 1:
            self.fail(f"{what} is not a format 1 record")
            return None
        return value

    # --- receipt and source manifest --------------------------------------------------------------------------------
    def inputs(self):
        receipt = self.read_json(self.prepared / "PREPARATION-RECEIPT.json", "PREPARATION-RECEIPT.json")
        manifest = self.read_json(self.prepared / "src" / "SOURCE-MANIFEST.json", "src/SOURCE-MANIFEST.json")
        if receipt is None or manifest is None:
            return False
        self.receipt = receipt
        if receipt.get("state") != "prepared-not-configured":
            self.fail(f"the receipt state is {receipt.get('state')!r}, not 'prepared-not-configured'")
        components = manifest.get("components")
        if not isinstance(components, list) or not components or components != receipt.get("source_components"):
            self.fail("the components of SOURCE-MANIFEST.json differ from the receipt's source_components")
            return False
        for component in components:
            if not (isinstance(component, dict) and HEX40.fullmatch(str(component.get("commit"))) and HEX40.fullmatch(str(component.get("tree")))
                    and isinstance(component.get("name"), str) and isinstance(component.get("destination"), str)):
                self.fail(f"a manifest component lacks a name, destination, 40-hex commit or tree: {component!r}")
                return False
        self.components = components
        return True

    def owner(self, relative):
        owners = [c for c in self.components if relative == c["destination"] or relative.startswith(c["destination"] + "/")]
        return max(owners, key=lambda c: len(c["destination"])) if owners else None

    # --- wheels ------------------------------------------------------------------------------------------------------------
    def check_wheels(self):
        keys = set()
        for entry in self.receipt.get("wheels", []):
            key = entry.get("key", "?")
            keys.add(key)
            parts = Path(str(entry.get("file"))).parts
            path = self.prepared.joinpath(*parts)
            if len(parts) != 2 or parts[0] != "wheels" or not parts[1].endswith(".whl") or path.is_symlink() or not path.is_file():
                self.fail(f"wheel {key}: {entry.get('file')} is missing from wheels/ or is not a plain wheel file")
                continue
            data = path.read_bytes()
            if digest(data) != entry.get("sha256"):
                self.fail(f"wheel {key}: {path.name} has SHA-256 {digest(data)}, the receipt records {entry.get('sha256')}")
                continue
            try:
                with zipfile.ZipFile(path) as archive:
                    metadata = [name for name in archive.namelist() if name.count("/") == 1 and name.endswith(".dist-info/METADATA")]
                    if len(metadata) != 1:
                        raise ValueError("it has no single dist-info METADATA")
                    fields = dict(line.split(": ", 1) for line in archive.read(metadata[0]).decode().split("\n\n")[0].splitlines()
                                  if ": " in line and not line.startswith(" "))
                    members = {name: digest(archive.read(name)) for name in archive.namelist()
                               if not name.endswith("/") and name.split("/")[0] != "__pycache__"
                               and not name.split("/")[0].endswith((".dist-info", ".data"))}
            except (zipfile.BadZipFile, ValueError, UnicodeDecodeError) as error:
                self.fail(f"wheel {key}: {path.name} cannot be read: {error}")
                continue
            expected = (entry.get("name"), entry.get("version"))
            if (fields.get("Name"), fields.get("Version")) != expected or not path.name.lower().startswith(
                    f"{re.sub('[-_.]+', '_', str(expected[0])).lower()}-{str(expected[1]).lower()}-"):
                self.fail(f"wheel {key}: {path.name} carries {fields.get('Name')} {fields.get('Version')}, the receipt records "
                          f"{expected[0]} {expected[1]}")
                continue
            owner = self.owner(str(entry.get("source")))
            if owner is None or (owner["name"], owner["commit"], owner["tree"]) != (entry.get("component"), entry.get("commit"), entry.get("tree")):
                self.fail(f"wheel {key}: its source {entry.get('source')} is not owned by the component, commit and tree the receipt records")
                continue
            tops = {(name.split("/")[0][:-3] if name.split("/")[0].endswith(".py") else name.split("/")[0]) for name in members}
            self.wheels[canonical(entry["name"])] = {"key": key, "name": entry["name"], "version": entry["version"], "file": path.name,
                                                      "sha256": entry["sha256"], "component": owner["name"], "commit": owner["commit"],
                                                      "members": members, "top_level": sorted(tops)}
        selection = self.receipt.get("selection") or {}
        required = set(BASE_WHEELS) | {key for option, wheels in SELECTED_WHEELS.items() if selection.get(option) for key in wheels}
        if required - keys:
            self.fail(f"the receipt has no wheel for: {', '.join(sorted(required - keys))}")
        unselected = {key for option, wheels in SELECTED_WHEELS.items() if not selection.get(option) for key in wheels} & keys
        if unselected:
            self.fail(f"the receipt carries wheels of extensions it did not select: {', '.join(sorted(unselected))}")

    # --- environments ------------------------------------------------------------------------------------------------------
    def check_environments(self):
        recorded = self.receipt.get("environments")
        if not isinstance(recorded, dict):
            self.fail("the receipt records no environments")
            return
        selection = self.receipt.get("selection") or {}
        required = set(BASE_ENVIRONMENTS) | ({PAPER_ENVIRONMENT} if selection.get("paper") else set())
        if required - set(recorded):
            self.fail(f"the receipt records no environment for: {', '.join(sorted(required - set(recorded)))}")
        if PAPER_ENVIRONMENT in recorded and not selection.get("paper"):
            self.fail(f"the receipt records {PAPER_ENVIRONMENT} without selecting Paper")
        installed_somewhere = set()
        for name, record in sorted(recorded.items()):
            python = self.prepared / name / "bin" / "python"
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or not python.exists():
                self.fail(f"{name}: the environment has no interpreter at {name}/bin/python")
                continue
            expected = {}
            for first_party in record.get("first_party", []):
                wheel = self.wheels.get(canonical(first_party))
                if wheel is None:
                    self.fail(f"{name}: the receipt lists first-party {first_party}, but no verified wheel provides it")
                else:
                    expected[canonical(first_party)] = wheel
            installed_somewhere |= set(expected)
            config = {"environment": str(self.prepared / name), "python": record.get("python"), "wheel_directory": str(self.prepared / "wheels"),
                      "wheels": {key: {k: wheel[k] for k in ("version", "file", "sha256", "members", "top_level")} for key, wheel in expected.items()}}
            result = self.probe(name, python, config)
            self.environments[name] = {"interpreter": str(python), "python": (result or {}).get("python"),
                                       "first_party_checked": sorted(wheel["name"] for wheel in expected.values()),
                                       "distributions": (result or {}).get("distributions", {}), "pip_check": self.pip_check(name, python)}
        for key in sorted(set(self.wheels) - installed_somewhere):
            self.fail(f"wheel {self.wheels[key]['key']} ({self.wheels[key]['name']}) is not listed as installed in any recorded environment")

    def run_isolated(self, command, scratch):
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(scratch / "home"), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
                       "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}
        return subprocess.run(command, cwd=scratch, env=environment, capture_output=True, text=True, timeout=900)

    def probe(self, name, python, config):
        with tempfile.TemporaryDirectory(prefix="qorqut-installed-check-") as scratch:
            scratch = Path(scratch)
            (scratch / "home").mkdir()
            (scratch / "probe-config.json").write_text(json.dumps(config))
            result = self.run_isolated([str(python), "-I", "-B", str(PROBE), str(scratch / "probe-config.json")], scratch)
        try:
            outcome = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            self.fail(f"{name}: the probe produced no result (exit {result.returncode}): {result.stderr.strip()[-400:]}")
            return None
        self.failures.extend(f"{name}: {message}" for message in outcome["failures"])
        return outcome

    def pip_check(self, name, python):
        with tempfile.TemporaryDirectory(prefix="qorqut-installed-check-") as scratch:
            (Path(scratch) / "home").mkdir()
            result = self.run_isolated([str(python), "-I", "-B", "-m", "pip", "check"], Path(scratch))
        text = (result.stdout + result.stderr).strip()
        if result.returncode:
            self.fail(f"{name}: pip check failed: {text[-600:]}")
        return {"ok": result.returncode == 0, "output": text[-600:]}

    def report(self):
        return {"format": 1, "kind": "installed-host-verification", "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "prepared_root": str(self.prepared), "passed": not self.failures,
                "receipt": {"prepared_at": self.receipt.get("prepared_at"), "python": self.receipt.get("python"),
                            "selection": self.receipt.get("selection")},
                "source_components": self.components,
                "wheels": [{k: wheel[k] for k in ("key", "name", "version", "file", "sha256", "component", "commit")}
                           for wheel in self.wheels.values()],
                "environments": self.environments, "failures": self.failures, "not_checked": NOT_CHECKED}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--prepared-root", required=True, type=Path, help="output directory of prepare-local.py")
    parser.add_argument("--output", required=True, type=Path, help="new JSON verification report (must not exist)")
    options = parser.parse_args(argv)
    prepared, output = options.prepared_root.resolve(), Path(os.path.abspath(options.output))
    if not prepared.is_dir():
        print(f"check_installed_host: --prepared-root {prepared} is not a directory", file=sys.stderr)
        return 2
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        print(f"check_installed_host: --output {output} must be a new file in an existing directory", file=sys.stderr)
        return 2
    output = output.parent.resolve() / output.name
    if inside(output, prepared):
        print("check_installed_host: --output must lie outside the prepared root, which is never modified", file=sys.stderr)
        return 2
    verifier = Verifier(prepared)
    if verifier.inputs():
        verifier.check_wheels()
        verifier.check_environments()
    report = verifier.report()
    with open(output, "x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    checked = ", ".join(f"{name} ({len(value['first_party_checked'])} first-party)" for name, value in report["environments"].items())
    print(f"environments checked: {checked or 'none'}\nreport: {output}")
    if report["failures"]:
        print(f"FAILED ({len(report['failures'])}):\n  " + "\n  ".join(report["failures"]), file=sys.stderr)
        return 1
    print("passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
