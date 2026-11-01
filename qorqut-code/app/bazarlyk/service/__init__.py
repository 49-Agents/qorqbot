"""Single-process dashboard and HTTP MCP service.

The assembled runtime and web application load on first use: the host contributions the Operations engine is built
from (the change feed published after commit) live in this package and must be importable before the runtime that
consumes them."""

__all__ = ["Runtime", "build_runtime", "create_app"]


def __getattr__(name):
    if name in {"Runtime", "build_runtime"}:
        from . import runtime
        return getattr(runtime, name)
    if name == "create_app":
        from .web import create_app
        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
