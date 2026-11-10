"""``DomainError`` on its own, for modules that must import quickly.

The error is part of the contracts and is still imported from ``qorqut_operations.contracts`` everywhere it is
caught, declared or documented; that module re-exports this class, so there is one class under both names.

It is defined here because importing the contracts module builds the engine's whole contract vocabulary, tens of
milliseconds, and some modules are loaded by processes that live for one tool call: the host's activity hook runs
after every native tool call, and what it loads can refuse with a ``DomainError`` but almost never does. Such a
module imports the class from here and pays for the contracts only if an error carries structured values.
"""



class DomainError(Exception):
    def __init__(self, code, message, *, retryable=False, field_errors=None, current_revision=None, details=None):
        """``code`` and ``message`` are strings; ``field_errors`` and ``details`` are JSON mappings, kept frozen."""
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.field_errors = _contracts().freeze(field_errors) if field_errors is not None else None
        self.current_revision = current_revision
        self.details = _contracts().freeze(details) if details is not None else None

    def to_dict(self):
        result = {"code": self.code, "message": self.message, "retryable": self.retryable}
        for key in ("field_errors", "current_revision", "details"):
            value = getattr(self, key)
            if value is not None:
                result[key] = _contracts().plain(value)
        return result


def _contracts():
    """The JSON value rules (``freeze``, ``plain``) live with the contracts and are loaded when first needed."""
    from qorqut_operations import contracts
    return contracts
