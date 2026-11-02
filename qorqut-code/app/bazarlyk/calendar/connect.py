"""Provider-neutral saving of one calendar connection into the private ``calendar.json``.

Each provider package supplies its own one-time setup that calls ``save_connection``: ``python -m qorqut_google_calendar.connect``
for Google and ``python -m qorqut_fastmail.calendar_connect`` for Fastmail. Core has no provider setup of its own."""

import json
import os
from pathlib import Path
import tempfile


def save_connection(path, identity, connection):
    path = Path(path)
    data = json.loads(path.read_text()) if path.exists() else {"connections": {}}
    data.setdefault("connections", {})[identity] = connection
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".calendar-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(data, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
