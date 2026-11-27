"""Reading extension bundles: the checks shared by both bundle formats, and format 2 for downloadable extensions.

Format 1 (``EXTENSION.json`` and ``wheels/``) is the format of ``./qorqbot extension-install``. Its installer,
``tools/distribution/native_extension.py``, runs outside the app's import path and loads this file by its location, so the
module level here uses the standard library only; what format 2 needs of the app is imported inside its functions.

Format 2 is one downloadable extension folder (``docs/EXTENSION-CONTRACT.md`` section 5) as one ``.tar.gz``:

```
BUNDLE.json        {"format": 2, "id", "version", "platform", "files": {path: sha256}}
<id>/...           the extension folder, without any ``tests`` part
<id>/deps/*.whl    wheels of the code's third-party dependencies, when it has any
```

``unpack`` takes only regular files, checks every one against ``BUNDLE.json`` and refuses links, ``..``, duplicates and
any ``tests`` part. ``check_bundle`` returns every way a bundle breaks the format and the contract as one sentence each.
"""
from __future__ import annotations

import base64
import csv
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import tarfile
import tempfile
import zipfile

LIMIT = 1024 ** 3
SHA256 = re.compile(r"^[a-f0-9]{64}$")
# A bundle's platform: ``any``, or the manifest's platform word and the processor (``extension_index.bundle_platform``).
BUNDLE_PLATFORM = re.compile(r"^(?:any|(?:linux|mac)-[a-z0-9_]+)$")
HEADER = "BUNDLE.json"
FORMAT = 2
# The folder the contract keeps out of every bundle (section 5, rule 13 of section 12).
TESTS = "tests"
DEPS = "deps"
BUNDLE_KEYS = frozenset({"format", "id", "version", "platform", "files"})
# A file is stored in a bundle as executable (0o755) when any execute bit is set in the folder, else as 0o644.
EXECUTABLE = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def canonical_name(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def checksum(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def plain(path):
    path = Path(path).absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Installation paths may not cross symlinks')
    return path


def relative(name):
    """``name`` as a relative path, or ``ValueError`` when it could leave the folder it is written into on any computer:
    judged by the POSIX and the Windows rules (no root, no drive, no ``..``), with no colon, which Windows reads as a
    drive or a data stream, and no part ending in a dot or a space, which Windows drops."""
    path, windows = PurePosixPath(name), PureWindowsPath(name)
    if (not name or '\\' in name or ':' in name or path.is_absolute() or windows.is_absolute() or windows.anchor
            or any(p in ('', '.', '..') or p.endswith(('.', ' ')) for p in name.split('/'))
            or any(ord(c) < 32 for c in name) or len(name) > 4096):
        raise ValueError('Unsafe archive path')
    return path


def index_members(members, directories=()):
    """Archive members by name: safe relative names, no duplicates, regular files and only the named directories."""
    indexed = {}
    for member in members:
        name = member.name.rstrip('/') if member.isdir() else member.name
        relative(name)
        if name in indexed or not (member.isfile() or member.isdir() and name in directories):
            raise ValueError('Duplicate, linked or special archive member')
        indexed[name] = member
    return indexed


def extract(tar, indexed, destination):
    """Write the regular files of ``indexed`` under ``destination``; each target must be new."""
    destination = plain(destination)
    inside = destination.resolve()
    for name, member in indexed.items():
        if member.isdir():
            continue
        # Judged again where it is written, whatever judged the names before.
        target = destination / relative(name)
        if not target.resolve().is_relative_to(inside):
            raise ValueError('Unsafe archive path')
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open('xb') as output, tar.extractfile(member) as source:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())


def inspect_wheel(path):
    """Validate file identities and every wheel RECORD digest before pip sees it."""
    path = Path(path)
    with zipfile.ZipFile(path) as wheel:
        members = wheel.infolist()
        if len(members) > 50000 or sum(m.file_size for m in members) > LIMIT:
            raise ValueError('Wheel exceeds verification limits')
        names = set()
        for member in members:
            name = member.filename.rstrip('/') if member.is_dir() else member.filename
            relative(name)
            mode = stat.S_IFMT(member.external_attr >> 16)
            if name in names or mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError('Duplicate or linked wheel member')
            names.add(name)
            if any(part.endswith('.data') for part in PurePosixPath(name).parts):
                raise ValueError('Relocated wheel files are not supported in connector bundles')
        metadata_files = [name for name in names if re.fullmatch(r'[^/]+\.dist-info/METADATA', name)]
        if len(metadata_files) != 1:
            raise ValueError('Wheel must have exactly one metadata identity')
        prefix = metadata_files[0].rsplit('/', 1)[0]
        record_name = prefix + '/RECORD'
        if record_name not in names or prefix + '/WHEEL' not in names:
            raise ValueError('Wheel metadata or RECORD is missing')
        if wheel.getinfo(metadata_files[0]).file_size > 4 * 1024 * 1024 or wheel.getinfo(record_name).file_size > 16 * 1024 * 1024:
            raise ValueError('Oversized wheel metadata')
        metadata_raw = wheel.read(metadata_files[0])
        scripts = {}
        if prefix + '/entry_points.txt' in names:
            import configparser
            entries = configparser.ConfigParser()
            entries.optionxform = str
            entries.read_string(wheel.read(prefix + '/entry_points.txt').decode())
            for section in ('console_scripts', 'gui_scripts'):
                for name, value in entries.items(section) if entries.has_section(section) else ():
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', name) or name in scripts:
                        raise ValueError('Unsafe or duplicate wheel launcher name')
                    scripts[name] = value
        metadata = BytesParser().parsebytes(metadata_raw)
        name, version = metadata.get('Name'), metadata.get('Version')
        if not name or not version or len(metadata.get_all('Name', [])) != 1 or len(metadata.get_all('Version', [])) != 1:
            raise ValueError('Invalid wheel name/version')
        filename_parts = path.name[:-4].split('-')
        if len(filename_parts) not in (5, 6) or canonical_name(filename_parts[0]) != canonical_name(name) or filename_parts[1] != version:
            raise ValueError('Wheel filename and metadata identity differ')
        if prefix != filename_parts[0] + '-' + version + '.dist-info':
            raise ValueError('Wheel metadata directory identity differs')
        entries = {}
        for row in csv.reader(io.StringIO(wheel.read(record_name).decode())):
            if len(row) != 3 or row[0] in entries:
                raise ValueError('Invalid or duplicate wheel RECORD row')
            relative(row[0]); entries[row[0]] = row[1:]
        file_names = {m.filename for m in members if not m.is_dir()}
        if set(entries) != file_names:
            raise ValueError('Wheel RECORD does not cover its exact files')
        for name_in_wheel, (hashed, size) in entries.items():
            if name_in_wheel == record_name:
                if hashed or size:
                    raise ValueError('Wheel RECORD cannot hash itself')
                continue
            raw = wheel.read(name_in_wheel)
            digest = 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip('=')
            if hashed != digest or size != str(len(raw)):
                raise ValueError('Wheel RECORD checksum mismatch')
        return {'filename':path.name, 'name':canonical_name(name), 'version':version,
                'files':sorted(file_names), 'scripts':scripts, 'requires':metadata.get_all('Requires-Dist', []), 'requires_python':metadata.get('Requires-Python')}


# Format 2.

def platform_specific(wheel_filename):
    """Whether a wheel is built for one platform: its platform tag, the last part of the file name, is not ``any``."""
    return wheel_filename[:-4].split('-')[-1] != 'any'


def folder_files(folder, leave_out=None):
    """``{path: sha256}`` of every file of an extension folder, paths relative to its parent; ``tests`` parts left out.

    ``leave_out(parts)`` may name more paths to leave out; the bundle builder passes the download builder's rule for
    development metadata. A link or anything but a folder or a regular file is refused, so the bundle holds exactly what
    the folder holds.
    """
    folder = Path(folder)
    files = {}
    for path in sorted(folder.rglob('*')):
        inside = path.relative_to(folder)
        if TESTS in inside.parts or leave_out is not None and leave_out(inside.parts):
            continue
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f'The extension folder holds {inside.as_posix()}, which is not a plain file or folder.')
        if path.is_file():
            files[f'{folder.name}/{inside.as_posix()}'] = checksum(path)
    return files


