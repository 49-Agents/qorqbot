"""The dependency wheels of a downloadable extension: judged against the running app, then made importable.

A bundle carries the third-party wheels its code needs in ``<id>/deps/*.whl`` (``extension_bundle.py``). At a staged
install or update, before the folder is moved in, ``prepare`` judges them against the app that runs (``findings``) and
unpacks them into ``<id>/deps/site/``. At a start the registry puts ``deps/site/`` on the import path after ``code/``,
both appended after the app's own packages, and judges ``deps/site/`` again, so an app update it no longer fits makes the
extension ``unsupported``.

The judgement is the one the format 1 installer makes (``tools/distribution/native_extension.py`` ``CHECK_ENVIRONMENT``):

- every wheel is for this Python and this computer (its tags) and admits this Python version (``Requires-Python``);
- no wheel is a distribution the app already has, so nothing of the app is shadowed or replaced;
- every requirement of the wheels and of the code (``code/pyproject.toml`` ``project.dependencies``) is met by the app or
  by the wheels.

Wheels are unpacked here with ``zipfile``, not with pip: ``inspect_wheel`` has already refused links, paths leaving the
folder, files outside its RECORD and relocated ``.data`` files, so a wheel is its files at their places. The version and
requirement rules come from ``packaging``, or the copy pip carries, as for the format 1 installer.
"""
from __future__ import annotations

from email.parser import BytesParser
import importlib.metadata
import os
from pathlib import Path
import shutil
import sys
import tomllib
from types import SimpleNamespace
import zipfile

from qorqut.bootstrap.extension_bundle import DEPS, canonical_name, inspect_wheel

SITE = "site"
_NEW_SITE = ".site-new"


class DepsError(ValueError):
    """The wheels cannot be used; the message is one sentence for the owner."""


def _rules():
    """Requirement, version, specifier and tag rules from ``packaging``, or from the copy pip carries."""
    try:
        from packaging import requirements, specifiers, tags, utils, version
    except ImportError:
        try:
            from pip._vendor.packaging import requirements, specifiers, tags, utils, version
        except ImportError:
            return None
    return SimpleNamespace(Requirement=requirements.Requirement, SpecifierSet=specifiers.SpecifierSet,
                           Version=version.Version, sys_tags=tags.sys_tags, parse_tag=tags.parse_tag,
                           parse_wheel_filename=utils.parse_wheel_filename)


def wheels(folder) -> list[Path]:
    deps = Path(folder) / DEPS
    return sorted(deps.glob("*.whl")) if deps.is_dir() else []


def site(folder) -> Path:
    return Path(folder) / DEPS / SITE


def is_site(entry) -> bool:
    """Whether an import path entry is the ``deps/site`` of an extension."""
    path = Path(entry)
    return path.name == SITE and path.parent.name == DEPS


def _of_wheel(path, rules):
    found = inspect_wheel(path)
    _, _, _, tags = rules.parse_wheel_filename(path.name)
    return {"name": found["name"], "version": found["version"], "label": path.name, "tags": set(tags),
            "requires": found["requires"], "requires_python": found["requires_python"]}


def _of_site(folder, rules):
    found = []
    for info in sorted(site(folder).glob("*.dist-info")):
        metadata = BytesParser().parsebytes((info / "METADATA").read_bytes())
        wheel = BytesParser().parsebytes((info / "WHEEL").read_bytes())
        tags = set()
        for tag in wheel.get_all("Tag", []):
            tags |= set(rules.parse_tag(tag.strip()))
        found.append({"name": canonical_name(metadata["Name"]), "version": metadata["Version"], "label": info.name,
                      "tags": tags, "requires": metadata.get_all("Requires-Dist", []),
                      "requires_python": metadata.get("Requires-Python")})
    return found


def code_requirements(folder) -> list[str]:
    path = Path(folder) / "code" / "pyproject.toml"
    if not path.is_file():
        return []
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise DepsError("Its code/pyproject.toml cannot be read.") from None
    found = (value.get("project") or {}).get("dependencies") or []
    if not isinstance(found, list) or not all(isinstance(item, str) for item in found):
        raise DepsError("Its code/pyproject.toml has dependencies that are not a list of requirements.")
    return found


def app_distributions() -> dict[str, str]:
    """The distributions of the running app by canonical name: what its import path has, without any extension's
    ``deps/site``."""
    places = [entry for entry in sys.path if not is_site(entry)]
    found = {}
    for distribution in importlib.metadata.distributions(path=places):
        name = distribution.metadata.get("Name")
        if name:
            found.setdefault(canonical_name(name), distribution.version)
    return found


