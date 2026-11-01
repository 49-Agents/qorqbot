"""Installation-wide System Dev reports with scoped agent follow-up."""

import json

from bazarlyk.agents import roles
from bazarlyk.application import OperationDescriptor
from bazarlyk.contracts import DomainError, new_id
from bazarlyk.domains.common import PlanBuilder, apply_records, require_revision, schema, timestamp, utc_clock

TITLE = {"type": "string", "minLength": 1, "maxLength": 200, "pattern": r"\S"}
DETAILS = {"type": "string", "maxLength": 12000}
KIND = {"enum": ["bug", "feature", "tooling", "workflow"]}
STATUS = {"enum": ["open", "closed"]}
UUID = {"type": "string", "format": "uuid"}
AGENT_SCOPE = {"scope": {"enum": ["business", "global"]}}


class DevTickets:
    def __init__(self, *, clock=utc_clock):
        self.clock = clock

    def register(self, registry):
        registry.register(OperationDescriptor("dev_tickets_create",
            schema({"title": TITLE, "details": DETAILS, "kind": KIND}, ("title",)),
            scope_kinds=frozenset({"business", "agent_workspace"}), supports_signoff=False,
            description="File a Qorqut bug or feature/tooling/workflow request in the shared System Dev queue. A title is enough; optionally include what happened, what you needed, and known IDs/errors. Report for yourself or others with available context; no investigation required. Identity/business are automatic. Reuse the idempotency_key on retry. Filing does not assign or start development.",
            prepare=self.prepare_create, apply=self.apply_create))
        common = dict(scope_kinds=frozenset({"global"}), supports_signoff=False,
                      owner_only=True, global_capability="read_global_context", exposure=frozenset({"dashboard"}))
        registry.register(OperationDescriptor("dev_tickets_list",
            schema({"status": {"enum": ["open", "closed", "all"]}, "details": {"type": "boolean"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "offset": {"type": "integer", "minimum": 0}}),
            **common, effect_kind="read", description="Owner: list System Dev reports, open by default.",
            read=self.list))
        registry.register(OperationDescriptor("dev_tickets_status_set",
            schema({"ticket_id": UUID, "status": STATUS, "resolution": DETAILS, "verification": DETAILS,
                    "deployment_ref": {"type": "string", "maxLength": 200}}, ("ticket_id", "status")),
            **common, description="Owner: close or reopen a System Dev report using its current revision.",
            prepare=self.prepare_status, apply=self.apply_status, validate_targets=self.validate_owner))

        # Agent-facing reads and lifecycle writes deliberately use separate
        # descriptors from the owner dashboard queue. Their domain checks are
        # narrower than the installation-wide owner routes.
        agent_common = dict(scope_kinds=frozenset({"business", "global"}), supports_signoff=False,
                            global_capability="read_global_context", exposure=frozenset({"mcp"}),
                            resolve_namespace=self.agent_namespace)
        registry.register(OperationDescriptor("dev_tickets_agent_list",
            schema({**AGENT_SCOPE, "status": {"enum": ["open", "closed", "all"]},
                    "details": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "offset": {"type": "integer", "minimum": 0}}),
            **agent_common, effect_kind="read", description="Read System Dev reports in your authorized scope; other agents can read their own reports.",
            read=self.agent_list))
        registry.register(OperationDescriptor("dev_tickets_get",
            schema({"ticket_id": UUID, "history_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "history_offset": {"type": "integer", "minimum": 0}}, ("ticket_id",)),
            **agent_common, effect_kind="read", description="Read one System Dev report and its follow-up/resolution history when authorized.",
            read=self.get))
        registry.register(OperationDescriptor("dev_tickets_follow_up",
            schema({"ticket_id": UUID, "details": DETAILS}, ("ticket_id", "details")),
            **agent_common, description="Append concrete follow-up context to an open System Dev report you can access.",
            prepare=self.prepare_follow_up, apply=self.apply_follow_up, validate_targets=self.validate_agent_plan))
        registry.register(OperationDescriptor("dev_tickets_resolve",
            schema({"ticket_id": UUID, "resolution": DETAILS, "verification": DETAILS,
                    "deployment_ref": {"type": "string", "maxLength": 200}},
                   ("ticket_id", "resolution", "verification")),
            **agent_common, description="Close an authorized System Dev report with resolution and verification evidence; the actor and timestamp are recorded.",
            prepare=self.prepare_resolve, apply=self.apply_resolve, validate_targets=self.validate_agent_plan))

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
        business = ticket["business_id"]
        if business is None:
            return ctx.business_id is None and "read_global_context" in ctx.global_capabilities
        if ctx.business_id is not None and business != ctx.business_id:
            return False
        if business not in ctx.allowed_business_ids:
            return False
        return bool(uow.connection.execute(
            "SELECT 1 FROM business_memberships WHERE business_id=? AND principal_id=? AND active=1",
            (business, ctx.principal_id)).fetchone())

    def agent_access(self, ctx, ticket, uow=None, *, read_only=False):
        if ctx.principal_kind != "agent":
            raise DomainError("forbidden", "System Dev agent tools require an authenticated agent")
        if uow is None or not self.in_scope(ctx, ticket, uow):
            raise DomainError("forbidden", "System Dev report is outside your authorized scope")
        if ticket["reporter_id"] == ctx.principal_id:
            return True
        # System-origin incidents are visible through an explicit impact link,
        # while retaining the ordinary membership/scope checks below.
        if read_only and uow is not None and uow.connection.execute(
                "SELECT 1 FROM principals p JOIN system_incidents i ON i.ticket_id=? "
                "JOIN system_incident_impacts x ON x.incident_id=i.id "
                "WHERE p.id=? AND p.kind='system' AND "
                "((x.agent_id=? AND x.agent_id IS NOT NULL) OR "
                "(x.business_id=? AND x.business_id IS NOT NULL)) LIMIT 1",
                (ticket["id"], ticket["reporter_id"], ctx.principal_id, ctx.business_id)).fetchone():
            if self.in_scope(ctx, {"business_id": ctx.business_id}, uow):
                return True
        role = self.role(ctx)
        if role in roles.BUSINESS_ROLES:
            if ctx.business_id is not None and ticket["business_id"] == ctx.business_id:
                return True
        elif role == "ordubashy":
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
        where, params = ("", []) if status == "all" else (" WHERE status=?", [status])
        columns = "*" if args.get("details", False) else "id,title,kind,status,reporter_id,business_id,created_at,updated_at,revision"
        columns += ',(SELECT i.id FROM system_incidents i WHERE i.ticket_id=dev_tickets.id) AS incident_id'
        limit, offset = args.get("limit", 50), args.get("offset", 0)
        items = [dict(row) for row in uow.connection.execute(
            f"SELECT {columns} FROM dev_tickets{where} ORDER BY created_at,id LIMIT ? OFFSET ?",
            [*params, limit + 1, offset])]
        return {"items": items[:limit], "next_offset": offset + limit if len(items) > limit else None}

    def agent_list(self, ctx, args, opts, uow):
        if ctx.principal_kind != "agent":
            raise DomainError("forbidden", "System Dev agent tools require an authenticated agent")
        status = args.get("status", "open")
        requested_scope = args.get("scope", "business" if ctx.business_id is not None else "global")
        clauses, params = [], []
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        role = self.role(ctx)
        if role in roles.BUSINESS_ROLES:
            if ctx.business_id is None or requested_scope != "business":
                raise DomainError("scope_required", "Business agent System Dev reads require its business scope")
            if not self.in_scope(ctx, {"business_id": ctx.business_id}, uow):
                raise DomainError("forbidden", "System Dev report is outside your authorized scope")
            clauses.append("business_id=?")
            params.append(ctx.business_id)
        elif role == "ordubashy":
            if requested_scope == "business":
                if ctx.business_id is None or ctx.business_id not in ctx.allowed_business_ids:
                    raise DomainError("scope_required", "Ordubashy business reads require an authorized business scope")
                if not uow.connection.execute(
                        "SELECT 1 FROM business_memberships WHERE business_id=? AND principal_id=? AND active=1",
                        (ctx.business_id, ctx.principal_id)).fetchone():
                    raise DomainError("forbidden", "System Dev report is outside your authorized scope")
                scopes = {ctx.business_id}
            else:
                if ctx.business_id is not None:
                    raise DomainError("scope_required", "Ordubashy Global reads require Global scope")
                active = {row[0] for row in uow.connection.execute(
                    "SELECT business_id FROM business_memberships WHERE principal_id=? AND active=1", (ctx.principal_id,))}
                scopes = set(ctx.allowed_business_ids) & active
            global_scope = "read_global_context" in ctx.global_capabilities
            scoped = ["business_id IN (" + ",".join("?" for _ in scopes) + ")"] if scopes else []
            if requested_scope == "global" and global_scope:
                scoped.append("business_id IS NULL")
            clauses.append("(" + " OR ".join(scoped or ["0"]) + ")")
            params.extend(sorted(scopes))
        else:
            if ctx.business_id is not None and not self.in_scope(ctx, {"business_id": ctx.business_id}, uow):
                raise DomainError("forbidden", "System Dev report is outside your authorized scope")
            if ctx.business_id is None:
                clauses.append("business_id IS NULL")
            else:
                clauses.append('business_id=?')
                params.append(ctx.business_id)
            # Include explicitly linked system incidents for this business.
            clauses.append("(reporter_id=? OR id IN (SELECT i.ticket_id FROM system_incidents i JOIN system_incident_impacts x ON x.incident_id=i.id WHERE x.business_id IS ? AND (x.business_id IS NOT NULL OR x.agent_id=?)))")
            params.extend((ctx.principal_id, ctx.business_id, ctx.principal_id))
        where = " WHERE " + " AND ".join(clauses)
        columns = "*" if args.get("details", False) else "id,title,kind,status,reporter_id,business_id,created_at,updated_at,revision"
        columns += ',(SELECT i.id FROM system_incidents i WHERE i.ticket_id=dev_tickets.id) AS incident_id'
        limit, offset = args.get("limit", 50), args.get("offset", 0)
        rows = [dict(row) for row in uow.connection.execute(
            f"SELECT {columns} FROM dev_tickets{where} ORDER BY created_at,id LIMIT ? OFFSET ?",
            [*params, limit + 1, offset])]
        return {"items": rows[:limit], "next_offset": offset + limit if len(rows) > limit else None}

    def get(self, ctx, args, opts, uow):
        ticket = self.ticket(uow, args["ticket_id"])
        self.agent_access(ctx, ticket, uow, read_only=True)
        events, next_offset = self.event_rows(uow, ticket["id"], limit=args.get("history_limit", 50),
                                              offset=args.get("history_offset", 0))
        return {"ticket": ticket, "events": events, "events_next_offset": next_offset}

    def prepare_create(self, ctx, args, opts, uow):
        business = ctx.business_id or ctx.home_business_id
        namespace = "business:" + business if business else "agent:" + ctx.agent_id
        scope = {"kind": "business", "business_id": business} if business else {"kind": "global"}
        builder = PlanBuilder(ctx, "dev_tickets_create", args, "System Dev report", namespace=namespace,
                              affected_scope=scope)
        identity = new_id()
        values = {"id": identity, "title": args["title"].strip(), "details": args.get("details", ""),
                  "kind": args.get("kind", "bug"), "status": "open", "reporter_id": ctx.principal_id,
                  "business_id": ctx.business_id, "created_at": builder.at, "updated_at": builder.at}
        builder.record("dev_tickets", identity, None, values, business_id=None, namespace=namespace)
        return builder.build()

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

    def _prepare_agent_change(self, ctx, args, uow, operation, title, values):
        ticket = self.ticket(uow, args["ticket_id"])
        self.agent_access(ctx, ticket, uow)
        namespace = self.agent_namespace(ctx, args)
        builder = PlanBuilder(ctx, operation, args, title, namespace=namespace,
                              affected_scope={"kind": "business", "business_id": ticket["business_id"]}
                              if ticket["business_id"] else {"kind": "global"})
        builder.record("dev_tickets", ticket["id"], ticket, values, business_id=None, namespace=namespace)
        return builder.build()

    def prepare_follow_up(self, ctx, args, opts, uow):
        ticket = self.ticket(uow, args["ticket_id"])
        self.agent_access(ctx, ticket, uow)
        if ticket["status"] != "open":
            raise DomainError("ticket_closed", "Follow-up requires an open System Dev report")
        details = args["details"].strip()
        if not details:
            raise DomainError("validation_error", "Follow-up details cannot be blank")
        return self._prepare_agent_change(ctx, args, uow, "dev_tickets_follow_up", "System Dev follow-up",
                                          {"updated_at": timestamp(self.clock)})

    def prepare_resolve(self, ctx, args, opts, uow):
        ticket = self.ticket(uow, args["ticket_id"])
        self.agent_access(ctx, ticket, uow)
        if ticket["status"] != "open":
            raise DomainError("ticket_closed", "System Dev report is already closed")
        resolution, verification = args["resolution"].strip(), args["verification"].strip()
        if not resolution or not verification:
            raise DomainError("validation_error", "Resolution and verification evidence cannot be blank")
        now = timestamp(self.clock)
        return self._prepare_agent_change(ctx, args, uow, "dev_tickets_resolve", "Resolve System Dev report",
                                          {"status": "closed", "resolved_at": now,
                                           "resolved_by": ctx.principal_id, "resolution": resolution,
                                           "updated_at": now})

    def validate_owner(self, ctx, plan, uow):
        self.owner(ctx, uow)

    def validate_agent_plan(self, ctx, plan, uow):
        ticket = self.ticket(uow, plan.arguments["ticket_id"])
        self.agent_access(ctx, ticket, uow)

    def apply_create(self, ctx, plan, uow):
        ticket = apply_records(ctx, plan, uow)[0]
        return {"ticket_id": ticket["id"], "status": ticket["status"], "revision": ticket["revision"], "queue": "System Dev"}

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

    def apply_follow_up(self, ctx, plan, uow):
        before = self.ticket(uow, plan.arguments["ticket_id"])
        self.agent_access(ctx, before, uow)
        if before["status"] != "open":
            raise DomainError("ticket_closed", "Follow-up requires an open System Dev report")
        ticket = apply_records(ctx, plan, uow)[0]
        self.insert_event(uow, ticket["id"], "follow_up", ctx.principal_id, details=plan.arguments["details"].strip(),
                          created_at=ticket["updated_at"])
        return {"ticket_id": ticket["id"], "status": ticket["status"], "revision": ticket["revision"],
                "event": "follow_up"}

    def apply_resolve(self, ctx, plan, uow):
        before = self.ticket(uow, plan.arguments["ticket_id"])
        self.agent_access(ctx, before, uow)
        if before["status"] != "open":
            raise DomainError("ticket_closed", "System Dev report is already closed")
        ticket = apply_records(ctx, plan, uow)[0]
        args = plan.arguments
        self.insert_event(uow, ticket["id"], "resolved", ctx.principal_id, resolution=args["resolution"].strip(),
                          verification=args["verification"].strip(), deployment_ref=args.get("deployment_ref", "").strip(),
                          created_at=ticket["resolved_at"])
        self.notify_reporter(uow, ticket, ctx, args)
        from bazarlyk.incidents import IncidentReporter
        IncidentReporter.ticket_status(uow,ticket,ctx.principal_id,verified=True)
        return {"ticket_id": ticket["id"], "status": ticket["status"], "revision": ticket["revision"],
                "event": "resolved", "resolved_at": ticket["resolved_at"], "resolved_by": ticket["resolved_by"]}

    def notify_reporter(self, uow, ticket, ctx, args):
        reporter = uow.repository("principals").get(ticket["reporter_id"])
        profile = uow.connection.execute("SELECT home_business_id,profile_json FROM agent_profiles WHERE agent_id=? AND status='active'",
                                         (ticket["reporter_id"],)).fetchone()
        if not reporter or not reporter["active"] or reporter["kind"] != "agent" or not profile or reporter["id"] == ctx.principal_id:
            return
        if ticket["business_id"] is None:
            scope = json.loads(profile["profile_json"]).get("scope", {})
            if scope.get("kind") != "ecosystem" or "read_global_context" not in scope.get("global_capabilities", ()):
                return
        else:
            scope = json.loads(profile["profile_json"]).get("scope", {})
            if (ticket["business_id"] != profile["home_business_id"]
                    and ticket["business_id"] not in scope.get("allowed_business_ids", ())):
                return
            if not uow.connection.execute(
                    "SELECT 1 FROM business_memberships WHERE business_id=? AND principal_id=? AND active=1",
                    (ticket["business_id"], ticket["reporter_id"])).fetchone():
                return
        now = timestamp(self.clock)
        identity = new_id()
        summary = "System Dev resolved: " + ticket["title"]
        event_id = uow.connection.execute(
            "INSERT INTO inbox_events(business_id,source_type,source_id,source_revision,actor_id,event_type,summary,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)", (ticket["business_id"], "agent_messages", identity, ticket["revision"],
                                           ctx.principal_id, "dev_ticket_resolved", summary[:500], now)).lastrowid
        body = "System Dev report resolved.\nResolution: " + args["resolution"].strip() + "\nVerification: " + args["verification"].strip()
        if args.get("deployment_ref", "").strip():
            body += "\nDeployment: " + args["deployment_ref"].strip()
        uow.connection.execute(
            "INSERT INTO agent_messages(id,event_id,business_id,sender_id,recipient_id,reply_to,thread_id,body,created_at,current_sender_id,current_recipient_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (identity, event_id, ticket["business_id"], ctx.principal_id, ticket["reporter_id"],
                                           None, identity, body, now, ctx.principal_id, ticket["reporter_id"]))
        uow.connection.execute("INSERT INTO inbox_deliveries(agent_id,event_id) VALUES(?,?)",
                               (ticket["reporter_id"], event_id))
