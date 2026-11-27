"""Completion-review policy. The host retains authorized reads and durable claims."""

from .instructions import extension_notes

REVIEW = "work-review"
# The correction the activity hook accepts (``checkpoint_stop``) and the gap between Qorqbot's text and extensions' texts.
CORRECTION_LIMIT = 2500
NOTES_SEPARATOR = "\n\n"


def review_required(*, coordinator, dirty_sequence, reviewed):
    return not coordinator and bool(dirty_sequence) and not reviewed


def closes(key, issues, reviewed):
    if key in issues:
        return False
    return not key.startswith(REVIEW) or reviewed or any(k.startswith(REVIEW) for k in issues)


def result(instructions, issues, saved, claimed, state, *, notes=(), rest_file=None, spilled=()):
    """The checkpoint answer. ``claimed`` are Qorqbot's own claimed messages; they come first and keep today's bound.
    ``notes`` are the claimed ``(extension name, text)`` pairs of extensions' ``stop`` entries: they follow while the whole
    correction fits ``CORRECTION_LIMIT``, and the rest is returned as ``correction_rest`` for ``rest_file``, and ``notes_shown`` says how many are in the correction. When notes do not
    all fit, the sentence pointing to that file is always in the correction, and Qorqbot's text is cut further if needed.
    ``spilled`` are claimed pairs that go to the rest file whatever the room (a text longer than a finding row holds)."""
    answer = {"state": state, "outstanding_count": len(saved),
              "issues": [{"code": issue["code"], "message": issue["message"],
                          "correction_attempted": bool(saved.get(key, {}).get("claimed_at"))}
                         for key, issue in issues.items()],
              "correction": "\n".join(dict.fromkeys(claimed))[:CORRECTION_LIMIT] if claimed else None}
    if notes or spilled:
        answer["correction"], rest, answer["notes_shown"] = extension_notes(
            instructions, answer["correction"], notes, CORRECTION_LIMIT, rest_file, separator=NOTES_SEPARATOR,
            spilled=spilled)
        if rest is not None:
            answer["correction_rest"] = {"path": rest_file, "text": rest}
    return answer