def _first_problem(described, requirements, app, rules, here=True) -> str | None:
    tags = set(rules.sys_tags()) if here else set()
    python = rules.Version(".".join(map(str, sys.version_info[:3])))
    given = dict(app)
    for wheel in described:
        if wheel["name"] in app:
            return f"the wheel {wheel['label']} would replace {wheel['name']}, which comes with Qorqbot"
        if wheel["name"] in given:
            return f"two wheels are of {wheel['name']}"
        if here and not wheel["tags"] & tags:
            return f"the wheel {wheel['label']} is for another Python or another computer"
        if here and wheel["requires_python"] and python not in rules.SpecifierSet(wheel["requires_python"]):
            return f"the wheel {wheel['label']} needs Python {wheel['requires_python']}"
        given[wheel["name"]] = wheel["version"]
    for raw in (*requirements, *(item for wheel in described for item in wheel["requires"])):
        try:
            requirement = rules.Requirement(raw)
        except Exception:  # noqa: BLE001 - packaging's own error type differs between its copies
            return f"the requirement {raw!r} cannot be read"
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        version = given.get(canonical_name(requirement.name))
        if requirement.url or version is None or rules.Version(version) not in requirement.specifier:
            return f"it needs {raw}, which neither Qorqbot nor its wheels give"
    return None


def findings(folder, *, installed=False) -> str | None:
    """None when the dependency wheels of ``folder`` fit the running app; else one sentence for the owner. With
    ``installed`` the unpacked ``deps/site/`` is judged, else the wheels in ``deps/``."""
    try:
        requirements = code_requirements(folder)
    except DepsError as error:
        return str(error)
    if installed:
        return _judged(requirements, lambda rules: _of_site(folder, rules), site(folder).is_dir())
    return judge(wheels(folder), requirements)


def judge(paths, requirements, *, here=True) -> str | None:
    """None when the wheel files at ``paths`` and the code's ``requirements`` fit the running app; else one sentence. The
    bundle builder asks this of the wheels it is given, against the app it runs with, with ``here`` False: a bundle for
    another computer is built with that computer's wheels, so their tags and Python are judged where it installs."""
    paths = [Path(path) for path in paths]
    return _judged(requirements, lambda rules: [_of_wheel(path, rules) for path in paths], bool(paths), here)


def _judged(requirements, describe, present, here=True) -> str | None:
    if not (requirements or present):
        return None
    rules = _rules()
    if rules is None:
        return "This Qorqbot has no way to check dependency requirements."
    try:
        described = describe(rules)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        return f"Its dependency wheels cannot be read: {error.__class__.__name__}."
    problem = _first_problem(described, requirements, app_distributions(), rules, here)
    return None if problem is None else f"Its dependency wheels do not fit this Qorqbot: {problem}."


def prepare(folder) -> None:
    """Judge the wheels of the staged ``folder`` against the running app and unpack them into ``deps/site/``; ``DepsError``
    with the sentence when they do not fit. A folder without wheels gets no ``deps/site/``. Done again from the start
    after an interruption."""
    problem = findings(folder)
    if problem:
        raise DepsError(problem)
    found = wheels(folder)
    if not found:
        return
    target, building = site(folder), Path(folder) / DEPS / _NEW_SITE
    shutil.rmtree(building, ignore_errors=True)
    if target.exists():
        shutil.rmtree(target)
    unpack(found, building)
    os.rename(building, target)


def unpack(paths, target) -> None:
    """Unpack the wheel files at ``paths`` into the new folder ``target``, as an import path entry: each wheel's files at
    their places, its ``.dist-info`` beside them. ``DepsError`` when they cannot be, and then ``target`` is gone. Only
    wheels ``inspect_wheel`` passed are given here (``wheels`` of a checked bundle, or the builder's)."""
    target = Path(target)
    target.mkdir(mode=0o700)
    try:
        for path in paths:
            with zipfile.ZipFile(path) as wheel:
                for member in wheel.infolist():
                    if member.is_dir():
                        continue
                    destination = target / member.filename
                    if destination.exists():
                        raise DepsError(f"Its dependency wheels both hold {member.filename}.")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with wheel.open(member) as source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
    except (OSError, zipfile.BadZipFile) as error:
        shutil.rmtree(target, ignore_errors=True)
        raise DepsError(f"Its dependency wheels could not be unpacked: {error.__class__.__name__}.") from None
    except DepsError:
        shutil.rmtree(target, ignore_errors=True)
        raise


def missing_site(folder) -> str | None:
    """The refusal of a folder that has dependency wheels but no ``deps/site/``: one put in place by hand, or one that
    comes with the app. Qorqbot unpacks wheels only when it installs or updates a downloaded extension."""
    if wheels(folder) and not site(folder).is_dir():
        return ("Its dependency wheels are not installed. Qorqbot installs them only when it installs the extension "
                "from the index.")
    return None
