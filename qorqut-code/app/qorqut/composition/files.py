"""File-target resolution and lock locations, selected explicitly for the target instance.

The engine's ``FileApplier`` checks exact target, hash, revision and authority and keeps the ``.qorqut-``
temporary-file prefix; it asks this resolver which registered root and relative path a prepared file resource names.
Resolvers are registered by trusted assembly code per resource type before the service serves traffic; no request can
add one. ``LockPaths`` come from the instance's runtime directory, never from a discovered installation root."""

from __future__ import annotations

from qorqut_operations.storage.locks import LockPaths

from qorqut.instance_paths import InstancePaths


def lock_paths(paths: InstancePaths) -> LockPaths:
    """``operations/.runtime/locks``: application claims, per-receipt apply locks and ``recovery.lock``."""
    return LockPaths.under(paths.locks)


class FileTargetResolver:
    """Resource type -> trusted resolver, with the document store's resolver as the default for other types."""

    def __init__(self, default):
        if not callable(default):
            raise TypeError("A default file target resolver is required")
        self._default = default
        self._resolvers = {}
        self._sealed = False

    def register(self, resource_type, resolver):
        if self._sealed:
            raise ValueError("File target resolvers are registered before the service starts")
        if resource_type in self._resolvers:
            raise ValueError(f"File target resolver already registered: {resource_type}")
        if not callable(resolver):
            raise TypeError("A file target resolver must be callable")
        self._resolvers[resource_type] = resolver
        return self

    def seal(self):
        self._sealed = True
        return self

    def __call__(self, resource):
        return self._resolvers.get(resource.resource_type, self._default)(resource)
