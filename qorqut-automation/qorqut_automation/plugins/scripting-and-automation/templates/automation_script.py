"""Minimal automation script: one phase, one durable read, one published result, bounded by the invocation ceiling.

Replace ``tasks_list`` with the read your automation needs (a script can call any of Ordubashy's own tools except ``automations_*``,
``agentic_change_*``, ``management_*``, ``permissions_*`` and ``profile_*``). A Project automation's calls use its Project automatically.
"""

import qorqut_automation_sdk as sdk


def work(run):
    run.phase("check")
    result = run.call("tasks_list", {"limit": 50}, key="list-tasks")
    count = len(result.get("tasks", []))
    if count and run.saved.get("last_count") != count:
        run.publish("count-%d" % count, "Tasks listed: %d" % count, data={"count": count}, advance={"last_count": count})
    return {"count": count}


sdk.main(work)
