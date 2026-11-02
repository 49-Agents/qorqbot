"""Verified provenance of the installed host sources and the selected extension implementations.

The migration coordinator reuses three existing contracts instead of inventing a new one:

* ``source_snapshot.validate_export`` proves the exported components are byte-exact to their manifest (the caller has done
  that before it reaches this module);
* a *prepared installation* (``<prepared>/src`` next to ``PREPARATION-RECEIPT.json``) is verified with the production startup
  contract ``initialize.require_installed`` plus the byte comparison ``tools/check_installed_host.py`` makes between every
  installed first-party file and its receipt wheel;
* a plain export (a source checkout or a bare tree) is verified by comparing the loaded package directory with the exported
  component's package directory, file by file.

Every check hashes fresh bytes; nothing is cached by mtime. An installed package that is not the exported component the plan
names for a selected factory is refused, whatever its name.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import sysconfig
import zipfile

from qorqut_operations.contracts import DomainError

PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}\Z")
RECEIPT = "PREPARATION-RECEIPT.json"
MAX_PACKAGE_FILES = 20_000
MAX_WHEEL_MEMBERS = 50_000


def _refuse(message, **details):
    return DomainError("operation_conflict", message, details=details or None)


def split_component(value: str):
    """``component`` or ``component:subpath`` -> (component name, safe relative posix subpath or '')."""
    name, _, subpath = value.partition(":")
    parts = PurePosixPath(subpath).parts if subpath else ()
    if not name or (subpath and (PurePosixPath(subpath).is_absolute() or any(
            part in {"", ".", ".."} or part.lower() == ".git" for part in parts) or "\\" in subpath)):
        raise DomainError("invalid_configuration", "A release component reference is not a component name with an "
                          "optional safe subpath")
    return name, "/".join(parts)


def prepared_root(export_root: Path):
    """The prepared installation an export root is the ``src`` of, or None for a plain export."""
    export_root = Path(export_root)
    candidate = export_root.parent / RECEIPT
    if export_root.name == "src" and (candidate.exists() or candidate.is_symlink()):
        return export_root.parent
    return None


def exported_source(export_root: Path, component: dict, subpath: str) -> Path:
    destination = component.get("destination")
    if (not isinstance(destination, str) or not destination or PurePosixPath(destination).is_absolute()
            or ".." in PurePosixPath(destination).parts):
        raise DomainError("invalid_configuration", "A release component destination is not a safe relative path",
                          details={"component": component.get("name")})
    path = Path(export_root) / destination / subpath if subpath else Path(export_root) / destination
    if path.is_symlink() or not path.is_dir():
        raise DomainError("invalid_configuration", "A manifest-selected component source is missing or unsafe",
                          details={"component": component.get("name"), "subpath": subpath})
    return path


def descendant_exclusions(component: dict, components: list) -> list:
    destination = component["destination"]
    return [item["destination"][len(destination) + 1:] for item in components
            if item["destination"].startswith(destination + "/")]


def package_files(directory: Path) -> dict | None:
    """relative path -> sha256 of every regular file below a package directory (bytecode caches ignored); None when it holds
    a symlink, another non-regular entry or exceeds the bound."""
    import os
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        return None
    result = {}

    def visit(path: Path, prefix: str) -> bool:
        try:
            entries = sorted(os.scandir(path), key=lambda item: item.name)
        except OSError:
            return False
        for entry in entries:
            if entry.name in {"__pycache__", ".pytest_cache"} or entry.name.endswith(".pyc"):
                continue
            relative = prefix + entry.name
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError:
                return False
            if stat.S_ISDIR(mode):
                if not visit(Path(entry.path), relative + "/"):
                    return False
            elif stat.S_ISREG(mode):
                try:
                    result[relative] = hashlib.sha256(Path(entry.path).read_bytes()).hexdigest()
                except OSError:
                    return False
            else:
                return False
            if len(result) > MAX_PACKAGE_FILES:
                return False
        return True

    return result if visit(directory, "") else None


def same_package(installed: Path, exported: Path) -> bool:
    """The loaded package directory holds nothing that is not exported byte-for-byte, and every exported Python module."""
    installed_files, exported_files = package_files(installed), package_files(exported)
    if installed_files is None or exported_files is None:
        return False
    if any(exported_files.get(name) != digest for name, digest in installed_files.items()):
        return False
    return all(name in installed_files for name in exported_files if name.endswith(".py"))


def loaded_package(package: str) -> Path:
    """The directory of an already imported regular package, never of a namespace package, a bare module or a missing one."""
    module = sys.modules.get(package)
    origin = getattr(module, "__file__", None)
    if not origin or Path(origin).name != "__init__.py":
        raise _refuse("A selected extension's package is not a loaded regular package", package=package)
    return Path(origin).resolve().parent


def wheel_members(wheel: Path) -> dict:
    """member name -> sha256 for the wheel's importable files (as ``tools/check_installed_host.py`` reads them)."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            if len(names) > MAX_WHEEL_MEMBERS:
                raise _refuse("A receipt wheel exceeds its member bound", wheel=wheel.name)
            return {name: hashlib.sha256(archive.read(name)).hexdigest() for name in names
                    if "__pycache__" not in name.split("/") and not name.endswith(".pyc")
                    and not name.split("/")[0].endswith((".dist-info", ".data"))}
    except (OSError, zipfile.BadZipFile) as exc:
        raise _refuse("A receipt wheel cannot be read", wheel=wheel.name) from exc


