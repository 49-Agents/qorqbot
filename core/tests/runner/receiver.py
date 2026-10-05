"""Synthetic raw terminal receiver, never a model CLI or transcript recorder."""

import hashlib
import json
import os
from pathlib import Path
import select
import sys
import tty


def main():
    root = Path(sys.argv[1])
    tty.setraw(0)
    os.write(1, b"\x1b[?2004hfixture ready\r\n")
    begin, end = b"\x1b[200~", b"\x1b[201~"
    waiting = bytearray()
    current = b""
    pasting = False
    submitted = []
    pastes = 0
    last_paste = None
    with (root / "input.bin").open("ab", buffering=0) as archive:
        while True:
            size = os.get_terminal_size(0)
            state = {"pid": os.getpid(), "current_sha256": hashlib.sha256(current).hexdigest(),
                     "current_bytes": len(current), "last_paste": last_paste,
                     "submitted": submitted, "pastes": pastes, "columns": size.columns, "rows": size.lines}
            temporary = root / "state.tmp"
            temporary.write_text(json.dumps(state))
            temporary.replace(root / "state.json")
            if not select.select([0], [], [], 0.01)[0]:
                continue
            chunk = os.read(0, 65536)
            if not chunk:
                return
            archive.write(chunk)
            waiting.extend(chunk)
            while waiting:
                if pasting:
                    index = waiting.find(end)
                    if index < 0:
                        break
                    current = bytes(waiting[:index])
                    del waiting[:index + len(end)]
                    pastes += 1
                    last_paste = hashlib.sha256(current).hexdigest()
                    pasting = False
                elif waiting.startswith(begin):
                    del waiting[:len(begin)]
                    pasting = True
                elif begin.startswith(waiting):
                    break
                elif waiting[0] == 13:
                    submitted.append(hashlib.sha256(current).hexdigest())
                    current = b""
                    del waiting[0]
                else:
                    current += bytes(waiting[:1])
                    del waiting[0]


if __name__ == "__main__":
    main()
