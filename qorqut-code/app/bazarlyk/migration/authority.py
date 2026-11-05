"""The error class shared by the snapshot and recovery helpers.

The legacy writer barrier that once lived here (authority marker, freeze, activate, restore) was retired with the
numbered-migration lifecycle; this import path stays so snapshots and recovery raise and catch the same ``AuthorityError``.
"""

from __future__ import annotations


class AuthorityError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