def wheel_matches_installed(members: dict, purelib: Path) -> list:
    """The member names whose installed file is absent, behind a symlink, not regular or differs from the wheel's bytes."""
    import os
    real = Path(os.path.realpath(purelib))
    mismatched = []
    for name, digest in sorted(members.items()):
        path = real / name
        try:
            plain = Path(os.path.realpath(path)) == path and path.is_file()
            if not plain or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                mismatched.append(name)
        except OSError:
            mismatched.append(name)
    return mismatched


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_evidence(prepared: Path, manifest: dict, manifest_sha: str, factories: list, packages: dict) -> dict:
    """A prepared installation: the receipt, the running interpreter, every first-party wheel and each selected package."""
    from importlib import metadata
    from bazarlyk.bootstrap.initialize import InitializationError, require_installed
    receipt_path = prepared / RECEIPT
    try:
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise _refuse("The preparation receipt is missing or unsafe")
        raw = receipt_path.read_bytes()
        receipt = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise _refuse("The preparation receipt is unreadable") from exc
    if (not isinstance(receipt, dict) or receipt.get("format") != 1 or receipt.get("state") != "prepared-not-configured"
            or receipt.get("source_manifest_sha256") != manifest_sha or receipt.get("source_components") != manifest["components"]):
        raise _refuse("The preparation receipt does not describe the verified release export")
    try:
        require_installed(prepared, receipt, factories)
    except InitializationError as exc:
        raise _refuse("The running installation is not the prepared environment: " + str(exc)) from exc
    components = {item["name"]: item for item in manifest["components"]}
    wheels = {_canonical(entry["name"]): entry for entry in receipt["wheels"]}
    purelib = Path(sysconfig.get_paths()["purelib"])
    first_party = [_canonical(name) for name in receipt["environments"]["venv"]["first_party"]]
    verified = {}
    for name in sorted(first_party):
        entry = wheels.get(name)
        if entry is None:
            raise _refuse("A first-party distribution is not one of the receipt wheels", distribution=name)
        wheel_path = prepared / entry["file"]
        mismatched = wheel_matches_installed(wheel_members(wheel_path), purelib)
        if mismatched:
            raise _refuse("An installed first-party file is not the receipt wheel's file",
                          distribution=name, files=mismatched[:10])
        owner = components.get(entry.get("component"))
        if owner is None or (owner["commit"], owner["tree"]) != (entry.get("commit"), entry.get("tree")):
            raise _refuse("A receipt wheel is not built from the exported component it names", distribution=name)
        verified[name] = {"sha256": entry["sha256"], "component": entry["component"], "commit": entry["commit"]}
    owners = metadata.packages_distributions()
    for package, (component_name, _subpath) in sorted(packages.items()):
        providing = {_canonical(name) for name in owners.get(package, ())} & set(verified)
        if not providing or any(verified[name]["component"] != component_name for name in providing):
            raise _refuse("A selected extension's package is not provided by the receipt wheel built from its named "
                          "exported component", package=package, component=component_name)
    return {"mode": "prepared", "receipt_sha256": hashlib.sha256(raw).hexdigest(), "wheels": verified}


def extension_evidence(selected_set, entries, release: dict, manifest: dict, manifest_sha: str, export_root: Path) -> dict:
    """Bind every selected factory's loaded package (and each constructed extension) to its exported component."""
    from bazarlyk.extensions import validate_entries
    factories = validate_entries(entries)
    mapping = release.get("extension_components") or {}
    packages = sorted({module.partition(".")[0] for module, _attribute, _config in factories})
    if not factories and mapping:
        raise DomainError("invalid_configuration", "The release names extension components but selects no extension")
    if factories and (not isinstance(mapping, dict) or set(mapping) != set(packages)
                      or not all(PACKAGE.fullmatch(name) and isinstance(value, str) for name, value in mapping.items())):
        raise DomainError("invalid_configuration", "The release needs one extension component reference for exactly "
                          "each selected factory package", details={"packages": packages})
    components = {item["name"]: item for item in manifest["components"]}
    resolved = {}
    for package in packages:
        name, subpath = split_component(mapping[package])
        if name not in components:
            raise DomainError("invalid_configuration", "An extension component reference names no exported component",
                              details={"package": package})
        resolved[package] = (name, subpath)
    prepared = prepared_root(export_root)
    installation = None
    if prepared is not None:
        installation = installed_evidence(prepared, manifest, manifest_sha,
                                          [f"{module}:{attribute}" for module, attribute, _config in factories], resolved)
    summary = {}
    for package, (name, subpath) in sorted(resolved.items()):
        component = components[name]
        exported = exported_source(export_root, component, subpath) / package
        installed = loaded_package(package)
        if not same_package(installed, exported):
            raise _refuse("A selected extension's loaded package is not its exported component's source",
                          package=package, component=name)
        summary[package] = {"component": name, "subpath": subpath, "commit": component["commit"],
                            "tree": component["tree"], "files": len(package_files(installed) or ())}
    for extension in selected_set.extensions:
        module = type(extension).__module__.partition(".")[0]
        if module not in resolved:
            raise _refuse("A selected extension is implemented by a package its factory selection does not bind",
                          extension=extension.id, package=module)
    return {"packages": summary, "installation": installation}
