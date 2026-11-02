#!/usr/bin/env python3
"""Build an optional connector wheel on its declared target, outside the source tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

IMAGE = 'golang@sha256:5cf287a799e6b94384bad13d16b14904c531f51ba65792237e122ce42b392f61'
GO_VERSION = 'go1.27.2'
TARGETS = {'linux-x86_64': 'linux_x86_64', 'macos-arm64': 'macosx_13_0_arm64'}


def native_mac_environment(native):
    if sys.platform != 'darwin' or platform.machine().lower() not in {'arm64', 'aarch64'}:
        raise SystemExit('macos-arm64 requires a native Apple Silicon Mac; Linux cross-CGO builds are not supported')
    if int(platform.mac_ver()[0].split('.')[0]) < 13:
        raise SystemExit('The selected Go toolchain requires macOS 13 or newer')
    environment = dict(os.environ, GOTOOLCHAIN='local', GOCACHE=str(native/'cache'),
                       GOMODCACHE=str(native/'modules'), CGO_ENABLED='1', GOOS='darwin', GOARCH='arm64',
                       MACOSX_DEPLOYMENT_TARGET='13.0')
    if subprocess.check_output(['go', 'env', 'GOVERSION'], env=environment, text=True).strip() != GO_VERSION:
        raise SystemExit('Install Go 1.27.2 for this reproducible helper build')
    environment['CC'] = subprocess.check_output(['xcrun', '--find', 'clang'], text=True).strip()
    for name in ('CGO_CFLAGS', 'CGO_LDFLAGS'):
        environment[name] = environment.get(name, '') + ' -mmacosx-version-min=13.0'
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True, help='Fresh private build directory')
    parser.add_argument('--target', choices=TARGETS, default='linux-x86_64')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    work = args.work.absolute()
    native = work / 'native'
    environment = native_mac_environment(native) if args.target == 'macos-arm64' else None
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    native.mkdir(mode=0o700, exist_ok=True)
    if environment is None:
        subprocess.run(['docker', 'run', '--rm', '--platform', 'linux/amd64', '--network', 'bridge', '--user', f'{os.getuid()}:{os.getgid()}',
            '-e', 'GOCACHE=/build/cache', '-e', 'GOMODCACHE=/build/modules', '-e', 'CGO_ENABLED=1',
            '-v', f'{source / "native"}:/source:ro', '-v', f'{native}:/build', '-w', '/source', IMAGE,
            'sh', '-c', 'go test -race ./... && go mod verify && go build -trimpath -buildvcs=false -o /build/whatsapp-personal . && go list -m -json all > /build/modules.json && cp /usr/local/go/LICENSE /build/GO-LICENSE'], check=True)
        module_cache_prefix = '/build/modules'
    else:
        for command in (['go', 'test', '-race', './...'], ['go', 'mod', 'verify'],
                        ['go', 'build', '-trimpath', '-buildvcs=false', '-o', str(native/'whatsapp-personal'), '.']):
            subprocess.run(command, cwd=source/'native', env=environment, check=True)
        with (native/'modules.json').open('w') as output:
            subprocess.run(['go', 'list', '-m', '-json', 'all'], cwd=source/'native', env=environment, stdout=output, check=True)
        goroot = subprocess.check_output(['go', 'env', 'GOROOT'], env=environment, text=True).strip()
        shutil.copyfile(Path(goroot)/'LICENSE', native/'GO-LICENSE')
        module_cache_prefix = str(native/'modules')
    stage = work / 'stage'
    if stage.exists():
        raise SystemExit('Build stage already exists; use a new work directory')
    shutil.copytree(source, stage, ignore=shutil.ignore_patterns('native', 'tests', 'tools', '__pycache__', 'build', '*.egg-info'))
    package = stage / 'qorqut_whatsapp'
    (package/'bin').mkdir(exist_ok=True)
    helper = native/'whatsapp-personal'
    shutil.copy2(helper, package/'bin/whatsapp-personal')
    notices = package/'third_party'
    notices.mkdir()
    for path in (native/'modules').rglob('*'):
        if path.is_file() and path.name.split('.')[0] in {'LICENSE', 'COPYING', 'NOTICE'}:
            target = notices/path.relative_to(native/'modules')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    shutil.copyfile(native/'GO-LICENSE', notices/'GO-LICENSE')
    from source_archive import build_source_archive, module_records
    # Go's raw module stream contains local Dir/GoMod paths. Ship identities and checksums only.
    public_modules = [{key: value for key, value in record.items()
                       if key in {'Path', 'Version', 'Main', 'Sum', 'GoModSum', 'GoVersion'}}
                      for record in module_records(native/'modules.json')]
    (notices/'go-modules.json').write_text(json.dumps(public_modules, indent=2)+'\n')
    metadata = {'platform': TARGETS[args.target], 'go_version': GO_VERSION,
                'go_image': IMAGE if environment is None else None,
                'helper_sha256': hashlib.file_digest(helper.open('rb'), 'sha256').hexdigest()}
    (package/'build.json').write_text(json.dumps(metadata, indent=2)+'\n')
    (stage/'setup.cfg').write_text('[bdist_wheel]\nplat_name = '+TARGETS[args.target]+'\n')
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-cache-dir', '--wheel-dir', str(work/'dist'), str(stage)], check=True)
    archive = build_source_archive(source, native, work, IMAGE if environment is None else None,
                                   target=args.target, module_cache_prefix=module_cache_prefix)
    print(archive, hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest())
    for wheel in (work/'dist').glob('*.whl'):
        print(wheel, hashlib.file_digest(wheel.open('rb'), 'sha256').hexdigest())


if __name__ == '__main__':
    main()
