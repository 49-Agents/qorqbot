#!/usr/bin/env python3
"""Package complete corresponding source alongside a built native WhatsApp helper."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tomllib

BUILD_SCRIPT = r"""#!/bin/sh
set -eu
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
build_dir=${1:-"$source_dir/build"}
mkdir -p "$build_dir"
build_dir=$(CDPATH= cd -- "$build_dir" && pwd)
mkdir -p "$build_dir/modules/cache/download" "$build_dir/cache"
cp -R "$source_dir/THIRD-PARTY-SOURCE/." "$build_dir/modules/"
cp -R "$source_dir/GO-MODULE-METADATA/." "$build_dir/modules/cache/download/"
export GOCACHE="$build_dir/cache" GOMODCACHE="$build_dir/modules"
export CGO_ENABLED=1 GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOWORK=off
cd "$source_dir"
go mod verify
go build -trimpath -buildvcs=false -o "$build_dir/whatsapp-personal" .
go version -m "$build_dir/whatsapp-personal"
sha256sum "$build_dir/whatsapp-personal"
"""

README = r"""# WhatsApp native helper corresponding source

The first-party native helper in this directory is copyright 49 Agents LLC and
licensed under GPL-3.0-only (see LICENSE). All third-party modules retain their
own licenses and notices. The separate Qorqbot Python adapter/UI uses Apache 2.0;
it is not required to build or run this executable.

This archive includes the exact helper sources, go.mod/go.sum, all 29 linked Go
module source trees, their module metadata/checksums, and the build script.
First-party developer tests are excluded; upstream module source is preserved.
SOURCE-MANIFEST.json records the matching
binary hash, build settings and source-file hashes. The standard Go/C toolchains
and system libraries are external build prerequisites.

## Rebuild without network access

Use Linux amd64, Go 1.27.2 and a C compiler/glibc toolchain. Choose a new
build directory for each rebuild (Go module source cache files are read-only). Run:

    ./build.sh ../whatsapp-build

The script copies the bundled module source into a local module cache, verifies
modules and builds with CGO_ENABLED=1,
-trimpath and -buildvcs=false. No account credentials or service connection is
needed. It disables Go network/module downloads and automatic toolchain updates.

The original build used this official Go image:

    golang@sha256:5cf287a799e6b94384bad13d16b14904c531f51ba65792237e122ce42b392f61

With that image already installed, an isolated rebuild can use:

    mkdir -p ../whatsapp-build
    docker run --rm --network none --user "$(id -u):$(id -g)" \
      -v "$PWD:/source:ro" -v "$(cd ../whatsapp-build && pwd):/build" \
      -w /source \
      golang@sha256:5cf287a799e6b94384bad13d16b14904c531f51ba65792237e122ce42b392f61 \
      ./build.sh /build

