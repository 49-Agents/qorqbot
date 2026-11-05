"""Errors of the operational-history export library.

A ``HistoryCompositionError`` means trusted export composition is wrong (an invalid contribution, a classification of
a table that does not exist, a declared column the schema lacks). It is never a request-time error. A
``HistoryExportError`` is a ``DomainError`` for a value that violates the declared closed export contract or for an
export that was not allowed to run. Neither ever carries a stored value: only table, field and Python type names.
"""

from __future__ import annotations

from qorqut_operations.contracts import DomainError


class HistoryCompositionError(ValueError):
    """The trusted export composition is invalid or does not match the schema it is exported from."""


class HistoryExportError(DomainError):
    def __init__(self, code: str, message: str, *, table: str | None = None, field: str | None = None):
        details = {key: value for key, value in (("table", table), ("field", field)) if value is not None}
        super().__init__(code, message, details=details or None)
        self.table, self.field = table, field
