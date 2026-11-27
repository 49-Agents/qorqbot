"""The dependency wheels of a downloadable extension: judged against the running app, then made importable.

A bundle carries the third-party wheels its code needs in ``<id>/deps/*.whl`` (``extension_bundle.py``). At a staged
install or update, before the folder is moved in, ``prepare`` judges them against the app that runs (``findings``),
unpacks them into ``<id>/deps/site/`` and judges what was unpacked as a start will. At a start the registry puts every
extension's ``code/`` on the import path before any ``deps/site/``, all of them after the app's own packages, and judges
``deps/site/`` again, so an app update it no longer fits makes the extension ``unsupported``.

The judgement is the one the format 1 installer makes (``tools/distribution/native_extension.py`` ``CHECK_ENVIRONMENT``):

- every wheel is for this Python and this computer (its tags) and admits this Python version (``Requires-Python``);
- no wheel is a distribution the app already has, so nothing of the app is shadowed or replaced;
- no wheel is another version of a distribution that another extension brings, of those whose ``deps/site/`` a start
  puts on the path (not refused, not unsupported): one Python process holds one version of a distribution;
- every requirement of the wheels and of the code (``code/pyproject.toml`` ``project.dependencies``) is met by the app or
  by the wheels, with what its extras need, for each computer its markers are judged for. A version that is there
  counts, a pre-release too.

Nothing that goes wrong while judging leaves these functions as an exception: it is a sentence, so a start goes on and a
change fails with it. A fault that is not an unreadable wheel or requirement is also logged with its traceback. Wheels are unpacked here with ``zipfile``, not with pip; every name is judged again where it is
written (``extension_bundle.relative``) and the place must be inside the folder. The version and requirement rules
come from ``packaging``, or the copy pip carries, as for the format 1 installer.
"""
from __future__ import annotations

from email.parser import BytesParser
import importlib.metadata
import logging
import os
from pathlib import Path
import shutil
import sys
import tomllib
from types import SimpleNamespace
import zipfile

from qorqut.bootstrap.extension_bundle import DEPS, canonical_name, inspect_wheel, relative

LOGGER = logging.getLogger(__name__)
SITE = "site"
_NEW_SITE = ".site-new"
NOT_JUDGED = "Its dependency wheels cannot be judged: {}."
# The manifest's platform word of each computer, from ``connectors.host_platform``, and the word of the system Qorqbot
# runs in there: on Windows it runs inside WSL, a Linux. ``extension_registry.current_platform`` and the markers below
# both read these, so they cannot drift. This changes when a native Windows app exists.
HOSTS = {"darwin": "mac", "wsl": "windows", "linux": "linux"}
RUNS_IN = {"linux": "linux", "mac": "mac", "windows": "linux"}
# The markers of the system Qorqbot runs in, as Python reports them there.
MARKERS = {"linux": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"},
           "mac": {"sys_platform": "darwin", "platform_system": "Darwin", "os_name": "posix"}}
MACHINES = {("linux", "arm64"): "aarch64"}


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


def environments(platform, platforms=()) -> list[dict]:
    """The marker values of the systems a bundle for ``platform`` runs in: one for ``linux-x86_64`` or ``mac-arm64``,
    and for ``any`` one for each of ``platforms`` (the manifest's), each as Qorqbot runs there (``RUNS_IN``)."""
    if platform == "any":
        words = [RUNS_IN[word] for word in (platforms or tuple(RUNS_IN)) if word in RUNS_IN]
        return [dict(MARKERS[word]) for word in dict.fromkeys(words)]
    word, _, machine = platform.partition("-")
    return [{**MARKERS[word], "platform_machine": MACHINES.get((word, machine), machine)}]


def _of_wheel(path, rules):
    found = inspect_wheel(path)
    _, _, _, tags = rules.parse_wheel_filename(path.name)
    return {"name": found["name"], "version": found["version"], "label": path.name, "tags": set(tags),
            "requires": found["requires"], "requires_python": found["requires_python"]}


def _metadata(info):
    return BytesParser().parsebytes((info / "METADATA").read_bytes())


def _of_site(folder, rules):
    found = []
    for info in sorted(site(folder).glob("*.dist-info")):
        metadata = _metadata(info)
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


def _app_places():
    return [entry for entry in sys.path if not is_site(entry)]


