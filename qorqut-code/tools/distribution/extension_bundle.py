#!/usr/bin/env python3
"""Build a local, checksummed optional connector archive from verified wheels."""
import argparse
import gzip
import importlib.util
import io
import json
from pathlib import Path
import tarfile

spec = importlib.util.spec_from_file_location('native_extension', Path(__file__).with_name('native_extension.py'))
contract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contract)


def build(extension_id, wheels, output):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', choices=sorted(contract.FACTORIES), required=True)
    parser.add_argument('--wheel', action='append', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build(args.id, args.wheel, args.output), indent=2))


if __name__ == '__main__':
    main()
