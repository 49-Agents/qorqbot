"""Read-only installed view for explicit offline migration inspection.

Normal agents keep WAL-aware readers. This private view refuses nonempty WAL and
uses the same binding, store, catalogue and active-inventory validation without
creating SQLite sidecars. It cannot open a writable database connection.
"""
from qorqut_agentic import AgenticLayer
from qorqut_agentic.layer import _real, _inside, resolve_git
from qorqut_agentic.store import Store

from .installation import _failure, _validated_installed_binding, _validate_open_layer
from .layout_runtime import _offline_reader


class _OfflineStore(Store):
    reader = _offline_reader

    def connect(self, **unused):
        raise _failure("An offline inspection view cannot open database writers")


def open_installation(paths, selected_extensions):
    try:
        with _validated_installed_binding(paths, timeout=10, offline_read_only=True) as record:
            root, state = _real(paths.agentic_root, "root"), _real(paths.agentic_runtime, "state")
            if not root.is_dir() or _inside(state, root) or _inside(root, state):
                raise ValueError("Invalid offline authoring root or state path")
            store = _OfflineStore(state)
            store.verify(str(root))
            layer = AgenticLayer._build(root, store, resolve_git(None), None, None, "host_authenticated")
            return _validate_open_layer(paths, record, selected_extensions, layer)
    except Exception as exc:
        raise _failure("Cannot inspect installed agentic layer: " + str(exc)) from exc