Distribute this matching source archive alongside the optional native executable
with equivalent access. Preserve the GPL and all third-party notices. Qorqbot's
separate application license terms do not apply to this helper/source.
"""


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def module_records(path):
    decoder = json.JSONDecoder()
    text = path.read_text()
    while text.strip():
        text = text.lstrip()
        value, offset = decoder.raw_decode(text)
        yield value
        text = text[offset:]


def build_source_archive(source, native, output, image, *, target="linux-x86_64", module_cache_prefix="/build/modules"):
    version = tomllib.loads((source / 'pyproject.toml').read_text())['project']['version']
    if target not in {'linux-x86_64', 'macos-arm64'}:
        raise ValueError('Unsupported helper source target')
    name = f'qorqut-whatsapp-{version}-{target}-source'
    documented = name + '.tar.gz'
    if documented not in (source / 'SOURCE-AVAILABILITY.md').read_text():
        raise ValueError('Update SOURCE-AVAILABILITY.md for the connector version before building')
    tree = output / name
    archive = output / (name + '.tar.gz')
    if tree.exists() or archive.exists():
        raise ValueError('Source output already exists; use a fresh output directory')
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source / 'native', tree, ignore=shutil.ignore_patterns('.git', '__pycache__'))
    modules = []
    for record in module_records(native / 'modules.json'):
        # Unfetched graph-only/test-only requirements have metadata but no used source.
        if record.get('Main') or not record.get('Dir'):
            continue
        if record.get('Replace'):
            raise ValueError('Module replacements need explicit corresponding-source handling')
        relative = Path(record['Dir']).relative_to(module_cache_prefix)
        destination = Path('THIRD-PARTY-SOURCE') / relative
        shutil.copytree(native / 'modules' / relative, tree / destination)
        modules.append({'module': record['Path'], 'version': record['Version'],
                        'go_checksum': record.get('Sum'), 'source_directory': str(destination)})
    cache = native / 'modules/cache/download'
    for path in cache.rglob('*'):
        if path.is_file() and path.suffix in {'.mod', '.info', '.ziphash'}:
            metadata_target = tree / 'GO-MODULE-METADATA' / path.relative_to(cache)
            metadata_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, metadata_target)
    build_script = BUILD_SCRIPT
    if target == 'macos-arm64':
        build_script = build_script.replace('set -eu', 'set -eu\n[ "$(uname -s)" = Darwin ] && [ "$(uname -m)" = arm64 ] || { echo "Build on an Apple Silicon Mac" >&2; exit 1; }', 1)
        build_script = build_script.replace('cd "$source_dir"', 'export GOOS=darwin GOARCH=arm64 MACOSX_DEPLOYMENT_TARGET=13.0\nexport CGO_CFLAGS="${CGO_CFLAGS:-} -mmacosx-version-min=13.0"\nexport CGO_LDFLAGS="${CGO_LDFLAGS:-} -mmacosx-version-min=13.0"\ncd "$source_dir"', 1)
        build_script = build_script.replace('sha256sum "$build_dir/whatsapp-personal"', 'python3 -c \'import hashlib,sys; print(hashlib.file_digest(open(sys.argv[1],"rb"),"sha256").hexdigest())\' "$build_dir/whatsapp-personal"')
    (tree / 'build.sh').write_text(build_script)
    (tree / 'build.sh').chmod(0o755)
    readme = README.replace('all 29 linked Go', f'all {len(modules)} linked Go')
    old_image = 'golang@sha256:5cf287a799e6b94384bad13d16b14904c531f51ba65792237e122ce42b392f61'
    if target == 'macos-arm64':
        readme = ("# WhatsApp native helper corresponding source — Apple Silicon\n\n"
                  "The native helper is GPL-3.0-only; third-party modules retain their original licenses. "
                  "The separate Python adapter/UI is Apache 2.0.\n\n"
                  "Use an Apple Silicon Mac with macOS 13+, Go 1.27.2, Python 3 and the Xcode command-line tools. "
                  "Run ./build.sh ../whatsapp-build with a new build directory. The bundled module source and "
                  "metadata allow a network-free build; no account or service is needed. Docker/Linux cannot "
                  "cross-build this CGO helper for macOS.\n\n"
                  "SOURCE-MANIFEST.json records the exact helper and source hashes. "
                  "Supply this source archive alongside the matching optional binary with equivalent access.\n")
    else:
        readme = readme.replace(old_image, image).replace('docker run --rm --network none', 'docker run --rm --platform linux/amd64 --network none')
    (tree / 'README-SOURCE.md').write_text(readme)
    manifest = {'format': 1, 'helper_sha256': checksum(native / 'whatsapp-personal'),
                'first_party_source_license': 'GPL-3.0-only', 'modules': modules,
                'build': {'platform': 'darwin/arm64' if target == 'macos-arm64' else 'linux/amd64', 'go': '1.27.2', 'CGO_ENABLED': '1',
                          'flags': ['-trimpath', '-buildvcs=false'], 'go_image': image},
                'files': []}
    for path in sorted(tree.rglob('*')):
        if path.is_symlink():
            raise ValueError('Source archive may not contain symlinks')
        if path.is_file():
            manifest['files'].append({'path': str(path.relative_to(tree)), 'sha256': checksum(path)})
    (tree / 'SOURCE-MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
    with archive.open('xb') as stream:
        with gzip.GzipFile(fileobj=stream, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as tar:
                for path in [tree, *sorted(tree.rglob('*'))]:
                    info = tar.gettarinfo(str(path), arcname=str(path.relative_to(output)))
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ''
                    if path.is_file():
                        with path.open('rb') as body:
                            tar.addfile(info, body)
                    else:
                        tar.addfile(info)
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-build', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--target', choices=['linux-x86_64', 'macos-arm64'], default='linux-x86_64')
    args = parser.parse_args()
    from build import IMAGE
    archive = build_source_archive(Path(__file__).resolve().parents[1],
                                   args.native_build.resolve(), args.output.resolve(), IMAGE if args.target == 'linux-x86_64' else None,
                                   target=args.target, module_cache_prefix='/build/modules' if args.target == 'linux-x86_64' else str(args.native_build.resolve()/'modules'))
    print(json.dumps({'source_archive': str(archive), 'sha256': checksum(archive)}))


if __name__ == '__main__':
    main()