def folder_digest(files, extension_id):
    """The digest of the released folder: its files and their checksums, without the dependency wheels."""
    deps = f'{extension_id}/{DEPS}/'
    lines = ''.join(f'{sha}  {name}\n' for name, sha in sorted(files.items()) if not name.startswith(deps))
    return hashlib.sha256(lines.encode()).hexdigest()


def bundle_header(value):
    """``BUNDLE.json`` of a format 2 bundle, or ``ValueError`` with the first problem."""
    from qorqut.extension_contract import VERSION
    from qorqut.extensions import EXTENSION_ID

    if not isinstance(value, dict) or set(value) != BUNDLE_KEYS:
        raise ValueError(f'{HEADER} must have exactly the fields {", ".join(sorted(BUNDLE_KEYS))}.')
    if type(value['format']) is not int or value['format'] != FORMAT:
        raise ValueError(f'{HEADER} must have format {FORMAT}.')
    if not isinstance(value['id'], str) or not EXTENSION_ID.fullmatch(value['id']):
        raise ValueError(f'{HEADER} has an id that is not an extension id.')
    if not isinstance(value['version'], str) or not VERSION.fullmatch(value['version']):
        raise ValueError(f'{HEADER} has a version that is not three numbers separated by dots.')
    if not isinstance(value['platform'], str) or not BUNDLE_PLATFORM.fullmatch(value['platform']):
        raise ValueError(f'{HEADER} must name the platform any, or a platform and processor such as linux-x86_64.')
    files = value['files']
    if not isinstance(files, dict) or not files:
        raise ValueError(f'{HEADER} must list the files of the bundle.')
    for name, sha in files.items():
        try:
            parts = relative(name).parts
        except ValueError:
            raise ValueError(f'{HEADER} lists the unsafe path {name!r}.') from None
        if parts[0] != value['id'] or len(parts) < 2:
            raise ValueError(f'{HEADER} lists {name!r}, which is not inside the folder {value["id"]}/.')
        if TESTS in parts:
            raise ValueError(f'{HEADER} lists {name!r}, which is inside a tests folder.')
        if not isinstance(sha, str) or not SHA256.fullmatch(sha):
            raise ValueError(f'{HEADER} has no sha256 for {name!r}.')
    return value


