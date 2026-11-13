"""Explicit provisioning of the Context repository and runtime of one instance. Never repair, adoption or startup work.

    python -m qorqut.bootstrap.context_repository --instance-root INSTANCE --component NAME=PATH ...

Reads and normal service startup never create a missing Context store; this is the one explicit creation. The fresh-instance
initializer calls ``provision_context_repository``, and the packaged launcher runs this command for an installation that was
initialized before the store was provisioned. Anything already at ``operations/context-graph`` is left exactly as found and
reported as not created. The store is created without initial categories, with its canonical personal context node."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from qorqut.instance_paths import InstancePaths
from qorqut.service.instance_arguments import add_instance_arguments, instance_paths_from_arguments


@dataclass(frozen=True)
class ContextRepository:
    """``created`` is False when the repository path already existed: nothing was read or changed, and ``revision`` is None."""

    path: Path
    runtime: Path
    created: bool
    revision: str | None = None


def provision_context_repository(paths: InstancePaths) -> ContextRepository:
    """Create the empty Context repository and its runtime unless something already exists at the repository path."""
    path, runtime = paths.context, paths.context_runtime
    if os.path.lexists(path):
        return ContextRepository(path, runtime, False)
    from qorqut_context.engine import Context
    context = Context.initialize(path, runtime)
    return ContextRepository(path, runtime, True, context.git.head())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create the Context repository of an instance when it is missing (starts nothing).")
    add_instance_arguments(parser)
    args = parser.parse_args(argv)
    previous_umask = os.umask(0o077)
    try:
        paths = instance_paths_from_arguments(args, required=("context_engine",))
        result = provision_context_repository(paths)
    except Exception as exc:                  # includes the Context engine's own errors
        print(f"context repository: failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)
    print(f"context repository: {result.path} " + (f"created at {result.revision}" if result.created else "already existed; left untouched"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
