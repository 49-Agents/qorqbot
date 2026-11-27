"""The index of downloadable extensions: what can be installed, which version, and where each bundle is.

The index is one JSON file, ``extensions-index.json``, written at release by ``tools/distribution/extension_index.py``
from the downloadable extension folders and their format 2 bundles (``qorqut/bootstrap/extension_bundle.py``):

```
{"format": 1, "app_version": "1.7.0",
 "extensions": [{"id", "version", "name", "summary", "requires_qorqbot", "platforms", "setup_kind",
                 "requires_extensions", "folder_sha256", "bundles": [{"platform", "url", "sha256", "size"}]}]}
```

Name, summary, platforms and set-up kind are copies of the manifest, so an extension that is not installed can be shown.
``load`` reads one index file and refuses a malformed one with ``ExtensionIndexError`` and a plain reason.

The app's own index is the file a release puts next to the app (``qorqut-code/extensions-index.json``); the trusted
installation configuration may name another file under ``extensions_index``, for test installations (``index_path``).
``offered`` gives the registry the entries that are not installed, as ``not_installed`` or ``unsupported``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import platform as _platform
from typing import Any

from qorqut.bootstrap.extension_bundle import BUNDLE_PLATFORM, SHA256, unique_object
from qorqut.extension_contract import PLATFORMS, SETUP_KINDS, SPECIFIER, VERSION
from qorqut.extensions import EXTENSION_ID

FORMAT = 1
INDEX_KEYS = frozenset({"format", "app_version", "extensions"})
ENTRY_KEYS = frozenset({"id", "version", "name", "summary", "requires_qorqbot", "platforms", "setup_kind",
                        "requires_extensions", "folder_sha256", "bundles"})
BUNDLE_KEYS = frozenset({"platform", "url", "sha256", "size"})
ANY = "any"
INDEX_FILE = "extensions-index.json"
CONFIG_KEY = "extensions_index"
NOT_INSTALLED, UNSUPPORTED = "not_installed", "unsupported"
LOGGER = logging.getLogger(__name__)
# Processor names as Python reports them on Linux and macOS, written the one way bundle platforms use.
_MACHINES = {"amd64": "x86_64", "aarch64": "arm64"}


class ExtensionIndexError(ValueError):
    """The index file cannot be used; the message says why in one sentence."""


@dataclass(frozen=True)
class Bundle:
    platform: str
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Entry:
    id: str
    version: str
    name: str
    summary: str
    requires_qorqbot: str
    platforms: tuple[str, ...]
    setup_kind: str
    requires_extensions: tuple[str, ...]
    folder_sha256: str
    bundles: tuple[Bundle, ...]


@dataclass(frozen=True)
class Index:
    app_version: str
    entries: tuple[Entry, ...]

    def entry(self, extension_id: str) -> Entry | None:
        return next((item for item in self.entries if item.id == extension_id), None)

    def entry_for(self, extension_id: str, platform: str) -> tuple[Entry, Bundle] | None:
        """The entry and the bundle to download on ``platform``: the bundle made for it, else the one for any platform.

        None when the index has no such extension or no bundle for that platform.
        """
        entry = self.entry(extension_id)
        if entry is None:
            return None
        chosen = {item.platform: item for item in entry.bundles}
        bundle = chosen.get(platform) or chosen.get(ANY)
        return (entry, bundle) if bundle is not None else None


def _keys(value: Any, keys: frozenset, where: str) -> dict:
    if not isinstance(value, dict):
        raise ExtensionIndexError(f"{where} must be a JSON object.")
    if set(value) != keys:
        missing, extra = sorted(keys - set(value)), sorted(set(value) - keys)
        detail = f"lacks {missing[0]!r}" if missing else f"has the unknown field {extra[0]!r}"
        raise ExtensionIndexError(f"{where} {detail}.")
    return value


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExtensionIndexError(f"{where} must be a non-empty string.")
    return value


def _bundle(value: Any, where: str) -> Bundle:
    value = _keys(value, BUNDLE_KEYS, where)
    if not isinstance(value["sha256"], str) or not SHA256.fullmatch(value["sha256"]):
        raise ExtensionIndexError(f"{where} has a sha256 that is not 64 lowercase hexadecimal characters.")
    if type(value["size"]) is not int or value["size"] < 0:
        raise ExtensionIndexError(f"{where} has a size that is not a whole number of bytes.")
    if not isinstance(value["platform"], str) or not BUNDLE_PLATFORM.fullmatch(value["platform"]):
        raise ExtensionIndexError(f"{where} has a platform that is not any or a platform and processor such as "
                                  "linux-x86_64.")
    return Bundle(value["platform"], _text(value["url"], f"{where} url"), value["sha256"], value["size"])


def _entry(value: Any, number: int) -> Entry:
    where = f"The index entry {number}"
    value = _keys(value, ENTRY_KEYS, where)
    if not isinstance(value["id"], str) or not EXTENSION_ID.fullmatch(value["id"]):
        raise ExtensionIndexError(f"{where} has an id that is not an extension id.")
    where = f"The index entry {value['id']!r}"
    if not isinstance(value["version"], str) or not VERSION.fullmatch(value["version"]):
        raise ExtensionIndexError(f"{where} has a version that is not three numbers separated by dots.")
    spec = value["requires_qorqbot"]
    if not isinstance(spec, str) or not SPECIFIER.fullmatch(spec.replace(" ", "")):
        raise ExtensionIndexError(f"{where} has a requires_qorqbot that is not comparisons against a version.")
    platforms = value["platforms"]
    if not (isinstance(platforms, list) and platforms and all(item in PLATFORMS for item in platforms)
            and len(set(platforms)) == len(platforms)):
        raise ExtensionIndexError(f"{where} must have platforms as a list of {', '.join(PLATFORMS)}, each at most once.")
    if value["setup_kind"] not in SETUP_KINDS:
        raise ExtensionIndexError(f"{where} has an unknown setup_kind.")
    needed = value["requires_extensions"]
    if not (isinstance(needed, list) and all(isinstance(item, str) and EXTENSION_ID.fullmatch(item) for item in needed)):
        raise ExtensionIndexError(f"{where} must have requires_extensions as a list of extension ids.")
    if value["id"] in needed:
        raise ExtensionIndexError(f"{where} names itself in requires_extensions.")
    if len(set(needed)) != len(needed):
        raise ExtensionIndexError(f"{where} repeats an id in requires_extensions.")
    if not isinstance(value["folder_sha256"], str) or not SHA256.fullmatch(value["folder_sha256"]):
        raise ExtensionIndexError(f"{where} has a folder_sha256 that is not 64 lowercase hexadecimal characters.")
    if not isinstance(value["bundles"], list) or not value["bundles"]:
        raise ExtensionIndexError(f"{where} must have bundles as a non-empty list.")
    bundles = tuple(_bundle(item, f"{where}, bundle {count}") for count, item in enumerate(value["bundles"], 1))
    if len({item.platform for item in bundles}) != len(bundles):
        raise ExtensionIndexError(f"{where} has two bundles for one platform.")
    return Entry(value["id"], value["version"], _text(value["name"], f"{where} name"),
                 _text(value["summary"], f"{where} summary"), spec, tuple(platforms), value["setup_kind"],
                 tuple(needed), value["folder_sha256"], bundles)


def parse(value: Any) -> Index:
    """The index from its decoded JSON, or ``ExtensionIndexError``."""
    value = _keys(value, INDEX_KEYS, "The extension index")
    if type(value["format"]) is not int or value["format"] != FORMAT:
        raise ExtensionIndexError(f"The extension index must have format {FORMAT}.")
    if not isinstance(value["app_version"], str) or not VERSION.fullmatch(value["app_version"]):
        raise ExtensionIndexError("The extension index has an app_version that is not three numbers separated by dots.")
    if not isinstance(value["extensions"], list):
        raise ExtensionIndexError("The extension index must have extensions as a list.")
    entries = tuple(_entry(item, number) for number, item in enumerate(value["extensions"], 1))
    ids = [item.id for item in entries]
    repeated = sorted({name for name in ids if ids.count(name) > 1})
    if repeated:
        raise ExtensionIndexError(f"The extension index lists {repeated[0]!r} twice.")
    return Index(value["app_version"], entries)


def load(path: Path) -> Index:
    """The index in the file at ``path``, or ``ExtensionIndexError`` when it cannot be read or is malformed."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ExtensionIndexError(f"The extension index cannot be read: {exc.__class__.__name__}.") from None
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except ValueError as exc:
        raise ExtensionIndexError(f"The extension index is not valid JSON: {exc}.") from None
    return parse(value)