def _require_distinct(indexed):
    """Refuse names that one file system would hold as one path: names equal but for case (a Mac's disk merges them), and
    a file whose name is also a folder of another member."""
    folded = {}
    for name in sorted(indexed):
        key = name.casefold()
        if key in folded:
            raise ValueError(f'The bundle holds both {folded[key]} and {name}, which differ only by case.')
        folded[key] = name
    for name in sorted(indexed):
        parents = PurePosixPath(name).parents
        for parent in list(parents)[:-1]:
            if parent.as_posix().casefold() in folded:
                raise ValueError(f'The bundle holds {folded[parent.as_posix().casefold()]} as a file and also {name} '
                                 'inside it.')


def _new_folder(destination):
    """The destination of an unpack: a folder that does not exist yet, or an empty one, on a path without links."""
    destination = plain(destination)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('A bundle is unpacked only into a new or empty folder.')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    return destination


def unpack(archive, destination):
    """Check a format 2 bundle against its ``BUNDLE.json`` and write it under ``destination``, which must be new or
    empty; returns ``BUNDLE.json``. A member stored as executable is executable after the unpack.

    ``ValueError`` (or ``tarfile.TarError``) with a plain sentence when the bundle is unsafe or differs from its header;
    nothing is written then.
    """
    with tarfile.open(archive, mode='r:gz') as tar:
        members = tar.getmembers()
        if sum(m.size for m in members) > LIMIT:
            raise ValueError('Extension bundle exceeds extraction limits')
        try:
            indexed = index_members(members)
        except ValueError:
            raise ValueError('The bundle holds a link, a special file, a duplicate or an unsafe path.') from None
        tested = sorted(name for name in indexed if TESTS in PurePosixPath(name).parts)
        if tested:
            raise ValueError(f'The bundle holds {tested[0]}, which is inside a tests folder.')
        _require_distinct(indexed)
        if HEADER not in indexed:
            raise ValueError(f'The bundle has no {HEADER}.')
        try:
            value = json.loads(tar.extractfile(indexed[HEADER]).read(), object_pairs_hook=unique_object)
        except ValueError:
            raise ValueError(f'{HEADER} is not valid JSON.') from None
        value = bundle_header(value)
        listed, held = set(value['files']), set(indexed) - {HEADER}
        if held - listed:
            raise ValueError(f'The bundle holds {sorted(held - listed)[0]}, which {HEADER} does not list.')
        if listed - held:
            raise ValueError(f'The bundle lacks {sorted(listed - held)[0]}, which {HEADER} lists.')
        for name in sorted(listed):
            with tar.extractfile(indexed[name]) as body:
                if hashlib.file_digest(body, 'sha256').hexdigest() != value['files'][name]:
                    raise ValueError(f'The checksum of {name} differs from {HEADER}.')
        destination = _new_folder(destination)
        extract(tar, indexed, destination)
        for name, member in indexed.items():
            if member.mode & EXECUTABLE:
                target = destination / name
                target.chmod(target.stat().st_mode | stat.S_IXUSR)
        return value


