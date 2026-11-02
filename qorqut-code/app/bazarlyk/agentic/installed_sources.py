"""Bounded, RECORD-verified instruction sources from explicitly selected wheels."""
from importlib import metadata
import base64

from .support import ImportFailure, absolute, digest, relative


CONNECTORS = frozenset({"telegram", "whatsapp", "fastmail"})


def installed_source(source_id):
    if source_id not in CONNECTORS:
        raise ImportFailure("invalid_source", "Unsupported installed instruction owner")
    distribution_name = "qorqut-" + source_id
    package_name = "qorqut_" + source_id
    try:
        distribution = metadata.distribution(distribution_name)
        record = distribution.read_text("RECORD")
        if not record:
            raise ValueError("A wheel RECORD is required")
        root = absolute(str(distribution.locate_file("")))
        files = {}
        for item in distribution.files or ():
            path = str(item)
            if not path.startswith(package_name + "/") or item.suffix not in {".py", ".md", ".json"}:
                continue
            relative(path)
            if item.hash is None or item.hash.mode != "sha256":
                raise ValueError("Instruction source has no SHA-256 RECORD entry: " + path)
            sha = base64.urlsafe_b64decode(item.hash.value + "=" * (-len(item.hash.value) % 4)).hex()
            if len(sha) != 64:
                raise ValueError("Invalid RECORD digest")
            files[path] = sha
        if not files:
            raise ValueError("Installed wheel has no instruction resources")
        return dict(root=str(root), distribution=distribution_name, version=distribution.version,
                    distribution_record_sha256=digest(record.encode()), distribution_files=files)
    except (metadata.PackageNotFoundError, OSError, ValueError) as exc:
        raise ImportFailure("invalid_installed_source", "Cannot use installed " + distribution_name + ": " + str(exc)) from exc
