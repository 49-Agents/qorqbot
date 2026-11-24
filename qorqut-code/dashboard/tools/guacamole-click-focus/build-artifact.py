#!/usr/bin/env python3
"""Build a reproducible Guacamole extension JAR from the checked-in sources."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
FILES = ("guac-manifest.json", "click-clipboard.css", "click-clipboard.js", "click-focus.js")
EPOCH_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def build(output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative in FILES:
                source = ROOT / relative
                if not source.is_file():
                    raise FileNotFoundError(source)
                info = zipfile.ZipInfo(relative, EPOCH_ZIP_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, source.read_bytes())
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()

    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(f"{output} sha256={digest}")
    return digest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "guacamole-click-focus-1.0.0.jar",
    )
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
