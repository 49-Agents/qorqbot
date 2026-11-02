#!/usr/bin/env python3
"""Build the optional Linux wheel from a pinned Go image, outside the source tree."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

IMAGE = 'golang@sha256:5cf287a799e6b94384bad13d16b14904c531f51ba65792237e122ce42b392f61'

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', type=Path, required=True, help='Private build scratch directory')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    work = args.work.absolute()
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    native = work / 'native'
    native.mkdir(mode=0o700, exist_ok=True)
    import os
    subprocess.run(['docker','run','--rm','--network','bridge','--user',f'{os.getuid()}:{os.getgid()}',
        '-e','HOME=/build','-e','GOCACHE=/build/cache','-e','GOMODCACHE=/build/modules','-e','CGO_ENABLED=1',
        '-v',f'{source / "native"}:/source:ro','-v',f'{native}:/build','-w','/source',IMAGE,
        'sh','-c','go test -race ./... && go mod verify && go build -trimpath -buildvcs=false -o /build/whatsapp-personal . && go list -m -json all > /build/modules.json && cp /usr/local/go/LICENSE /build/GO-LICENSE'],check=True)
    stage = work / 'stage'
    if stage.exists():
        raise SystemExit('Build stage already exists; use a new work directory')
    shutil.copytree(source,stage,ignore=shutil.ignore_patterns('native','tests','tools','__pycache__','build','*.egg-info'))
    package = stage / 'qorqut_whatsapp'
    (package/'bin').mkdir(exist_ok=True)
    helper = native/'whatsapp-personal'
    shutil.copy2(helper,package/'bin/whatsapp-personal')
    notices = package/'third_party'
    notices.mkdir()
    for path in (native/'modules').rglob('*'):
        if path.is_file() and path.name.split('.')[0] in {'LICENSE','COPYING','NOTICE'}:
            target=notices/path.relative_to(native/'modules')
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,target)
    shutil.copyfile(native/'GO-LICENSE',notices/'GO-LICENSE')
    shutil.copyfile(native/'modules.json',notices/'go-modules.json')
    (package/'build.json').write_text(json.dumps({'platform':'linux_x86_64','go_image':IMAGE,'helper_sha256':hashlib.file_digest(helper.open('rb'),'sha256').hexdigest()},indent=2)+'\n')
    (stage/'setup.cfg').write_text('[bdist_wheel]\nplat_name = linux_x86_64\n')
    subprocess.run([sys.executable,'-m','pip','wheel','--no-deps','--wheel-dir',str(work/'dist'),str(stage)],check=True)
    from source_archive import build_source_archive
    archive = build_source_archive(source, native, work, IMAGE)
    print(archive, hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest())
    for wheel in (work/'dist').glob('*.whl'):
        print(wheel,hashlib.file_digest(wheel.open('rb'),'sha256').hexdigest())

if __name__=='__main__':
    main()
