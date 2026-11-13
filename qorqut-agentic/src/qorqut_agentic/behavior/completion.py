"""Completion-review policy. The host retains authorized reads and durable claims."""

REVIEW = "work-review"


def review_required(*, coordinator, dirty_sequence, reviewed):
    return not coordinator and bool(dirty_sequence) and not reviewed


def closes(key, issues, reviewed):
    if key in issues:
        return False
    return not key.startswith(REVIEW) or reviewed or any(k.startswith(REVIEW) for k in issues)


def result(instructions, issues, saved, claimed, state):
    return {"state": state, "outstanding_count": len(saved),
            "issues": [{"code": issue["code"], "message": issue["message"],
                        "correction_attempted": bool(saved.get(key, {}).get("claimed_at"))}
                       for key, issue in issues.items()],
            "correction": "\n".join(dict.fromkeys(claimed))[:2500] if claimed else None}
