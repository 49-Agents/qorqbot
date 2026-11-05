"""Tiny hand-assembled wheels for installation-tooling checks: no build backend, index or network is involved."""
import base64
import hashlib
from pathlib import Path
import subprocess
import sys
import zipfile


def record_hash(content: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()


def make_wheel(directory, name, version, files=None, requires=()):
    """Write ``name-version-py3-none-any.whl`` below ``directory``; ``files`` maps archive paths to bytes."""
    stem = name.replace("-", "_")
    info = f"{stem}-{version}.dist-info"
    metadata = "".join(f"{key}: {value}\n" for key, value in
                       [("Metadata-Version", "2.1"), ("Name", name), ("Version", version), *(("Requires-Dist", r) for r in requires)])
    members = {**(files or {f"{stem}/__init__.py": b"VALUE = 1\n"}), f"{info}/METADATA": metadata.encode(),
               f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: synthetic\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
               f"{info}/top_level.txt": (stem + "\n").encode()}
    record = "".join(f"{path},{record_hash(content)},{len(content)}\n" for path, content in members.items()) + f"{info}/RECORD,,\n"
    path = Path(directory) / f"{stem}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for member, content in {**members, f"{info}/RECORD": record.encode()}.items():
            archive.writestr(member, content)
    return path


def make_environment(directory, *wheels):
    """A fresh venv (ensurepip, no index) with the wheels installed as explicit files; returns its interpreter."""
    subprocess.run([sys.executable, "-m", "venv", str(directory)], check=True, capture_output=True)
    python = Path(directory) / "bin" / "python"
    if wheels:
        subprocess.run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--disable-pip-version-check",
                        *map(str, wheels)], check=True, capture_output=True)
    return python
