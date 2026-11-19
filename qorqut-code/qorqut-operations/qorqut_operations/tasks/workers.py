"""Safe, shared Task Worker identity projection."""


class People:
    """Principals, agent profiles and the owner, each read once however many Tasks name them."""

    def __init__(self, uow):
        self.uow, self.principals, self.profiles = uow, {}, {}
        installation = uow.repository("installation").get("service")
        self.owner = installation["owner_principal_id"] if installation else None

    def principal(self, identity):
        if identity not in self.principals:
            self.principals[identity] = self.uow.repository("principals").get(identity)
        return self.principals[identity]

    def profile(self, identity):
        if identity not in self.profiles:
            self.profiles[identity] = self.uow.repository("agent_profiles", business_id=None).get(identity)
        return self.profiles[identity]


def project_workers(uow, task, people=None):
    people = people or People(uow)
    ids = list(task.get("worker_ids_json") or [])
    workers = []
    for identity in ids:
        principal = people.principal(identity)
        if not principal or principal["kind"] not in {"human", "agent"}:
            continue
        profile = people.profile(identity) if principal["kind"] == "agent" else None
        workers.append({"id": identity, "kind": principal["kind"], "display_name": principal["display_name"],
                        "is_owner": identity == people.owner, **({"name": profile["name"]} if profile else {})})
    return {"worker_ids": ids, "workers": workers, "user_needed": people.owner is not None and people.owner in ids,
            "latest_completed_at": task.get("latest_completed_at")}
