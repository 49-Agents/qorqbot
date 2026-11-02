"""Safe, shared Task Worker identity projection."""


def project_workers(uow, task):
    ids = list(task.get("worker_ids_json") or [])
    installation = uow.repository("installation").get("service")
    owner = installation["owner_principal_id"] if installation else None
    workers = []
    for identity in ids:
        principal = uow.repository("principals").get(identity)
        if not principal or principal["kind"] not in {"human", "agent"}:
            continue
        profile = uow.repository("agent_profiles", business_id=None).get(identity) if principal["kind"] == "agent" else None
        workers.append({"id": identity, "kind": principal["kind"], "display_name": principal["display_name"],
                        "is_owner": identity == owner, **({"name": profile["name"]} if profile else {})})
    return {"worker_ids": ids, "workers": workers, "user_needed": owner is not None and owner in ids,
            "latest_completed_at": task.get("latest_completed_at")}
