"""Installation-wide System Dev reports with scoped owner review.

Repair-ticket removal (phase 1) retired the agent tools (create, agent list, follow-up and resolve). The owner queue and
the retained internal scoped reader keep their own rules; the system incident reporter still stores observations until
phase 2 removes the feature and its data. The scoped reader serves the surviving Task, Signoff and Project references
and is refused to agents by the MCP caller's discovery and dispatch."""

from qorqut_agentic.behavior.tool_help import text as instruction_text

from bazarlyk.agents.global_scope import active_global, reaches_business
from qorqut_operations.application import OperationDescriptor
from qorqut_operations.contracts import DomainError, new_id
from qorqut_operations.application.plans import PlanBuilder, apply_records, require_revision, schema, timestamp, utc_clock

DETAILS = {"type": "string", "maxLength": 12000}
STATUS = {"enum": ["open", "closed"]}
UUID = {"type": "string", "format": "uuid"}


class DevTickets:
    def __init__(self, *, clock=utc_clock):
        self.clock = clock

    def register(self, registry):
        common = dict(scope_kinds=frozenset({"global"}), supports_signoff=False,
                      owner_only=True, global_capability="read_global_context", exposure=frozenset({"dashboard"}))
        registry.register(OperationDescriptor("dev_tickets_list",
            schema({"status": {"enum": ["open", "closed", "all"]}, "details": {"type": "boolean"},
                    "ticket_id": {"type": "string", "format": "uuid"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "offset": {"type": "integer", "minimum": 0}}),
            **common, effect_kind="read", description=instruction_text('dev-tickets-owner-list-system-dev-reports-open-by'),
            read=self.list))
        registry.register(OperationDescriptor("dev_tickets_status_set",
            schema({"ticket_id": UUID, "status": STATUS, "resolution": DETAILS, "verification": DETAILS,
                    "deployment_ref": {"type": "string", "maxLength": 200}}, ("ticket_id", "status")),
            **common, description=instruction_text('dev-tickets-owner-close-or-reopen-a-system-dev'),
            prepare=self.prepare_status, apply=self.apply_status, validate_targets=self.validate_owner))

        # Retained internal scoped reader: surviving Task, Signoff and Project references resolve through it,
        # so it keeps its scope authorization and handler but is no longer an agent tool. The MCP caller refuses
        # it in discovery and dispatch; phase 2 removes it with the remaining views and data.
        registry.register(OperationDescriptor("dev_tickets_get",
            schema({"ticket_id": UUID, "history_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "history_offset": {"type": "integer", "minimum": 0}}, ("ticket_id",)),
            scope_kinds=frozenset({"business", "global"}), supports_signoff=False,
            global_capability="read_global_context", exposure=frozenset({"mcp"}),
            resolve_namespace=self.agent_namespace, effect_kind="read",
            description=instruction_text('dev-tickets-read-one-system-dev-report-and-its'),
            read=self.get))

    @staticmethod
    def owner(ctx, uow):
        installation = uow.repository("installation").get("service")
        if ctx.principal_kind != "human" or not installation or installation["owner_principal_id"] != ctx.principal_id:
            raise DomainError("forbidden", "Only the installation owner can review the System Dev queue")

    @staticmethod
    def agent_namespace(ctx, _args):
        return "global" if ctx.business_id is None else "business:" + ctx.business_id

    @staticmethod
    def role(ctx):
        return ctx.role_artifact_ref.id if ctx.role_artifact_ref else None

    def in_scope(self, ctx, ticket, uow):
        if active_global(uow.connection, ctx.principal_id) is None:
            return False
        business = ticket["business_id"]
        if business is None:
            return ctx.business_id is None and "read_global_context" in ctx.global_capabilities
        if ctx.business_id is not None and business != ctx.business_id:
            return False
        return business in ctx.allowed_business_ids and reaches_business(uow.connection, ctx.principal_id, business)

    def agent_access(self, ctx, ticket, uow=None, *, read_only=False):
        if ctx.principal_kind != "agent":
            raise DomainError("forbidden", "System Dev agent tools require an authenticated agent")
        if uow is None or not self.in_scope(ctx, ticket, uow):
            raise DomainError("forbidden", "System Dev report is outside your authorized scope")
        if ticket["reporter_id"] == ctx.principal_id:
            return True
        # System-origin incidents are visible through an explicit impact link,
        # while retaining the ordinary reach/scope checks below.
        if read_only and uow is not None and uow.connection.execute(
                "SELECT 1 FROM principals p JOIN system_incidents i ON i.ticket_id=? "
                "JOIN system_incident_impacts x ON x.incident_id=i.id "
                "WHERE p.id=? AND p.kind='system' AND "
                "((x.agent_id=? AND x.agent_id IS NOT NULL) OR "
                "(x.business_id=? AND x.business_id IS NOT NULL)) LIMIT 1",
                (ticket["id"], ticket["reporter_id"], ctx.principal_id, ctx.business_id)).fetchone():
            if self.in_scope(ctx, {"business_id": ctx.business_id}, uow):
                return True
        if self.role(ctx) == "ordubashy":
            if ticket["business_id"] is None:
                return "read_global_context" in ctx.global_capabilities
            if ticket["business_id"] in ctx.allowed_business_ids:
                return True
        raise DomainError("forbidden", "System Dev report is outside your authorized scope")

    @staticmethod
    def ticket(uow, ticket_id):
        return uow.repository("dev_tickets").get(ticket_id, required=True)

    @staticmethod
    def event_rows(uow, ticket_id, *, limit=50, offset=0):
        rows = [dict(row) for row in uow.connection.execute(
            "SELECT id,ticket_id,event_kind,actor_id,created_at,details,resolution,verification,deployment_ref "
            "FROM dev_ticket_events WHERE ticket_id=? ORDER BY created_at,id LIMIT ? OFFSET ?",
            (ticket_id, limit + 1, offset))]
        return rows[:limit], offset + limit if len(rows) > limit else None

    @staticmethod
    def insert_event(uow, ticket_id, event_kind, actor_id, *, details="", resolution="", verification="",
                     deployment_ref="", created_at=None):
        uow.connection.execute(
            "INSERT INTO dev_ticket_events(id,ticket_id,event_kind,actor_id,created_at,details,resolution,verification,deployment_ref) "
            "VALUES(?,?,?,?,?,?,?,?,?)", (new_id(), ticket_id, event_kind, actor_id, created_at or timestamp(),
                                            details, resolution, verification, deployment_ref))

    def list(self, ctx, args, opts, uow):
        self.owner(ctx, uow)
        status = args.get("status", "open")
        clauses, params = [], []
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        if args.get("ticket_id"):
            clauses.append("id=?")
            params.append(args["ticket_id"])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        columns = "*" if args.get("details", False) else "id,title,kind,status,reporter_id,business_id,created_at,updated_at,revision"
        columns += ',(SELECT i.id FROM system_incidents i WHERE i.ticket_id=dev_tickets.id) AS incident_id'
        limit, offset = args.get("limit", 50), args.get("offset", 0)
        items = [dict(row) for row in uow.connection.execute(
            f"SELECT {columns} FROM dev_tickets{where} ORDER BY created_at,id LIMIT ? OFFSET ?",
            [*params, limit + 1, offset])]
        return {"items": items[:limit], "next_offset": offset + limit if len(items) > limit else None}

    def get(self, ctx, args, opts, uow):
        ticket = self.ticket(uow, args["ticket_id"])
        self.agent_access(ctx, ticket, uow, read_only=True)
        events, next_offset = self.event_rows(uow, ticket["id"], limit=args.get("history_limit", 50),
                                              offset=args.get("history_offset", 0))
        return {"ticket": ticket, "events": events, "events_next_offset": next_offset}

    def prepare_status(self, ctx, args, opts, uow):
        self.owner(ctx, uow)
        before = self.ticket(uow, args["ticket_id"])
        require_revision(before, opts.expected_revision)
        incident = uow.connection.execute('SELECT id FROM system_incidents WHERE ticket_id=?',(before['id'],)).fetchone()
        if incident and args['status'] == 'closed' and (not args.get('resolution','').strip() or not args.get('verification','').strip()):
            raise DomainError('validation_error','Automatic incident closure requires resolution and verification evidence')
        builder = PlanBuilder(ctx, "dev_tickets_status_set", args, "System Dev report status", namespace="global",
                              affected_scope={"kind": "global"})
        values = {"status": args["status"], "updated_at": builder.at}
        if args["status"] == "open" and before["status"] != "open":
            values.update(resolved_at=None, resolved_by=None, resolution="")
        elif args["status"] == "closed" and before["status"] != "closed":
            values.update(resolved_at=builder.at, resolved_by=ctx.principal_id, resolution=args.get('resolution','').strip())
        builder.record("dev_tickets", args["ticket_id"], before, values, business_id=None, namespace="global")
        return builder.build()

    def validate_owner(self, ctx, plan, uow):
        self.owner(ctx, uow)

    def apply_status(self, ctx, plan, uow):
        self.owner(ctx, uow)
        before = self.ticket(uow, plan.arguments["ticket_id"])
        ticket = apply_records(ctx, plan, uow)[0]
        if before["status"] != ticket["status"]:
            self.insert_event(uow, ticket["id"], "status_changed", ctx.principal_id,
                              details="Owner changed report status to " + ticket["status"],
                              created_at=ticket["updated_at"])
            verified = bool(plan.arguments.get('resolution','').strip() and plan.arguments.get('verification','').strip())
            if verified and ticket['status'] == 'closed':
                self.insert_event(uow, ticket['id'], 'resolved', ctx.principal_id,
                    resolution=plan.arguments['resolution'].strip(), verification=plan.arguments['verification'].strip(),
                    deployment_ref=plan.arguments.get('deployment_ref',''), created_at=ticket['updated_at'])
            from bazarlyk.incidents import IncidentReporter
            IncidentReporter.ticket_status(uow,ticket,ctx.principal_id,verified=verified)
        return {"ticket_id": ticket["id"], "status": ticket["status"], "revision": ticket["revision"], "queue": "System Dev"}