def bundle_platform(platform: str, machine: str | None = None) -> str:
    """The bundle a computer needs, from the manifest's platform name (``current_platform``: ``linux`` or ``mac``) and its
    processor: ``linux-x86_64``, ``mac-arm64``. A bundle for ``any`` fits every computer (``Index.entry_for``)."""
    machine = (machine or _platform.machine()).lower()
    return f"{platform}-{_MACHINES.get(machine, machine)}"


def index_path(paths) -> Path:
    """The index file of this installation: the trusted configuration's ``extensions_index`` when it names one, else the
    file next to the app."""
    from qorqut.installation import installation_config
    configured = (installation_config() or {}).get(CONFIG_KEY)
    return Path(configured) if configured else paths.component("app").parent / INDEX_FILE


def app_index(paths) -> Index | None:
    """The installation's index, or None when it has none or it is malformed (said in the log)."""
    path = index_path(paths)
    if not path.exists():
        return None
    try:
        return load(path)
    except ExtensionIndexError as exc:
        LOGGER.warning("extension_index_refused path=%s reason=%s", path.name, exc)
        return None


def offered(index, found, *, app_version, platform, machine=None):
    """The index entries with no installed folder, as the registry lists them: ``not_installed``, or ``unsupported`` with
    the reason when this computer or this app version does not fit, or a required extension is neither installed nor
    installable. A build without a version number does not compare versions."""
    from qorqut.extension_registry import InstalledExtension, _words, admits, version_numbers

    if index is None:
        return ()
    present = {item.id for item in found}
    fine = {item.id for item in found if item.fine}
    needs = bundle_platform(platform, machine)
    reasons = {}
    for entry in index.entries:
        if entry.id in present:
            continue
        reason = None
        if platform not in entry.platforms:
            reason = f"It runs only on {_words(entry.platforms)}, not on {platform}."
        elif version_numbers(app_version) is not None and not admits(app_version, entry.requires_qorqbot):
            reason = f"It needs Qorqbot {entry.requires_qorqbot}, and this is Qorqbot {app_version}."
        elif index.entry_for(entry.id, needs) is None:
            reason = f"No bundle of it is made for {needs}."
        reasons[entry.id] = reason
    changed = True
    while changed:
        changed = False
        usable = fine | {ext_id for ext_id, reason in reasons.items() if reason is None}
        for entry in index.entries:
            if reasons.get(entry.id, "") is None:
                missing = [other for other in entry.requires_extensions if other not in usable]
                if missing:
                    reasons[entry.id] = f"It needs the extension {missing[0]!r}, which cannot be installed here."
                    changed = True
    return tuple(InstalledExtension(entry.id, None, "downloadable", None, entry.version,
                                    NOT_INSTALLED if reasons[entry.id] is None else UNSUPPORTED, reasons[entry.id],
                                    (), (), None, entry.name, entry.summary)
                 for entry in index.entries if entry.id in reasons)
