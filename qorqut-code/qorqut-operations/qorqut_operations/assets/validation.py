"""Asset and upload file-name validation."""

from __future__ import annotations

from pathlib import PurePosixPath
import re

from qorqut_operations.contracts import DomainError


def filename(value):
    base = str(value).replace("\\", "/").strip().split("/")[-1]
    if not base or base.startswith("."): raise DomainError("validation_error", "Invalid asset filename")
    stem, extension = PurePosixPath(base).stem, PurePosixPath(base).suffix
    stem = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
    extension = re.sub(r"[^a-z0-9]", "", extension.lower())
    if not stem: raise DomainError("validation_error", "Invalid asset filename")
    return stem[:160] + ("." + extension[:20] if extension else "")
