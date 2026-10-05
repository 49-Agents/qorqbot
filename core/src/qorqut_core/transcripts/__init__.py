"""Local raw capture. Terminal output is never a semantic chat transcript."""

from .spool import CaptureSpool, RunBinding, Source, ensure_schema

__all__ = ["CaptureSpool", "RunBinding", "Source", "ensure_schema"]
