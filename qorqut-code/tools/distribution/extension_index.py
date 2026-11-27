#!/usr/bin/env python3
"""Write the index of downloadable extensions for one release from the extension folders and their built bundles.

Every given folder whose manifest says ``delivery: downloadable`` gets one entry; other folders are left out. Each such
folder needs at least one format 2 bundle built from it (``extension_bundle.py build-folder``), at most one per platform.
A bundle's address is ``--base-url`` followed by its file name. The same inputs give the same bytes. Needs the app on the
import path, as the checker does. The file format is described in ``qorqut/extension_index.py``, which reads it.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tarfile


def _bundle_builder():
    spec = importlib.util.spec_from_file_location('extension_bundle', Path(__file__).with_name('extension_bundle.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_index(folders, bundles, app_version, base_url):
    """The index as a JSON value; ``ValueError`` when a folder or bundle does not fit."""
    from qorqut import extension_index
    from qorqut.bootstrap import extension_bundle as reading
    from qorqut.extension_contract import ContractError, load_manifest

    downloadable = {}
    for folder in folders:
        folder = Path(folder).absolute()
        try:
            manifest = load_manifest(folder)
        except ContractError as error:
            raise ValueError(f'The folder {folder.name} breaks the contract: {error}') from None
        if manifest.delivery != 'downloadable':
            continue
        if manifest.id in downloadable:
            raise ValueError(f'Two folders have the id {manifest.id}.')
        downloadable[manifest.id] = (manifest, reading.folder_digest(_bundle_builder().folder_files(folder), manifest.id))
    built = {}
    for path in bundles:
        path = Path(path)
        found = reading.check_bundle(path)
        if found:
            raise ValueError(f'The bundle {path.name} does not pass its check: ' + ' '.join(found))
        with tarfile.open(path, mode='r:gz') as tar:
            header = json.loads(tar.extractfile(reading.HEADER).read())
        if header['id'] not in downloadable:
            raise ValueError(f'The bundle {path.name} is of {header["id"]}, which is not a given downloadable folder.')
        manifest, digest = downloadable[header['id']]
        if header['version'] != manifest.version:
            raise ValueError(f'The bundle {path.name} has the version {header["version"]}, but the folder has {manifest.version}.')
        if reading.folder_digest(header['files'], header['id']) != digest:
            raise ValueError(f'The bundle {path.name} was not built from the folder {header["id"]} as it is now.')
        entry = built.setdefault(header['id'], {})
        if header['platform'] in entry:
            raise ValueError(f'Two bundles of {header["id"]} are for the platform {header["platform"]}.')
        entry[header['platform']] = {'platform': header['platform'], 'url': base_url.rstrip('/') + '/' + path.name,
                                     'sha256': reading.checksum(path), 'size': path.stat().st_size}
    extensions = []
    for extension_id in sorted(downloadable):
        manifest, digest = downloadable[extension_id]
        if extension_id not in built:
            raise ValueError(f'The downloadable extension {extension_id} has no bundle.')
        extensions.append({
            'id': manifest.id, 'version': manifest.version, 'name': manifest.name, 'summary': manifest.summary,
            'requires_qorqbot': manifest.requires_qorqbot, 'platforms': list(manifest.platforms),
            'setup_kind': manifest.setup_kind, 'requires_extensions': list(manifest.requires_extensions),
            'folder_sha256': digest, 'bundles': [built[extension_id][name] for name in sorted(built[extension_id])]})
    value = {'format': extension_index.FORMAT, 'app_version': app_version, 'extensions': extensions}
    extension_index.parse(value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', nargs='+', type=Path)
    parser.add_argument('--bundle', action='append', type=Path, default=[])
    parser.add_argument('--app-version', required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    value = build_index(args.folder, args.bundle, args.app_version, args.base_url)
    with args.output.open('x') as stream:
        stream.write(json.dumps(value, indent=2, sort_keys=True) + '\n')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ValueError as error:
        print('Extension index: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
