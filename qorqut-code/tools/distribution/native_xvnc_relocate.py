#!/usr/bin/env python3
"""Keep Xorg's temporary compiled keymaps in the packaged helper's private cwd."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def relocate():
    work = Path('/build')
    source = work / 'tigervnc'
    path = source / 'unix/xserver/xkb/ddxLoad.c'
    before = path.read_bytes()
    marker = b'#include <xkb-config.h>\n'
    addition = (b'\n/* The packaged desktop supervisor sets a private writable working directory. */\n'
                b'#undef XKM_OUTPUT_DIR\n#define XKM_OUTPUT_DIR "."\n')
    if before.count(marker) != 1:
        raise RuntimeError('Unexpected pinned Xorg keymap source')
    path.write_bytes(before.replace(marker, marker + addition, 1))
    build = source / 'obj-x86_64-linux-gnu/unix/xserver'
    subprocess.run(['make', '-C', build, '-j8'], check=True)
    shutil.copy2(build / 'hw/vnc/Xvnc', work / 'Xtigervnc')
    subprocess.run(['strip', '--strip-unneeded', work / 'Xtigervnc'], check=True)
    record = json.loads((work / 'TIGERVNC-BUILD.json').read_text())
    record.update(keymap_output='private process working directory',
                  keymap_source_before_sha256=hashlib.sha256(before).hexdigest(),
                  keymap_source_after_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  relocation_recipe_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  binary_sha256=hashlib.sha256((work / 'Xtigervnc').read_bytes()).hexdigest())
    (work / 'TIGERVNC-BUILD.json').write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    relocate()
