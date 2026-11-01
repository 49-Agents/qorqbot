"""Canonical domain errors; adapters should translate these without rewriting them."""

from .contracts import DomainError

__all__ = ["DomainError"]
