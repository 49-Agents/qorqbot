"""Exclusive publication of a fully written file, on the version this system uses."""

from __future__ import annotations

import sys

if sys.platform == "win32":
    from .publication_windows import (RENAME_EXCL, Pending, anonymous_file, discard, prepare_publication, publish,
                                      publication_supported, require_anonymous_publication, require_same_device)
else:
    from .publication_unix import (RENAME_EXCL, Pending, anonymous_file, discard, prepare_publication, publish,
                                   publication_supported, require_anonymous_publication, require_same_device)

__all__ = ["RENAME_EXCL", "Pending", "anonymous_file", "discard", "prepare_publication", "publish",
           "publication_supported", "require_anonymous_publication", "require_same_device"]