def app_distributions() -> dict[str, str]:
    """The distributions of the running app by canonical name: what its import path has, without any extension's
    ``deps/site``."""
    found = {}
    for distribution in importlib.metadata.distributions(path=_app_places()):
        name = distribution.metadata.get("Name")
        if name:
            found.setdefault(canonical_name(name), distribution.version)
    return found


def _app_requires(name) -> list[str]:
    for distribution in importlib.metadata.distributions(path=_app_places()):
        if canonical_name(distribution.metadata.get("Name") or "") == name:
            return list(distribution.requires or [])
    return []


def held(folders) -> dict[str, list[tuple[str, str]]]:
    """``{distribution: [(version, extension id), ...]}`` unpacked in the ``deps/site/`` of ``folders``, in the order
    given; the folder's name is the extension's id. A metadata folder that cannot be read is left out: its extension is
    judged at its own turn."""
    found = {}
    for folder in folders:
        folder = Path(folder)
        if not site(folder).is_dir():
            continue
        for info in sorted(site(folder).glob("*.dist-info")):
            try:
                metadata = _metadata(info)
                found.setdefault(canonical_name(metadata["Name"]), []).append((metadata["Version"], folder.name))
            except (OSError, TypeError, AttributeError):
                continue
    return found


def others(root, ext_id, counted) -> dict[str, list[tuple[str, str]]]:
    """``held`` of the installed extensions under ``root`` whose ids are in ``counted``, other than ``ext_id``, in id
    order. ``counted`` holds those whose ``deps/site/`` a start puts on the path: not refused, not unsupported."""
    folders = sorted(Path(root).iterdir()) if Path(root).is_dir() else []
    return held(folder for folder in folders if folder.name != ext_id and folder.name in set(counted))


def _first_problem(described, requirements, app, rules, *, here=True, envs=({},), other=None) -> str | None:
    tags = set(rules.sys_tags()) if here else set()
    python = rules.Version(".".join(map(str, sys.version_info[:3])))
    given, requires = dict(app), {}
    for wheel in described:
        if wheel["name"] in app:
            return f"the wheel {wheel['label']} would replace {wheel['name']}, which comes with Qorqbot"
        if wheel["name"] in given:
            return f"two wheels are of {wheel['name']}"
        for version, ext_id in (other or {}).get(wheel["name"], ()):
            if version != wheel["version"]:
                return (f"it brings {wheel['name']} {wheel['version']}, and the extension {ext_id} brings "
                        f"{wheel['name']} {version}; one Qorqbot can hold only one version of it")
        if here and not wheel["tags"] & tags:
            return f"the wheel {wheel['label']} is for another Python or another computer"
        if here and wheel["requires_python"] and not rules.SpecifierSet(wheel["requires_python"]).contains(
                python, prereleases=True):
            return f"the wheel {wheel['label']} needs Python {wheel['requires_python']}"
        given[wheel["name"]] = wheel["version"]
        requires[wheel["name"]] = wheel["requires"]
    # Each requirement with the extra it was asked under ("" for none); an extra adds what the distribution needs for it.
    queue = [(raw, "") for raw in (*requirements, *(item for wheel in described for item in wheel["requires"]))]
    asked = set()
    while queue:
        raw, extra = queue.pop(0)
        try:
            requirement = rules.Requirement(raw)
        except Exception:  # noqa: BLE001 - packaging's own error type differs between its copies
            return f"the requirement {raw!r} cannot be read"
        for env in envs:
            # Under an extra only what the extra adds counts: the rest is needed without it, judged on its own.
            if extra and (requirement.marker is None or requirement.marker.evaluate({**env, "extra": ""})):
                continue
            if requirement.marker and not requirement.marker.evaluate({**env, "extra": extra}):
                continue
            name = canonical_name(requirement.name)
            version = given.get(name)
            if (requirement.url or version is None
                    or not requirement.specifier.contains(rules.Version(version), prereleases=True)):
                return f"it needs {raw}, which neither Qorqbot nor its wheels give"
            for wanted in sorted(requirement.extras):
                if (name, wanted) not in asked:
                    asked.add((name, wanted))
                    needed = requires[name] if name in requires else _app_requires(name)
                    queue += [(item, wanted) for item in needed]
    return None


def not_judged(error, what) -> str:
    """The sentence of a fault while judging, which is not an unreadable wheel or requirement: logged with its
    traceback, so that it can be found."""
    LOGGER.warning("extension_dependencies_not_judged of=%s error=%s", what, error.__class__.__name__, exc_info=error)
    return NOT_JUDGED.format(error.__class__.__name__)


