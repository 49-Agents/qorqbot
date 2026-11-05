"""The inbox source ``automations`` and the read-only ``application.automations`` reference helper.

Source readability is not a separate access list. It is exactly the canonical read: the current operation policy for ``automations_get``
(the engine's own authorization, evaluated for the supplied context in the automation's own scope: Global, or its Project) with the
principal's active identity. A subscription grants nothing, a helper never allows "any agent", and a refused or retired reader simply
stops seeing the source. Events addressed to the managing side (failures, questions, recovery notices) are visible only to the owner and
the configured Ordubashy; published output is visible to every reader the canonical read admits. The helper writes nothing and returns
no script, input, grant, restriction or saved-state content."""

from __future__ import annotations

import json

from qorqut_operations.contracts import DomainError

from bazarlyk.extensions import SourceHandler

from .events import MANAGERS, ordubashy
from .records import ITEM_TYPE, SOURCE

READ = "automations_get"


class AutomationReferences:
    """Installed as ``application.automations``."""

    def __init__(self, runtime):
        self.runtime = runtime

    def _row(self, identity, business, uow):
        if not isinstance(identity, str):
            raise DomainError("not_found", "Automation does not exist")
        found = uow.connection.execute("SELECT * FROM automations WHERE id=?", (identity,)).fetchone()
        if found is None and business is not False:
            found = uow.connection.execute("SELECT * FROM automations WHERE slug=? AND business_id IS ?", (identity, business or None)).fetchone()
        if found is None or (business is not False and found["business_id"] != business):
            raise DomainError("not_found", "Automation does not exist")
        return found

    def authorize(self, ctx, row, uow):
        """The canonical read decision for this automation's scope; raises ``DomainError`` unless the context may read it now."""
        app = self.runtime.application
        selected = ctx.select_business(row["business_id"])
        namespace = "business:" + row["business_id"] if row["business_id"] else "global"
        try:
            app.authorize(selected, app.registry.get(READ), (), uow, namespace=namespace)
        except DomainError as exc:
            raise DomainError("not_found", "Automation does not exist") from exc

    def manages(self, ctx, uow) -> bool:
        """Whether this context may see the managing side of automations: the owner, or the configured Ordubashy."""
        if ctx.principal_kind == "human":
            installation = uow.repository("installation").get("service")
            return bool(installation and installation["owner_principal_id"] == ctx.principal_id)
        return ctx.principal_kind == "agent" and ctx.automation_run_id is None and ctx.principal_id == ordubashy(uow)

    def reference(self, ctx, identity, uow, business=False):
        """``{'type', 'id', 'business_id', 'slug', 'title', 'description', 'revision', 'archived'}`` of one visible automation.

        ``business`` pins the expected scope (``None`` for Global); left ``False`` the automation is found by its stable id."""
        row = self._row(identity, business, uow)
        self.authorize(ctx, row, uow)
        return {"type": ITEM_TYPE, "id": row["id"], "business_id": row["business_id"], "slug": row["slug"], "title": row["title"],
                "description": row["description"], "revision": row["revision"], "archived": row["state"] == "archived"}

    def visible(self, ctx, identity, uow, business=False) -> bool:
        try:
            self.reference(ctx, identity, uow, business)
            return True
        except DomainError:
            return False


def _audience(event):
    value = event["visibility_json"] if "visibility_json" in event.keys() else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    return (value or {}).get("audience")


def source(references: AutomationReferences) -> SourceHandler:
    """The subscribable ``automations`` source. Events are published directly with their deliveries (see ``events``)."""

    def row(uow, business, identity):
        found = uow.connection.execute("SELECT * FROM automations WHERE id=? AND business_id IS ?", (identity, business)).fetchone()
        return {key: found[key] for key in ("id", "business_id", "slug", "title", "state", "revision", "updated_at")} if found else None

    def subscription_visible(service, ctx, identity, business, uow):
        return references.visible(ctx, identity, uow, business)

    def event_visible(service, ctx, event, uow):
        if not references.visible(ctx, event["source_id"], uow, event["business_id"]):
            return False
        return _audience(event) != MANAGERS or references.manages(ctx, uow)

    def reference(inbox, ctx, item, business, uow):
        found = references.reference(ctx, item["id"], uow, business)
        return {"label": found["title"], "revision": found["revision"], "resource_type": SOURCE, "resource_id": found["id"],
                "archived": found["archived"]}

    return SourceHandler(SOURCE, item_type=ITEM_TYPE, subscribable=True, row=row, subscription_visible=subscription_visible,
                         event_visible=event_visible, reference=reference)
