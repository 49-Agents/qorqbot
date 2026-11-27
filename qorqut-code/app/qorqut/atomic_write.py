"""Atomic replacement of one small file: written and flushed beside it, then renamed over it.

The operation lives in the system layer (``qorqut_system.atomic``).
"""
from qorqut_system.atomic import atomic_write

__all__ = ["atomic_write"]