#!/usr/bin/env python3
"""Build a local, checksummed optional connector archive from verified wheels.

``build-folder`` builds a format 2 bundle of one downloadable extension folder instead (see
``qorqut/bootstrap/extension_bundle.py``). It needs the app on the import path, as the checker does.
"""
import argparse
import functools
import gzip
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import zipfile

spec = importlib.util.spec_from_file_location('native_extension', Path(__file__).with_name('native_extension.py'))
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


@functools.cache
def _names():
    """The release's name rules (``extension_names.py``) beside this builder: the code part is refused by rule (b)."""
    spec = importlib.util.spec_from_file_location('extension_bundle_names', Path(__file__).with_name('extension_names.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(extension_id, wheels, output):
    if extension_id not in contract.FACTORIES:
        raise ValueError('Unsupported connector')
    inspected = [contract.inspect_wheel(path) for path in wheels]
    by_name = {item['name']:item for item in inspected}
    if len(by_name) != len(inspected) or len({path.name for path in wheels}) != len(wheels):
        raise ValueError('Duplicate wheel distribution or filename')
    primary = by_name.get('qorqut-' + extension_id)
    if primary is None:
        raise ValueError('Missing primary connector wheel')
    contract.inspect_resources(next(path for path in wheels if path.name == primary['filename']), extension_id)
    value = contract.manifest({'format':1, 'id':extension_id, 'version':primary['version'], 'factory':contract.FACTORIES[extension_id],
        'wheels':[{'filename':path.name, 'sha256':contract.checksum(path)} for path in sorted(wheels)]})
    encoded = json.dumps(value, indent=2, sort_keys=True).encode() + b'\n'
    with Path(output).open('xb') as stream:
        with gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                def add(name, body, size):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = size, 0o644, 0
                    archive.addfile(info, body)
                add('EXTENSION.json', io.BytesIO(encoded), len(encoded))
                for path in sorted(wheels):
                    with path.open('rb') as body:
                        add('wheels/' + path.name, body, path.stat().st_size)
    sha = contract.checksum(output)
    contract.inspect_bundle(output, sha)
    return {'path':str(output), 'sha256':sha, 'extension':value}


def build(extension_id, wheels, output):
    """Verify original wheels and retain only runtime payloads, using the core filter."""
    spec = importlib.util.spec_from_file_location('native_build_filter', Path(__file__).with_name('native_build.py'))
    filtering = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(filtering)
    for path in wheels:
        contract.inspect_wheel(path)
    provenance = []
    with tempfile.TemporaryDirectory(prefix='.extension-build-', dir=Path(output).absolute().parent) as temporary:
        selected = []
        for path in wheels:
            with zipfile.ZipFile(path) as archive:
                excluded = any(set(Path(name).parts) & filtering.DEV or Path(name).suffix in {'.pyc', '.pyo'} for name in archive.namelist())
            if excluded:
                target = Path(temporary) / path.name
                record = filtering.strip_wheel(path, target)
                provenance.append({'filename':path.name, 'original_sha256':record['original_sha256'],
                                   'runtime_sha256':record['runtime_sha256'], 'removed_files':len(record['removed'])})
                selected.append(target)
            else:
                selected.append(path)
                provenance.append({'filename':path.name, 'original_sha256':contract.checksum(path),
                                   'runtime_sha256':contract.checksum(path), 'removed_files':0})
        result = _build(extension_id, selected, output)
    result['source_wheels'] = provenance
    return result


def folder_files(folder):
    """The files of an extension folder as the bundle holds them."""
    from qorqut.bootstrap import extension_bundle as reading

    spec = importlib.util.spec_from_file_location('runtime_source', Path(__file__).with_name('runtime_source.py'))
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    return reading.folder_files(folder, source.development_metadata)


def _add(archive, name, body, size, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = size, mode, 0
    archive.addfile(info, body)


def _constraints(folder):
    """``{name: version}`` pinned by ``code/constraints.txt``: lines ``name==version``, blank lines and comments.

    The build tools hand constraints files to pip (``prepare-local.py``) and never read one themselves, so this is the one
    reader; it takes exact pins only, which is what a bundle's wheels must match."""
    path = Path(folder) / 'code/constraints.txt'
    if not path.is_file():
        return None
    pins = {}
    for line in path.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        name, separator, version = line.partition('==')
        if not separator or not name.strip() or not version.strip():
            raise ValueError(f'code/constraints.txt has the line {line!r}, which is not name==version.')
        pins[contract.canonical_name(name.strip())] = version.strip()
    return pins


def build_folder(folder, platform, output, wheels=()):
    """One reproducible format 2 bundle of an extension folder: the same folder and wheels give the same bytes.

    A file executable in the folder is stored as 0o755, every other file as 0o644, with no time or owner.

    The folder must pass the checker, with its code part loaded (``--load``). Its ``tests`` parts are left out.
    ``wheels`` are the code's third-party dependency wheels, each pinned in ``code/constraints.txt``; they go to
    ``<id>/deps/``.
    """
    from qorqut.bootstrap import extension_bundle as reading
    from qorqut.extension_contract import check_folder, load_findings, load_manifest

    folder = Path(folder).absolute()
    found = check_folder(folder)
    # Its code part is loaded as a start loads it, in a process of its own (``extension_contract --load``).
    found = found or load_findings(folder, names=_names())
    if found:
        raise ValueError('The extension folder breaks the contract: ' + ' '.join(found))
    manifest = load_manifest(folder)
    if (folder / reading.DEPS).exists():
        raise ValueError(f'The extension folder has a {reading.DEPS}/ folder; the dependency wheels are given to the builder.')
    files = folder_files(folder)
    wheels = sorted(Path(path) for path in wheels)
    if wheels:
        pins = _constraints(folder)
        if pins is None:
            raise ValueError('Dependency wheels need code/constraints.txt with their pins.')
        inspected = [reading.inspect_wheel(path) for path in wheels]
        if len({item['name'] for item in inspected}) != len(inspected) or len({path.name for path in wheels}) != len(wheels):
            raise ValueError('Duplicate wheel distribution or filename')
        for item in inspected:
            if pins.get(item['name']) != item['version']:
                raise ValueError(f'The wheel {item["filename"]} is not pinned in code/constraints.txt.')
        for path in wheels:
            files[f'{manifest.id}/{reading.DEPS}/{path.name}'] = reading.checksum(path)
    specific = any(reading.platform_specific(path.name) for path in wheels)
    if platform == 'any' and specific:
        raise ValueError('A wheel is built for one platform, so the bundle needs that platform, not any.')
    if platform != 'any' and not specific:
        raise ValueError('No wheel is built for one platform, so the bundle is for any platform.')
    header = {'format': reading.FORMAT, 'id': manifest.id, 'version': manifest.version, 'platform': platform,
              'files': files}
    reading.bundle_header(header)
    encoded = json.dumps(header, indent=2, sort_keys=True).encode() + b'\n'
    sources = {f'{manifest.id}/{reading.DEPS}/{path.name}': path for path in wheels}
    with Path(output).open('xb') as stream:
        with gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                _add(archive, reading.HEADER, io.BytesIO(encoded), len(encoded))
                for name in sorted(files):
                    source = sources.get(name) or folder.parent / name
                    executable = name not in sources and source.stat().st_mode & reading.EXECUTABLE
                    with source.open('rb') as body:
                        _add(archive, name, body, source.stat().st_size, 0o755 if executable else 0o644)
    found = reading.check_bundle(output)
    if found:
        Path(output).unlink()
        raise ValueError('The built bundle does not pass its check: ' + ' '.join(found))
    return {'path': str(output), 'sha256': contract.checksum(output), 'bundle': header}


def main_folder(argv):
    parser = argparse.ArgumentParser(prog='extension_bundle.py build-folder', description=build_folder.__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--wheel', action='append', type=Path, default=[])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build_folder(args.folder, args.platform, args.output, args.wheel), indent=2))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['build-folder']:
        return main_folder(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', choices=sorted(contract.FACTORIES), required=True)
    parser.add_argument('--wheel', action='append', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build(args.id, args.wheel, args.output), indent=2))


if __name__ == '__main__':
    main()
