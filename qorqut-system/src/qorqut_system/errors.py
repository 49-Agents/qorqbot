"""The errors the system layer raises.

Feature code translates these into its own error type, so callers keep the type they saw before the move.
"""


class LayerError(Exception):
    """A system operation could not be done, for a reason the message explains."""


class PrivateFileError(LayerError):
    """A private folder or file does not meet its rules."""


class UnsupportedSystem(LayerError):
    """The operation is not available on this system in this version of the layer."""


class UnsafePath(LayerError):
    """A path cannot be used for this operation without risking another file."""


class PathChanged(UnsafePath):
    """The path stopped being the object an operation created while the operation was securing it."""
