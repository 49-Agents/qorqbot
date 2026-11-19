"""What makes text written for the owner unfit to read: record identifiers and revision numbers where words belong.

The owner reads status lines and the notes posted for them. A full record ID, a short ID or commit hash, or a table
revision in that text is working detail pasted in place of a sentence; the record is linked instead and the detail goes
in a working note or a file.
"""

from __future__ import annotations

import re

RECORD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
# Seven or more hex characters standing alone with at least one digit and one letter: a short record ID or a commit.
# A number (no letter) and a word (no digit) never match.
SHORT_ID = re.compile(r"(?<![0-9A-Za-z_])(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,}(?![0-9A-Za-z_])", re.IGNORECASE)
# "rev 65", "revision 24", "r918": a stored revision of a record.
REVISION = re.compile(r"\brev(?:ision)?\.?\s*#?\d+\b", re.IGNORECASE)
REVISION_SHORT = re.compile(r"\br\d{2,}\b")


def problem(text: str) -> str | None:
    """Why ``text`` is not written for the owner, or ``None`` when it is."""
    if RECORD_ID.search(text) or SHORT_ID.search(text):
        return "it names a record by identifier"
    if REVISION.search(text) or REVISION_SHORT.search(text):
        return "it quotes a record revision"
    return None


__all__ = ["RECORD_ID", "problem"]
