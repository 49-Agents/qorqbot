"""Completion-review policy. The host retains authorized reads and durable claims."""

REVIEW = "work-review"
FOCUS_REVIEW = "focus-review"


def review_required(*, focus_only, coordinator, dirty_sequence, reviewed):
    return not focus_only and not coordinator and bool(dirty_sequence) and not reviewed


def closes(key, issues, reviewed):
    if key in issues:
        return False
    return not key.startswith(REVIEW) or reviewed or any(k.startswith(REVIEW) for k in issues)


def result(instructions, issues, saved, claimed, state):
    # Preserve the ordering of historical Focus corrections without re-enabling that check.
    focus = instructions.render("completion-focus-retired")
    if focus in claimed:
        claimed = [focus, *[message for message in claimed if message != focus]]
    return {"state": state, "outstanding_count": len(saved),
            "issues": [{"code": issue["code"], "message": issue["message"],
                        "correction_attempted": bool(saved.get(key, {}).get("claimed_at"))}
                       for key, issue in issues.items()],
            "correction": "\n".join(dict.fromkeys(claimed))[:2500] if claimed else None}
