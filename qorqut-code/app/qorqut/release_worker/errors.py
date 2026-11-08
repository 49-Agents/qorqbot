class ReleaseError(RuntimeError):
    """A safe, operator-facing release failure.

    Errors intentionally contain paths, identifiers and remediation guidance,
    but never command output.  Command output is redacted by ``io.py`` before
    it is put in a record.
    """


class UnsupportedAdapter(ReleaseError):
    """The manifest names a component without a supported deployment recipe."""


class StaleApproval(ReleaseError):
    """An authorization no longer binds the frozen plan."""


class ReconciliationRequired(ReleaseError):
    """An interrupted external step must be reconciled before it can run again."""


class UncertainRemoteEffect(ReleaseError):
    """A remote transport failure may have left the destination operation running."""