def _deps_findings(folder, value):
    deps = folder / DEPS
    found = []
    wheels = []
    for path in sorted(deps.rglob('*')) if deps.exists() else ():
        if path.is_dir():
            found.append(f'{DEPS}/ may hold only wheel files, not the folder {path.relative_to(folder).as_posix()}.')
        elif path.parent != deps or path.suffix != '.whl':
            found.append(f'{DEPS}/ may hold only wheel files, not {path.relative_to(folder).as_posix()}.')
        else:
            try:
                wheels.append(inspect_wheel(path))
            except (ValueError, zipfile.BadZipFile, UnicodeDecodeError) as error:
                found.append(f'The wheel {path.name} is not a valid wheel: {error}.')
    if len({wheel['name'] for wheel in wheels}) != len(wheels):
        found.append(f'{DEPS}/ holds two wheels of one distribution.')
    specific = any(platform_specific(wheel['filename']) for wheel in wheels)
    if value['platform'] == 'any' and specific:
        found.append('The bundle is for any platform but holds a wheel built for one platform.')
    if value['platform'] != 'any' and not specific:
        found.append(f'The bundle is for {value["platform"]} but every wheel in it works on any platform.')
    return found


def check_bundle(archive):
    """Every way a format 2 bundle breaks the format or the contract, one sentence each. Empty means it passes.

    The members must equal ``BUNDLE.json``, no member may be inside a ``tests`` folder, the folder inside must pass the
    checker, and its manifest must carry the id and version of ``BUNDLE.json``.
    """
    from qorqut.extension_contract import ContractError, check_folder, load_manifest

    with tempfile.TemporaryDirectory(prefix='.extension-bundle-') as temporary:
        try:
            value = unpack(archive, temporary)
        except (OSError, ValueError, tarfile.TarError) as error:
            return [str(error)]
        folder = Path(temporary) / value['id']
        found = list(check_folder(folder))
        if not found:
            try:
                manifest = load_manifest(folder)
            except ContractError as error:
                found.append(str(error))
            else:
                if manifest.version != value['version']:
                    found.append(f'{HEADER} has the version {value["version"]} but the manifest has {manifest.version}.')
        found.extend(_deps_findings(folder, value))
        return found