def _judged(requirements, describe, present, what, **judging) -> str | None:
    """The sentence of the judgement of ``what`` (an extension id, or the builder's folder); whatever goes wrong while
    judging is a sentence too, never an exception."""
    if not (requirements or present):
        return None
    try:
        rules = _rules()
        if rules is None:
            return "This Qorqbot has no way to check dependency requirements."
        try:
            described = describe(rules)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile) as error:
            return f"Its dependency wheels cannot be read: {error.__class__.__name__}."
        problem = _first_problem(described, requirements, app_distributions(), rules, **judging)
    except Exception as error:  # noqa: BLE001 - a judgement that fails is a sentence, so a start goes on
        return not_judged(error, what)
    return None if problem is None else f"Its dependency wheels do not fit this Qorqbot: {problem}."


def findings(folder, *, installed=False, other=None) -> str | None:
    """None when the dependency wheels of ``folder`` fit the running app and ``other`` (``others``); else one sentence
    for the owner. With ``installed`` the unpacked ``deps/site/`` is judged, else the wheels in ``deps/``. Never
    raises."""
    try:
        requirements = code_requirements(folder)
    except DepsError as error:
        return str(error)
    except Exception as error:  # noqa: BLE001
        return not_judged(error, Path(folder).name)
    what = Path(folder).name
    if installed:
        return _judged(requirements, lambda rules: _of_site(folder, rules), site(folder).is_dir(), what, other=other)
    paths = wheels(folder)
    return _judged(requirements, lambda rules: [_of_wheel(path, rules) for path in paths], bool(paths), what,
                   other=other)


def judge(paths, requirements, *, platform="any", platforms=()) -> str | None:
    """None when the wheel files at ``paths`` and the code's ``requirements`` fit the app this runs with on every
    computer a bundle for ``platform`` installs on (``environments``); else one sentence. The bundle builder asks this:
    tags and Python are judged where the bundle installs, the markers for those computers."""
    paths = [Path(path) for path in paths]
    return _judged(requirements, lambda rules: [_of_wheel(path, rules) for path in paths], bool(paths), "build",
                   here=False, envs=environments(platform, platforms))


def prepare(folder, *, other=None) -> None:
    """Judge the wheels of the staged ``folder`` against the running app and ``other``, unpack them into ``deps/site/``
    and judge what was unpacked as a start will; ``DepsError`` with the sentence when they do not fit, and then no
    ``deps/site/`` is left. A folder without wheels gets none. Done again from the start after an interruption."""
    problem = findings(folder, other=other)
    if problem:
        raise DepsError(problem)
    found = wheels(folder)
    if not found:
        return
    target, building = site(folder), Path(folder) / DEPS / _NEW_SITE
    try:
        shutil.rmtree(building, ignore_errors=True)
        if target.exists():
            shutil.rmtree(target)
        unpack(found, building)
        os.rename(building, target)
    except DepsError:
        raise
    except Exception as error:  # noqa: BLE001
        shutil.rmtree(building, ignore_errors=True)
        LOGGER.warning("extension_dependencies_not_unpacked of=%s error=%s", Path(folder).name,
                       error.__class__.__name__, exc_info=error)
        raise DepsError(f"Its dependency wheels could not be unpacked: {error.__class__.__name__}.") from None
    problem = findings(folder, installed=True, other=other)
    if problem:
        shutil.rmtree(target, ignore_errors=True)
        raise DepsError(problem)


def unpack(paths, target) -> None:
    """Unpack the wheel files at ``paths`` into the new folder ``target``, as an import path entry: each wheel's files at
    their places, its ``.dist-info`` beside them. ``DepsError`` when they cannot be, and then ``target`` is gone. Each
    name is judged here again, whatever judged the wheel before, and the place written must be inside ``target``."""
    target = Path(target)
    target.mkdir(mode=0o700)
    inside = target.resolve()
    try:
        for path in paths:
            with zipfile.ZipFile(path) as wheel:
                for member in wheel.infolist():
                    if member.is_dir():
                        continue
                    try:
                        relative(member.filename)
                    except ValueError:
                        raise DepsError(f"The wheel {Path(path).name} holds an unsafe name.") from None
                    destination = target / member.filename
                    if not destination.resolve().is_relative_to(inside):
                        raise DepsError(f"The wheel {Path(path).name} holds an unsafe name.")
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
