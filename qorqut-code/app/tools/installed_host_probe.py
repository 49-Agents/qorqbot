"""Installed identity, metadata and import probe of one prepared environment; run by check_installed_host.py as ``python -I -B``.

It reads one JSON configuration and prints one JSON object. It runs no test, installs and repairs nothing, writes nothing and
imports only the top-level packages of the first-party wheels it is given. Everything it finds wrong is returned as a failure.
"""
import hashlib
import importlib
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlparse

FIRST_PARTY = re.compile(r"qorqut-")
config = json.loads(Path(sys.argv[1]).read_text())
wheels = config["wheels"]
wheel_directory = Path(config["wheel_directory"]).resolve()
failures = []


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def inside(path, parent):
    return path == parent or parent in path.parents


def listing(names, limit=8):
    return ", ".join(sorted(names)[:limit]) + (f" (and {len(names) - limit} more)" if len(names) > limit else "")


# --- the interpreter is the owned, isolated environment and reaches nothing outside it ------------------------------------
prefix = Path(sys.prefix).resolve()
if prefix != Path(config["environment"]).resolve():
    failures.append(f"the interpreter belongs to {prefix}, not to the environment {config['environment']}")
if sys.prefix == sys.base_prefix:
    failures.append("the interpreter is not a virtual environment")
if not sys.flags.isolated:
    failures.append("the probe did not run isolated (-I)")
if sys.version.split()[0] != config["python"]:
    failures.append(f"the interpreter is Python {sys.version.split()[0]}, the receipt records {config['python']}")
try:
    settings = {key.strip(): value.strip().lower() for key, _, value in
                (line.partition("=") for line in (prefix / "pyvenv.cfg").read_text().splitlines())}
    if settings.get("include-system-site-packages") == "true":
        failures.append("the environment includes the system site-packages")
except OSError:
    failures.append("the environment has no pyvenv.cfg")
bases = {Path(sys.base_prefix).resolve(), Path(sys.base_exec_prefix).resolve()}
for entry in sys.path:
    path = Path(entry).resolve()
    standard_library = any(inside(path, base) for base in bases) and not {"site-packages", "dist-packages"} & set(path.parts)
    if not (inside(path, prefix) or standard_library):
        failures.append(f"sys.path entry outside the environment: {entry}")

# --- installed first-party distributions come only from the receipt's wheels --------------------------------------------
installed = {}
for distribution in metadata.distributions():
    if distribution.metadata["Name"]:
        installed.setdefault(canonical(distribution.metadata["Name"]), []).append(distribution)
checked = {}
for name, group in sorted(installed.items()):
    if len(group) > 1:
        failures.append(f"{name} is installed {len(group)} times")
    distribution, wheel = group[0], wheels.get(name)
    if FIRST_PARTY.match(name) and wheel is None:
        failures.append(f"first-party distribution {name} {distribution.version} is not recorded for this environment")
    text = distribution.read_text("direct_url.json")
    if text is None:
        if FIRST_PARTY.match(name):
            failures.append(f"{name} has no direct-URL record: it was not installed from a local wheel file")
        continue
    origin = json.loads(text)
    url = urlparse(origin.get("url", ""))
    if wheel is None:
        failures.append(f"{name} has a direct-URL origin ({origin.get('url')}) that no receipt wheel explains")
    elif "vcs_info" in origin or "dir_info" in origin or url.scheme != "file":
        failures.append(f"{name} was installed from a direct-URL origin that is not a local wheel file: {origin.get('url')}")
    elif Path(unquote(url.path)).resolve() != wheel_directory / wheel["file"]:
        failures.append(f"{name} was installed from {unquote(url.path)}, not from the receipt wheel {wheel['file']}")
    else:
        recorded = origin.get("archive_info", {}).get("hashes", {}).get("sha256")
        if recorded is not None and recorded != wheel["sha256"]:
            failures.append(f"{name} records archive SHA-256 {recorded}, the receipt wheel has {wheel['sha256']}")
for name in sorted(set(wheels) - set(installed)):
    failures.append(f"receipt wheel {name} {wheels[name]['version']} is not installed in this environment")

# --- the installed Python and package resource bytes equal the wheel's -------------------------------------------------
for name, wheel in sorted(wheels.items()):
    if name not in installed:
        continue
    distribution = installed[name][0]
    if distribution.version != wheel["version"]:
        failures.append(f"{name} is installed as version {distribution.version}, the receipt wheel is {wheel['version']}")
    missing, changed, outside = [], [], []
    for member, expected in wheel["members"].items():
        location = Path(distribution.locate_file(member))
        try:
            actual = hashlib.sha256(location.read_bytes()).hexdigest()
        except OSError:
            missing.append(member)
            continue
        if actual != expected:
            changed.append(member)
        if not inside(location.resolve(), prefix):
            outside.append(member)
    for what, names in (("missing from the installation", missing), ("different from the wheel", changed),
                        ("located outside the environment", outside)):
        if names:
            failures.append(f"{name}: {len(names)} wheel file(s) {what}: {listing(names)}")
    members = wheel["members"]
    checked[name] = {"version": distribution.version, "wheel": wheel["file"],
                     "python_files_verified": sum(1 for member in members if member.endswith(".py")) - sum(1 for member in missing + changed if member.endswith(".py")),
                     "resource_files_verified": sum(1 for member in members if not member.endswith(".py")) - sum(1 for member in missing + changed if not member.endswith(".py")),
                     "imported": []}

# --- the wheel's top-level packages import from the environment ------------------------------------------------------------
owned = {}
for name, wheel in sorted(wheels.items()):
    for top in wheel["top_level"]:
        owned[top] = name
        try:
            module = importlib.import_module(top)
        except BaseException as error:
            failures.append(f"{name}: importing {top} failed: {type(error).__name__}: {error}")
            continue
        locations = [module.__file__] if getattr(module, "__file__", None) else list(getattr(module, "__path__", []))
        if not locations or not all(inside(Path(location).resolve(), prefix) for location in locations):
            failures.append(f"{name}: {top} was imported from outside the environment: {locations}")
        elif name in checked:
            checked[name]["imported"].append(top)
for module_name, module in tuple(sys.modules.items()):
    path = getattr(module, "__file__", None)
    if module_name.split(".")[0] in owned and path and not inside(Path(path).resolve(), prefix):
        failures.append(f"module {module_name} was imported from outside the environment: {path}")

print(json.dumps({"interpreter": sys.executable, "python": sys.version.split()[0], "prefix": str(prefix), "distributions": checked,
                  "failures": failures}))
