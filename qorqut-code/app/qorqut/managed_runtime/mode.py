"""Transitional shim: there is no per-agent mode any more, every agent runs as a managed session.

Only ``state_root`` is kept, because ``service/main.py`` (line 74) still imports it from here: change that import to
``qorqut.managed_runtime.state`` and delete this file.
"""

from __future__ import annotations

from .state import state_root  # noqa: F401 - re-exported for the importer above
