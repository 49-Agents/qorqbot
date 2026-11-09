"""Existing dashboard response shapes, backed exclusively by typed operations."""

from dataclasses import replace
from datetime import datetime, timezone, timedelta
import base64
from email import policy as email_policy
from email.parser import BytesParser
import json
from pathlib import Path, PurePosixPath
import re

import anyio
from markdown_it import MarkdownIt
from starlette.responses import JSONResponse, Response
import yaml

from qorqut.composition.locations import business_hq
from qorqut_operations.contracts import DomainError, InvocationOptions, canonical_hash, new_id, plain
from qorqut_operations.team_channel import is_installation_owner
from qorqut_operations.storage.repositories import decode_row
from .http import check_body_length, checked_fields, read_json, revalidated
from .results import response
from .routine_views import recent_runs, run_view, hourly_runs

OPTIONS = {"expected_revision", "idempotency_key", "supersedes_signoff_id", "expected_superseded_status_revision"}
# What the dashboard draws for an agent whose profile chose no icon or color.
DEFAULT_AGENT_ICON, DEFAULT_AGENT_COLOR = "bot", "#949BA5"


def integer_parameter(value, name, minimum, maximum):
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise DomainError("validation_error", name + " must be an integer") from None
    if isinstance(value, bool) or not minimum <= result <= maximum:
        raise DomainError("validation_error", f"{name} must be {minimum}-{maximum}")
    return result


class DashboardCompatibility:
    def __init__(self, runtime, *, terminals=None, background=None, local_files=None):
        self.runtime, self.terminals, self.background = runtime, terminals, background
        if local_files is None:
            from qorqut.service.local import LocalFiles
            local_files = LocalFiles(runtime)
        self.local_files = local_files

    def invoke(self, context, operation, arguments=None, options=None):
        result = self.runtime.application.invoke(context, operation, arguments or {}, options)
        if result.status == "error":
            error = plain(result.error)
            raise DomainError(error.pop("code"), error.pop("message"), **error)
        return result

    def data(self, context, operation, arguments=None):
        return plain(self.invoke(context, operation, arguments).data)

    def collection(self, context, operation, arguments=None, *, key="items"):
        args, found = {"limit": 200, **(arguments or {})}, []
        for _ in range(100):
            page = self.data(context, operation, args)
            found.extend(page[key])
            if not page.get("next_cursor"):
                return found
            args["cursor"] = page["next_cursor"]
        raise DomainError("collection_too_large", "Use the paginated operation endpoint for this collection")

    # The retained history routes page like the operations do; each request is one bounded page and the browser
    # follows the cursor itself, so this router never loops over the archive.
    LEARNING_HISTORY_LIMIT = 50
    LEARNING_CURSOR_MAX = 2000

    def learning_page(self, context, operation, arguments):
        """A bounded archive page is served directly: its own completeness fields are the contract, not a result envelope."""
        return plain(self.invoke(context, operation, arguments).data)

    @staticmethod
    def learning_page_tag(request, context, data):
        """The conditional tag proves the *authorized current* page, not a version counter.

        These routes read scope-authorized historical source content; a learning-only change counter cannot
        prove that visibility is unchanged (a revoked grant, access change or redacted source can return the
        same counter). The tag hashes the bytes the current read just produced with the scope, path and
        normalized query selection.
        """
        return 'W/"' + canonical_hash({"path": request.url.path, "query": sorted(request.query_params.items()),
            "scope": context.business_id, "data": data}) + '"'

    def learning_revalidated(self, request, context, operation, arguments):
        """Recheck authority and selected content on every request, then decide a conditional 304.

        Unlike the ordinary `revalidated` helper this never skips the operation: the domain read is what
        rechecks authority and produces the current (possibly redacted) page. A matching If-None-Match still
        returns 304 only after that same authorized read succeeds and its bytes equal the earlier page.
        """
        data = self.learning_page(context, operation, arguments)
        tag = self.learning_page_tag(request, context, data)
        headers = {"ETag": tag, "Cache-Control": "no-store"}
        if request.headers.get("if-none-match") == tag:
            return Response(status_code=304, headers=headers)
        return JSONResponse(data, headers=headers)

    @classmethod
    def learning_cursor(cls, request, name):
        value = request.query_params.get(name)
        if value is None:
            return None
        if not value or len(value) > cls.LEARNING_CURSOR_MAX:
            raise DomainError("validation_error", name + " must be a non-empty cursor under 2,000 characters")
        return value

    def learning_history_arguments(self, request):
        limit = request.query_params.get("limit")
        cursor = self.learning_cursor(request, "cursor")
        return {"limit": self.LEARNING_HISTORY_LIMIT if limit in (None, "") else integer_parameter(limit, "limit", 1, 200),
                **({"cursor": cursor} if cursor is not None else {})}

    def learning_review_arguments(self, request, learning_id):
        arguments = {"learning_id": learning_id}
        for name in ("content_cursor", "evidence_cursor", "history_cursor"):
            cursor = self.learning_cursor(request, name)
            if cursor is not None:
                arguments[name] = cursor
        return arguments

    @staticmethod
    def options(body, *, revision=None):
        values = {key: value for key, value in body.items() if key in OPTIONS}
        values.setdefault("idempotency_key", new_id())
        if revision is not None:
            values.setdefault("expected_revision", revision)
        return InvocationOptions.from_mapping(values)

    @staticmethod
    def appearance(profile):
        presentation = profile.get("presentation", {})
        return {"icon": presentation.get("icon", DEFAULT_AGENT_ICON), "color": presentation.get("color", DEFAULT_AGENT_COLOR),
                "kind": profile.get("role", {}).get("id", "")}

    def registered_agent(self, context, selector, *, include_inactive=False):
        with self.runtime.database.read() as uow:
            row = uow.connection.execute("SELECT agent_id FROM agent_profiles WHERE home_business_id=? AND (agent_id=? OR name=?) AND (status='active' OR ?)", (context.business_id, selector, selector, include_inactive)).fetchone()
        if row is None:
            raise DomainError("not_found", "Active registered agent was not found in this project")
        return self.data(context, "agents_get", {"agent_id": row["agent_id"]})["agent"]

    def agent_view(self, context, profile):
        if profile.get("archival_record"):
            return profile
        plan = self.data(context, "plans_get", {"agent_id": profile["agent_id"]})["document"] or {}
        notes = self.data(context, "notes_list", {"agent_id": profile["agent_id"], "limit": 30})["items"]
        return {**profile, **self.appearance(profile), "agent_yaml": yaml.safe_dump({"generated": True, **profile}, sort_keys=False),
                "plan": plan.get("body", ""), "notes": [Path(note["path"]).name for note in notes],
                "sessions": self.local_files.session_names(context.business_id, profile["agent_id"]), "learnings": ""}

    def assignees(self, context):
        return self.data(context, "assignees_list")["assignees"]

    def assignee_id(self, context, value):
        if value in (None, ""):
            return None
        candidates = self.assignees(context)
        matches = [row for row in candidates if value == row["id"] or value == row["kind"] + ":" + (row.get("name") or row["display_name"])]
        if len(matches) != 1:
            raise DomainError("invalid_assignee", "Select one active member of this project")
        return matches[0]["id"]

    def task_view(self, context, task, *, members=None):
        members = members if members is not None else {row["id"]: row for row in self.assignees(context)}
        assignees = [members[identity] for identity in task.get("assignee_ids", []) if identity in members]
        owners = [assignee["kind"] + ":" + (assignee.get("name") or assignee["display_name"]) for assignee in assignees]
        creator = members.get(task.get("created_by"))
        return {**task, "created_by_name": (creator.get("name") or creator.get("display_name")) if creator else "",
            "created_by_kind": creator.get("kind") if creator else "", "owners": owners, "owner": owners[0] if owners else "", "created": task.get("created_at"), "updated": task.get("updated_at"),
            "notes": [{**note, "at": note.get("created_at"), "by": note.get("created_by"), "text": note.get("text", note.get("body", ""))} for note in task.get("notes", [])]}

    async def global_route(self, request, context, parts):
        if len(parts) == 4 and parts[:2] == ['root', 'context-library'] and parts[3] in ('retry', 'retract') and request.method == 'POST':
            body = await read_json(request)
            # Both operations take exactly {upload_id}; the revision precondition and the idempotency key are invocation options.
            # An unsupported field is refused, never reinterpreted (retraction has no force).
            checked_fields(body, {'expected_revision', 'idempotency_key'})
            options = self.options(body)
            operation = 'context_retry' if parts[3] == 'retry' else 'context_retract'
            return await anyio.to_thread.run_sync(lambda: response(self.invoke(context, operation, {'upload_id': parts[2]}, options)))
        if parts == ['root', 'context'] and request.method == 'GET':
            return await anyio.to_thread.run_sync(lambda: self.global_context_get(request, context))
        if parts[:2] == ['root', 'context-library'] and request.method == 'GET':
            if len(parts) == 2:
                args = {'scope': 'shared', 'limit': 50, 'kind': request.query_params.get('kind', 'context_note')}
                if request.query_params.get('cursor'):
                    args['cursor'] = request.query_params['cursor']
                return await anyio.to_thread.run_sync(lambda: response(self.invoke(context, 'context_list', args)))
            if len(parts) == 3:
                args = {'scope': 'shared', 'document_id': parts[2], 'max_chars': 12000}
                if request.query_params.get('cursor'):
                    args['cursor'] = request.query_params['cursor']
                return await anyio.to_thread.run_sync(lambda: response(self.invoke(context, 'context_get', args)))
        if parts == ["root", "context", "upload"] and request.method == "POST":
            body = await read_json(request)
            args = self.context_upload_args(body)
            return await anyio.to_thread.run_sync(lambda: self.context_upload_response(self.invoke(context, "context_upload", args, self.options(body))))
        if parts == ["root", "context", "people"] and request.method == "POST":
            body = await read_json(request)
            checked_fields(body, {"query", "limit"} | OPTIONS)
            args = {key: body[key] for key in ("query", "limit") if key in body}
            return await anyio.to_thread.run_sync(lambda: response(self.invoke(context, "context_people_list", args, self.options(body))))
        if parts[:2] in (["root", "learnings"], ["root", "notes"]):
            tail = parts[1:]
            if request.method == "POST":
                body = await read_json(request)
                return await anyio.to_thread.run_sync(lambda: self.business_post(request, context, None, tail, body))
            if request.method == "GET":
                return await anyio.to_thread.run_sync(lambda: self.business_get(request, context, None, tail))
            raise DomainError("not_found", "Endpoint is not registered")
        if parts == ["root", "env"]:
            if request.method == "GET":
                data = await anyio.to_thread.run_sync(lambda: self.local_files.environment_settings(context))
            elif request.method == "POST":
                body = await read_json(request)
                checked_fields(body, {"pairs"}, {"pairs"})
                data = await anyio.to_thread.run_sync(lambda: self.local_files.replace_environment(context, body["pairs"]))
            else:
                raise DomainError("not_found", "Endpoint is not registered")
            return JSONResponse(data, headers={"Cache-Control": "no-store"})
        if parts[:1] == ["routines"] or parts[:2] == ["root", "routines"] or parts in (["filer"], ["bridge", "status"]):
            body = await read_json(request) if request.method == "POST" else {}
            return await anyio.to_thread.run_sync(lambda: self.background_global(request, context, parts, body))
        return await anyio.to_thread.run_sync(lambda: self._global_get(request, context, parts))

    def _global_get(self, request, context, parts):
        if request.method != "GET":
            raise DomainError("not_found", "Endpoint is not registered")
        if parts == ["me", "tasks"]:
            if context.principal_kind != "human":
                raise DomainError("forbidden", "Personal tasks require a human principal")
            with self.runtime.database.read() as uow:
                businesses = [dict(row) for row in uow.connection.execute(
                    "SELECT b.id,b.slug,b.name FROM businesses b JOIN business_memberships m ON m.business_id=b.id "
                    "WHERE m.principal_id=? AND m.active=1 AND b.archived_at IS NULL ORDER BY b.slug", (context.principal_id,))
                    if row["id"] in context.allowed_business_ids]
            tasks = []
            for business in businesses:
                selected = context.select_business(business["id"])
                rows = self.collection(selected, "tasks_list", {"assignee_id": context.principal_id, "include_closed": True, "snooze_filter": "all"}, key="tasks")
                members = {row["id"]: row for row in self.assignees(selected)} if rows else {}
                tasks.extend({**self.task_view(selected, task, members=members), "business": business} for task in rows)
            return JSONResponse({"principal_id": context.principal_id, "tasks": tasks})
        if parts == ["biz"]:
            with self.runtime.database.read() as uow:
                registered = uow.connection.execute("SELECT b.* FROM businesses b JOIN business_memberships m ON m.business_id=b.id WHERE m.principal_id=? AND m.active=1 AND b.archived_at IS NULL ORDER BY b.slug", (context.principal_id,)).fetchall()
            businesses = []
            for row in registered:
                selected = context.select_business(row["id"])
                profiles = self.data(selected, "agents_list")["agents"]
                metadata = json.loads(row["config_json"])
                businesses.append({"slug": row["slug"], "path": str(business_hq(self.runtime.paths, row)), "name": row["name"], "id": row["id"], "business_id": row["id"],
                    "one_liner": metadata.get("one_liner", metadata.get("description", "")), "stage": row["stage"], "website": metadata.get("website"),
                    "owner": context.principal.display_name, "agents": [profile["name"] for profile in profiles],
                    "agent_meta": {profile["name"]: self.appearance(profile) for profile in profiles}})
            return JSONResponse(businesses)
        if parts == ["marketplace"]:
            if not context.allowed_business_ids:
                return JSONResponse({"v": 2, "kinds": []})
            selected = context.select_business(sorted(context.allowed_business_ids)[0])
            references = self.data(selected, "roles_list")["roles"]
            kinds = []
            for reference in references:
                role = self.data(selected, "roles_get", {"role": reference})["role"]
                if "business" not in role["manifest"]["scope_kinds"]:
                    continue
                display = self.appearance({"role": reference})
                kinds.append({"id": reference["id"], "role": reference, "label": role["manifest"].get("name", reference["id"]),
                    "description": role["brief"], "available": True, **display})
            return JSONResponse({"v": 2, "kinds": kinds})
        if parts == ["sessions"]:
            return JSONResponse([])
        raise DomainError("not_found", "Endpoint is not registered")

    async def business_route(self, request, context, business, tail):
        if request.method == "GET":
            return await anyio.to_thread.run_sync(lambda: self.business_get(request, context, business, tail))
        if request.method == "POST":
            if tail == ["assets", "upload"]:
                return await self.asset_upload(request, context)
            body = await read_json(request)
            return await anyio.to_thread.run_sync(lambda: self.business_post(request, context, business, tail, body))
        raise DomainError("not_found", "Endpoint is not registered")

    def business_get(self, request, context, business, tail):
        if tail == ["env"]:
            return JSONResponse(self.local_files.environment_settings(context), headers={"Cache-Control": "no-store"})
        if len(tail) == 3 and tail[0] == "session":
            profile = self.registered_agent(context, tail[1], include_inactive=True)
            return JSONResponse(self.local_files.session_events(context.business_id, profile["agent_id"], tail[2]))
        if tail == ["git"]:
            return JSONResponse(self.local_files.git_recent(context.business_id))
        if tail == ["agents"]:
            def produce():
                profiles = self.data(context, "agents_list", {"include_inactive": request.query_params.get("include_inactive") == "true"})["agents"]
                return JSONResponse([{**profile, **self.appearance(profile)} for profile in profiles] if request.query_params.get("summary") == "1"
                    else [self.agent_view(context, profile) for profile in profiles])
            # agent_view() also reads plans/notes (documents) per profile; summary
            # mode does not, but declaring the union for both is simpler and only
            # costs summary mode an occasional extra miss, never a stale one.
            return revalidated(request, self.runtime.changes, {"agents", "documents"}, context.business_id, produce)
        if len(tail) == 2 and tail[0] == "agents":
            return response(self.invoke(context, "agents_get", {"agent_id": tail[1]}))
        if len(tail) == 2 and tail[0] == "task":
            task = self.data(context, "tasks_get", {"task_id": tail[1]})["task"]
            return JSONResponse({"task": self.task_view(context, task)})
        if tail == ["tasks"]:
            def produce():
                closed = bool(request.query_params.get("all") or request.query_params.get("closed"))
                tasks = self.collection(context, "tasks_list", {"include_closed": closed,
                    "snooze_filter": request.query_params.get("snooze_filter", "all" if closed else "active")}, key="tasks")
                tasks = [task for task in tasks if not request.query_params.get("closed") or task["closed"]]
                members = {row["id"]: row for row in self.assignees(context)} if tasks else {}
                return JSONResponse([self.task_view(context, task, members=members) for task in tasks])
            # assignees_list (agents kind) only actually runs when tasks is
            # non-empty, but the declared kind set must cover every request
            # this branch could make, not just this one's.
            return revalidated(request, self.runtime.changes, {"tasks", "agents"}, context.business_id, produce)
        if tail == ["log"]:
            try:
                limit = int(request.query_params.get("n", "40"))
            except ValueError:
                raise DomainError("validation_error", "Log limit must be an integer") from None
            def produce():
                entries = self.collection(context, "actions_list", key="actions") if limit == 0 else self.data(context, "actions_list", {"limit": limit})["actions"]
                return JSONResponse([self.log_view(row) for row in entries])
            return revalidated(request, self.runtime.changes, {"action_logs"}, context.business_id, produce)
        if tail == ["live"]:
            def produce():
                platform, since = self.live_platform_signoffs(context, request.query_params.get("since") or None)
                signoffs = self.data(context, "signoffs_list", {"limit": 1})
                learnings = self.data(context, "learnings_list", {"state": "open", "limit": 1})
                return JSONResponse({"platform_signoffs": platform, "since": since,
                    "learnings_open": learnings["counts"]["open"],
                    "routine_failures": self.live_routine_failures(context, datetime.now(timezone.utc)),
                    "signoffs_pending": signoffs["counts"]["pending"]})
            return revalidated(request, self.runtime.changes, {"signoffs", "learnings", "jobs_routines"}, context.business_id, produce)
        if tail[:1] == ["routines"]:
            return self.routine_get(request, context, tail[1:])
        if tail == ["learnings"]:
            def produce():
                items = self.collection(context, "learnings_list")
                from qorqut.domains.learnings.shared import STATES
                counts = {state: sum(row["state"] == state for row in items) for state in STATES}
                return JSONResponse({"items": items, "counts": counts})
            return revalidated(request, self.runtime.changes, {"learnings"}, context.business_id, produce)
        # The literal history inventory is matched before the generic candidate-ID route: "history" is not an ID.
        if tail == ["learnings", "history"]:
            args = self.learning_history_arguments(request)
            return self.learning_revalidated(request, context, "learnings_legacy_pending", args)
        if len(tail) == 3 and tail[0] == "learnings" and tail[2] == "review":
            args = self.learning_review_arguments(request, tail[1])
            return self.learning_revalidated(request, context, "learnings_review_get", args)
        if len(tail) == 2 and tail[0] == "learnings":
            return response(self.invoke(context, "learnings_get", {"learning_id": tail[1]}))
        if len(tail) == 3 and tail[0] == "learnings" and tail[2] == "intake":
            return response(self.invoke(context, "learnings_intake_status", {"learning_id": tail[1]}))
        if len(tail) == 2 and tail[0] == "jobs":
            return response(self.invoke(context, "jobs_get", {"job_id": tail[1]}))
        return self.document_get(request, context, business, tail)

    def business_post(self, request, context, business, tail, body):
        if tail == ["env"]:
            checked_fields(body, {"pairs"}, {"pairs"})
            return JSONResponse({"ok": True, **self.local_files.replace_environment(context, body["pairs"])}, headers={"Cache-Control": "no-store"})
        if tail[:1] == ["routines"]:
            return self.routine_post(context, tail[1:], body)
        if len(tail) == 4 and tail[0] == "learnings" and tail[2:] == ["intake", "retry"]:
            checked_fields(body, set())
            result = self.runtime.background.learnings.resume_intake(context, tail[1])
            return response(result) if hasattr(result, "status") else JSONResponse({"ok": True, **plain(result)})
        if len(tail) == 3 and tail[0] == "learnings" and tail[2] in {"state", "edit", "keep-both"}:
            before = self.data(context, "learnings_get", {"learning_id": tail[1]})["item"]
            if tail[2] == 'keep-both':
                checked_fields(body, OPTIONS)
                args = {'learning_id':tail[1]}
                operation = 'learnings_keep_both'
            elif tail[2] == "state":
                checked_fields(body, {"state", "note"} | OPTIONS, {"state"})
                args = {"learning_id": tail[1], "state": body["state"]}
                if "note" in body:
                    args["feedback"] = body["note"]
                operation = "learnings_review"
            else:
                checked_fields(body, {"text", "tag", "scope", "bucket"} | OPTIONS)
                args = {"learning_id": tail[1], "changes": {key: value for key, value in body.items() if key not in OPTIONS}}
                operation = "learnings_edit"
            result = self.invoke(context, operation, args, self.options(body, revision=before["revision"]))
            return response(result) if result.status != "ok" else JSONResponse({"ok": True, **plain(result.data)})
        if tail == ["task"]:
            checked_fields(body, {"title", "owner", "owners", "status", "due", "tags", "note", "blocked_by", "fields", "task_type", "overview", "email_conversation_id",
                                  "customer_kind", "customer_state", "customer_profile"} | OPTIONS, {"title"})
            args = {key: value for key, value in body.items() if key not in OPTIONS and key not in {"owner", "owners"}}
            args["assignee_ids"] = [self.assignee_id(context, value) for value in body.get("owners", [body["owner"]] if body.get("owner") else [])]
            if args.get("due") == "":
                args["due"] = None
            result = self.invoke(context, "tasks_create", args, self.options(body))
            return response(result) if result.status != "ok" else JSONResponse({"task": self.task_view(context, plain(result.data["task"]))}, status_code=201)
        if len(tail) in (2, 3) and tail[0] == "task":
            current = self.data(context, "tasks_get", {"task_id": tail[1]})["task"]
            if len(tail) == 3 and tail[2] == "done":
                checked_fields(body, {"note"} | OPTIONS)
                args = {"task_id": tail[1], **({"note": body["note"]} if "note" in body else {})}
                operation = "tasks_complete"
            elif len(tail) == 2:
                checked_fields(body, {"set", "note"} | OPTIONS)
                changes = dict(body.get("set", {}))
                if "owners" in changes:
                    changes["assignee_ids"] = [self.assignee_id(context, value) for value in changes.pop("owners")]
                    changes.pop("owner", None)
                elif "owner" in changes:
                    owner = changes.pop("owner")
                    changes["assignee_ids"] = [self.assignee_id(context, owner)] if owner else []
                if changes.get("due") == "":
                    changes["due"] = None
                if not changes:
                    operation, args = "tasks_comment", {"task_id": tail[1], "text": body.get("note", "")}
                else:
                    operation, args = "tasks_update", {"task_id": tail[1], "changes": changes, **({"note": body["note"]} if "note" in body else {})}
            else:
                raise DomainError("not_found", "Task endpoint is not registered")
            result = self.invoke(context, operation, args, self.options(body, revision=current["revision"]))
            return response(result) if result.status != "ok" else JSONResponse({"task": self.task_view(context, plain(result.data["task"]))})
        if tail == ["agent"]:
            checked_fields(body, {"name", "display_name", "role", "assignment_brief", "kind", "plugins", "presentation"} | OPTIONS,
                {"role"})
            # A business agent's name is allocated; agents_create rejects a chosen one.
            args = {key: body[key] for key in ("name", "display_name", "role", "assignment_brief", "plugins", "presentation") if key in body}
            return response(self.invoke(context, "agents_create", args, self.options(body)), created=True)
        if len(tail) == 3 and tail[0] == "agent" and tail[2] == "meta":
            checked_fields(body, {"icon"} | OPTIONS)
            profile = self.registered_agent(context, tail[1])
            presentation = {**profile["presentation"], **{key: body[key] for key in ("icon",) if key in body}}
            result = self.invoke(context, "agents_update", {"agent_id": profile["agent_id"], "changes": {"presentation": presentation}}, self.options(body, revision=profile["profile_revision"]))
            return response(result) if result.status != "ok" else JSONResponse({"ok": True, **self.appearance({**profile, "presentation": presentation})})
        return self.document_post(request, context, business, tail, body)

    def page_view(self, document, uow=None, context=None):
        """A note or journal entry. Its ``filing`` (and each journal submission's) is the one current-source projection of the
        upload it references, taken from the document operations; ``context`` is the caller (it decides error redaction)."""
        from qorqut_operations.documents.note_snapshot import snapshot_hash
        from qorqut_operations.documents.journal_access import JOURNAL_KINDS
        meta = document["metadata"]
        documents = self.runtime.domains.documents
        def resolve_filing(uow):
            upload = uow.repository("documents", business_id=document["business_id"]).get(meta["upload_id"])
            return documents.source_filing(uow, context, upload) if upload and upload["kind"] in {"context_upload", "context_dump"} else None
        filing = None
        if meta.get("upload_id"):
            if uow is not None:
                filing = resolve_filing(uow)
            else:
                with self.runtime.database.read() as uow:
                    filing = resolve_filing(uow)
        def submissions(uow):
            if document.get("kind") not in JOURNAL_KINDS:
                return []
            found = uow.connection.execute(
                "SELECT * FROM documents WHERE kind='context_upload' "
                "AND json_extract(metadata_json, '$.journal_source_id')=? ORDER BY created_at, id",
                (document["id"],)).fetchall()
            result = []
            for saved in found:
                upload = decode_row(saved)
                upload_meta = upload["metadata_json"]
                result.append({"uploadId": upload["id"], "sourceRevision": upload_meta.get("journal_source_revision"),
                    "sourceHash": upload_meta.get("journal_source_hash"), "sourceDate": upload_meta.get("journal_source_date"),
                    "createdAt": upload_meta.get("journal_uploaded_at"),
                    "changedSinceSubmission": (upload_meta.get("journal_source_revision") != document.get("revision")
                        or upload_meta.get("journal_source_hash") != document.get("content_hash")),
                    "filing": documents.source_filing(uow, context, upload)})
            return result

        if uow is not None:
            submissions_for_note = submissions(uow)
        else:
            with self.runtime.database.read() as uow:
                submissions_for_note = submissions(uow)
        return {**{key: value for key, value in document.items() if key != "body"}, "filing": filing,
            "filerSubmissions": submissions_for_note,
            "changedSinceUpload": bool(meta.get("upload_id")) and meta.get("upload_snapshot_hash") != snapshot_hash(document.get("body", ""), meta),
            "title": meta.get("title", ""), "content": document.get("body", ""),
            "ordubashyVisible": meta.get("ordubashy_visible", True) is True,
            "tags": list(meta.get("tags", [])), "authorId": meta.get("author_id", ""),
            "date": meta.get("date", document.get("created_at", "")[:10]), "pinned": meta.get("pinned") in (True, "true"),
            "statuses": meta.get("statuses", {}), "createdAt": meta.get("created", document.get("created_at", "")),
            "updatedAt": meta.get("updated", document.get("updated_at", "")),
            "uploaded": meta.get("uploaded", ""), "uploadId": meta.get("upload_id", "")}

    @staticmethod
    def project_target(context, supplied):
        """The explicit Project UUID a Project-note request carries, forwarded unchanged. The route slug only selects the route; the
        operation compares this value with the selected Project and the stored note owner. A Project route without it is refused."""
        if supplied is not None:
            return {"target_business_id": supplied}
        if context.business_id is not None and context.agent_id is None:
            raise DomainError("validation_error", "Project notes require target_business_id")
        return {}

    def document_selector(self, context, selector, operation="context_list", *, prefix=None, kinds=None, arguments=None):
        if not isinstance(selector, str) or not selector or "\\" in selector or ".." in PurePosixPath(selector).parts or selector.startswith("/"):
            raise DomainError("validation_error", "A registered document selector is required")
        path = prefix + "/" + selector if prefix else selector
        documents = self.collection(context, operation, arguments)
        matches = [row for row in documents if (kinds is None or row["kind"] in kinds) and
            (row["id"] == selector or row["path"] == path or (prefix is None and PurePosixPath(row["path"]).stem == selector))]
        if len(matches) != 1:
            raise DomainError("not_found", "Registered document was not found unambiguously in this project")
        return matches[0]

    # Stored document bookkeeping that older filers wrote beside the derived summary. The current-source ``filing`` is the evidence;
    # these never stand beside it in an upload's item.
    UPLOAD_BOOKKEEPING = ("context_base", "context_filing", "result", "error", "attempts", "last_attempt", "processed", "filed_by")

    def context_item(self, document):
        meta, path = document["metadata"], PurePosixPath(document["path"])
        if document["kind"] in {"context_upload", "context_dump"} and "filing" in document:
            meta = {**{key: value for key, value in meta.items() if key not in self.UPLOAD_BOOKKEEPING}, "filing": document["filing"]}
        relative = str(path.relative_to(path.parts[0]))
        kind = {"context_topic": "topic", "context_person": "person", "context_note": "note", "context_upload": "inbox",
            "context_dump": "dump", "context_map": "map", "pillar": "pillar", "context": "pillar"}.get(document["kind"], "note")
        return {**meta, "id": path.stem if kind in {"inbox", "dump"} else document["id"], "document_id": document["id"], "path": relative, "kind": kind,
            "title": meta.get("title", path.stem), "revision": document["revision"], "content_hash": document["content_hash"],
            "summary": meta.get("summary", ""), "text": document.get("body", ""), "chars": len(document.get("body", "")),
            "preview": " ".join(document.get("body", "").split())[:200], "at": meta.get("at", document.get("created_at", ""))}

    @staticmethod
    def map_view(document, upload):
        outline = document["metadata"].get("outline")
        if outline is None:
            root = {"text": document["metadata"].get("title", "Map"), "children": []}
            stack = [root]
            tokens = MarkdownIt("commonmark").parse(document.get("body", ""))
            for index, token in enumerate(tokens):
                if token.type == "heading_open" and token.tag == "h1" and len(stack) == 1:
                    root["text"] = tokens[index + 1].content
                elif token.type == "list_item_open":
                    node = {"text": "", "children": []}
                    stack[-1]["children"].append(node)
                    stack.append(node)
                elif token.type == "list_item_close":
                    stack.pop()
                elif token.type == "inline" and len(stack) > 1:
                    stack[-1]["text"] = (stack[-1]["text"] + " " + token.content).strip()
            outline = [root]
        def count(nodes):
            return sum(1 + count(node.get("children", [])) for node in nodes)
        return {**document["metadata"], "document_id": document["id"], "id": PurePosixPath(upload["path"]).stem,
            "at": document["metadata"].get("at", document["metadata"].get("created", document.get("created_at", ""))), "nodes": count(outline),
            "tree": outline[0] if len(outline) == 1 else {"text": document["metadata"].get("title", "Map"), "children": outline}}

    @staticmethod
    def person_notes(document):
        body = document.get("body", "")
        lines, tokens = body.splitlines(), MarkdownIt("commonmark").parse(body)
        headings = [(token, tokens[index + 1].content) for index, token in enumerate(tokens) if token.type == "heading_open"]
        summaries = []
        for index, (heading, title) in enumerate(headings):
            if heading.tag != "h2" or title not in {"Notes", "Summary"}:
                continue
            end = next((following.map[0] for following, _ in headings[index + 1:] if int(following.tag[1:]) <= 2), len(lines))
            if title == "Notes":
                return "\n".join(lines[heading.map[1]:end]).strip()
            summaries.append((heading.map[0], end))
        if document["metadata"].get("legacy_source"):
            return ""
        for start, end in reversed(summaries):
            del lines[start:end]
        return "\n".join(lines).strip()

    def context_queue(self, context):
        """The read-only ``context_queue`` object of the caller's namespace (the business, or Global when none is selected)."""
        from qorqut_context.integration.filing.queue_status import context_queue_summary
        with self.runtime.database.read() as uow:
            return context_queue_summary(uow.connection, context.business_id)

    def context_processes_view(self, context):
        """The processes view: only the namespace's Context queue summary, read from the queue's own rows.

        No count comes from generic job activity (a deferred Context job is not a model that is running), and no read initializes
        Context or starts work. Missing queue composition is an explicit error from the summary, not an empty answer."""
        return {"context_queue": self.context_queue(context)}

    def global_context_get(self, request, context):
        """``GET /api/root/context?view=processes``: the owner's read of Global-owned Context work (not a union of businesses)."""
        checked_fields(dict(request.query_params), {"view"}, {"view"})
        if request.query_params["view"] != "processes":
            raise DomainError("validation_error", "Only the processes view is available for Global Context")
        if context.business_id is not None:
            raise DomainError("scope_required", "The Global Context process view is read without a selected project")
        with self.runtime.database.read() as uow:
            if context.principal_kind != "human" or not is_installation_owner(context, uow):
                raise DomainError("forbidden", "Global Context process state is an owner view")
        return JSONResponse(self.context_processes_view(context), headers={"Cache-Control": "no-store"})

    def context_view(self, context):
        documents = self.collection(context, "context_list")
        nodes = {"": {"path": "", "slug": "", "children": []}}
        items = [self.context_item(row) for row in documents if row["path"].startswith("context/")]
        people = [{**item, "slug": PurePosixPath(item["path"]).stem, "name": item.get("name", item["title"]), "mentions": []}
            for item in items if item["kind"] == "person"]
        people_by_ref = {key: person for person in people for key in (person["document_id"], person["slug"])}
        for item in sorted(items, key=lambda row: (len(PurePosixPath(row["path"]).parts), row["path"])):
            if item["kind"] != "topic":
                continue
            path = str(PurePosixPath(item["path"]).parent)
            node = {**item, "path": path, "slug": PurePosixPath(path).name, "children": [], "notes": [], "titles": {}, "paths": {},
                "previews": {}, "summaries": {}, "also": {}, "people": {}, "from": {}, "meta": {}}
            nodes[path] = node
            nodes[""]["children"].append(node)
        topics_by_ref = {key: node["path"] for node in nodes.values() if node.get("document_id")
            for key in (node["document_id"], node["path"])}
        for node in nodes[""]["children"]:
            node["related_topics"] = [{key: value for key, value in child.items() if key != "related_topics"}
                for child in nodes[""]["children"] if node["document_id"] in child.get("parent_ids", [])]
        for item in items:
            if item["kind"] != "note":
                continue
            related = {people_by_ref[key]["slug"]: people_by_ref[key] for key in item.get("people", []) if key in people_by_ref}
            for person in related.values():
                person["mentions"].append(item["path"])
            name = item["document_id"]
            related_topics = list(dict.fromkeys(topics_by_ref[key]
                for key in [*item.get("also", []), *item.get("also_topic_ids", [])] if key in topics_by_ref))
            for topic_id in item.get("topic_ids", []):
                node = nodes.get(topics_by_ref.get(topic_id))
                if not node: continue
                node["notes"].append(name)
                for key, value in (("titles", item["title"]), ("paths", item["path"]), ("previews", " ".join(item["text"].split())[:200]),
                    ("summaries", item["summary"]), ("also", related_topics), ("people", list(related)),
                    ("from", item.get("from", "")), ("meta", item)):
                    node[key][name] = value
        by_id = {document["id"]: document for document in documents}
        maps = [self.map_view(document, by_id[document["metadata"]["upload_id"]]) for document in documents
            if document["kind"] == "context_map" and document["metadata"].get("upload_id") in by_id]
        mapped = {item["id"] for item in maps}
        result = {"tree": nodes[""], "pillars": [item["path"] for item in items if item["kind"] == "pillar"],
            "inbox": [item for item in items if item["kind"] == "inbox"],
            "dumps": [{**item, "map": item["id"] in mapped} for item in items if item["kind"] == "dump"], "maps": maps,
            "people": people}
        result["context_queue"] = self.context_queue(context)
        result["graph"] = self.data(context, "context_graph_get", {})
        return result

    def document_get(self, request, context, business, tail):
        if tail == ["notes"]:
            def produce():
                tag = request.query_params.get("tag")
                target = self.project_target(context, request.query_params.get("target_business_id"))
                rows = self.collection(context, tail[0] + "_list", {**({"tag": tag} if tag else {}), **target})
                with self.runtime.database.read() as uow:
                    return JSONResponse([self.page_view(row, uow, context) for row in rows])
            # page_view()'s filing status reads the Context queue's rows (coarse, global-only context_queue) and `jobs` directly
            # (coarse jobs_routines).
            return revalidated(request, self.runtime.changes, {"documents", "jobs_routines", "context_queue"}, context.business_id, produce)
        if tail == ["assets"]:
            groups = {"polished": [], "drafted": []}
            for row in self.collection(context, "assets_list"):
                meta = row["metadata"]
                groups.setdefault(meta.get("group", PurePosixPath(row["path"]).parts[1]), []).append({**row, **meta})
            return JSONResponse(groups)
        if len(tail) >= 2 and tail[0] == "asset":
            asset = self.document_selector(context, "/".join(tail[1:]), "assets_list")
            asset = self.data(context, "assets_get", {"asset_id": asset["id"]})["asset"]
            store = self.runtime.domains.documents.store
            with self.runtime.database.read() as uow:
                row = store.get(context, asset["id"], uow, kinds={"asset"})
                data = store.read_bytes(row, uow)
            media = asset["metadata"].get("media_type", "application/octet-stream")
            inline = media in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "video/mp4", "video/webm", "audio/mpeg", "audio/ogg"}
            return Response(data, media_type=media if inline else "application/octet-stream",
                headers={"Content-Disposition": ("inline" if inline else "attachment") + "; filename=\"" + PurePosixPath(asset["path"]).name + "\"", "Cache-Control": "private, no-store"})
        if tail == ["context"]:
            def produce():
                if request.query_params.get("view") == "graph":
                    return JSONResponse({"graph_only": True, "graph": self.data(context, "context_graph_get", {})})
                if request.query_params.get("view") == "processes":
                    return JSONResponse(self.context_processes_view(context))
                return JSONResponse(self.context_view(context))
            # One declared kind set for all three ?view= variants: graph/default
            # read context_list (documents) and the queue summary (coarse, global-only context_queue), processes/default read
            # `jobs` directly (coarse jobs_routines); the query string is already part
            # of the ETag's own identity, so the three variants never collide.
            return revalidated(request, self.runtime.changes, {"documents", "jobs_routines", "context_queue"}, context.business_id, produce)
        if tail == ["context", "file"]:
            doc = self.document_selector(context, request.query_params.get("p", ""), "context_list", prefix="context")
            return JSONResponse({"path": request.query_params["p"], "dir": "context", "text": doc["body"], "document_id": doc["id"], "revision": doc["revision"], "content_hash": doc["content_hash"],
                "material": {key: doc[key] for key in ("material_type", "origin", "tags") if key in doc},
                **({"filing": doc["filing"]} if "filing" in doc else {})})
        if tail == ["context", "search"]:
            query = request.query_params.get("q", "").strip()
            docs = self.collection(context, "context_search", {"query": query}) if len(query) >= 2 else []
            return JSONResponse({"q": query, "hits": [{**self.context_item(row), "snippet": row["body"][:180], "n": max(1, row["body"].casefold().count(query.casefold()))} for row in docs]})
        if len(tail) == 3 and tail[:2] == ["context", "person"]:
            doc = self.document_selector(context, tail[2], "context_people_list")
            return JSONResponse({**self.context_item(doc), "slug": PurePosixPath(doc["path"]).stem, "body": doc["body"], "notes_text": self.person_notes(doc)})
        if tail == ["context", "map"]:
            upload = self.document_selector(context, request.query_params.get("id", ""), kinds={"context_upload", "context_dump"})
            maps = self.collection(context, "context_maps_get", {"upload_id": upload["id"]})
            documents = {row["id"]: row for row in self.collection(context, "context_list")}
            identity = PurePosixPath(upload["path"]).stem
            filed = upload["metadata"].get("result")
            if not isinstance(filed, dict):
                filed = {"notes": [row["id"] for row in documents.values() if row["kind"] == "context_note" and
                    (row["metadata"].get("from") in {identity, upload["id"]} or any(
                        ref.get("resource_type") == "documents" and ref.get("resource_id") == upload["id"] for ref in row.get("source_refs", [])))]}
            notes = [self.context_item(documents[identity]) for identity in filed.get("notes", []) if identity in documents]
            topics = sorted({str(PurePosixPath(documents[key]["path"]).parent).removeprefix("context/") for key in filed.get("topics", []) if key in documents}
                | {str(PurePosixPath(note["path"]).parent) for note in notes})
            person_refs = {key: row for row in documents.values() if row["kind"] == "context_person" for key in (row["id"], PurePosixPath(row["path"]).stem)}
            people = {person_refs[key]["id"]: {"slug": PurePosixPath(person_refs[key]["path"]).stem, "name": person_refs[key]["metadata"].get("name", "")}
                for key in [*filed.get("people", []), *(key for note in notes for key in note.get("people", []))] if key in person_refs}
            current = None      # mapping is no longer a background job
            identity = PurePosixPath(upload["path"]).stem
            return JSONResponse({"id": identity, "document_id": upload["id"], "map": self.map_view(max(maps, key=lambda row: row["created_at"]), upload) if maps else None,
                "trace": {"id": identity, "document_id": upload["id"], "where": "_dumps" if upload["kind"] == "context_dump" else "_inbox",
                    "notes": notes, "topics": topics, "people": list(people.values()), "map": bool(maps)},
                "mapping": bool(current and current["state"] in {"queued", "running", "applying", "waiting_signoff"}),
                "error": (current.get("error_json") or {}).get("message", "") if current else ""})
        raise DomainError("not_found", "Endpoint is not registered")

    @staticmethod
    def context_upload_args(body):
        """The context_upload field mapping shared by the business and Global routes."""
        checked_fields(body, {"text", "notes", "topic", "task_ids", "origin", "occurred_on", "participants"} | OPTIONS, {"text"})
        args = {"body": body["text"], "title": body.get("topic") or body.get("notes") or "Context upload",
                "source": "dashboard", "task_ids": body.get("task_ids", [])}
        args.update({key: body[key] for key in ("origin", "occurred_on", "participants") if key in body})
        return args

    @staticmethod
    def context_upload_response(result):
        """The shared 202 shape for both context_upload routes; other statuses pass through unchanged."""
        if result.status != "accepted":
            return response(result)
        document = plain(result.data["document"])
        return JSONResponse({**result.to_dict(), "ok": True, "id": PurePosixPath(document["path"]).stem,
            "document_id": document["id"], "path": document["path"].removeprefix("context/")}, status_code=202)

    def document_post(self, request, context, business, tail, body):
        if tail[:1] == ["notes"]:
            domain = tail[0]
            checked_fields(body, {"title", "content", "pinned", "date", "statuses", "ordubashy_visible", "expected_content_hash", "task_ids", "tags",
                                  "target_business_id"} | OPTIONS)
            target = self.project_target(context, body.get("target_business_id"))
            if len(tail) == 1:
                args = {"title": body.get("title") or "Daily", "body": body.get("content", "")}
                args.update({key: body[key] for key in ("pinned", "date", "task_ids", "tags") if key in body})
                args.update(target)
                operation, revision = domain + "_create", None
            else:
                doc = self.document_selector(context, tail[1], domain + "_list", arguments=target)
                revision = doc["revision"]
                args = {"document_id": doc["id"], "expected_content_hash": body.get("expected_content_hash", doc["content_hash"]),
                        **(target if doc["kind"] == "business_owned_note" else {})}
                if len(tail) == 2:
                    operation = domain + "_update"
                    if "content" in body:
                        args["body"] = body["content"]
                    args["metadata"] = {key: body[key] for key in ("title", "pinned", "tags", "statuses", "ordubashy_visible") if key in body}
                elif len(tail) == 3 and tail[2] in {"delete", "upload"}:
                    operation = ("journal_upload_to_filer" if context.business_id is None and tail[2] == "upload"
                                 else domain + "_" + tail[2])
                else:
                    raise DomainError("not_found", "Document action is not registered")
            if "task_ids" in body and operation == "notes_upload":
                args["task_ids"] = body["task_ids"]
            elif "task_ids" in body and operation != "notes_create":
                raise DomainError("validation_error", "Use relationship operations to change associations on an existing note")
            result = self.invoke(context, operation, args, self.options(body, revision=revision))
            if result.status != "ok":
                return response(result)
            result_data = plain(result.data)
            if result_data.get("document"):
                doc = self.data(context, domain + "_get", {"document_id": result_data["document"]["id"]})["document"]
                return JSONResponse(self.page_view(doc, context=context))
            return JSONResponse({"ok": True, **result_data})
        if len(tail) == 2 and tail[0] == "context":
            action, revision = tail[1], None
            if action == "upload":
                operation, args = "context_upload", self.context_upload_args(body)
            elif action == "save":
                checked_fields(body, {"path", "text", "expected_content_hash"} | OPTIONS, {"path", "text"})
                doc = self.document_selector(context, body["path"], prefix="context")
                operation, args, revision = "context_document_update", {"document_id": doc["id"], "body": body["text"], "expected_content_hash": body.get("expected_content_hash", doc["content_hash"])}, doc["revision"]
            elif action == "retry":
                checked_fields(body, {"id"} | OPTIONS, {"id"})
                doc = self.document_selector(context, body["id"], kinds={"context_upload", "context_dump"})
                operation, args, revision = "context_retry", {"upload_id": doc["id"]}, doc["revision"]
            else:
                raise DomainError("not_found", "Context action is not registered")
            result = self.invoke(context, operation, args, self.options(body, revision=revision))
            if action == "upload":
                return self.context_upload_response(result)
            if result.status != "ok":
                return response(result)
            data = plain(result.data)
            return JSONResponse({"ok": True, **data, "id": (data.get("document") or {}).get("id", args.get("upload_id", args.get("document_id")))})
        if len(tail) == 3 and tail[0] == "log" and tail[2] == "labels":
            checked_fields(body, {"add", "remove"} | OPTIONS)
            current = self.data(context, "actions_get", {"action_id": tail[1]})["action"]
            removed = {label.casefold() for label in body.get("remove", [])}
            kept = [label for label in current["tags"] if label.casefold() not in removed]
            tags = kept + [label for label in body.get("add", []) if label.casefold() not in {kept_label.casefold() for kept_label in kept}]
            return response(self.invoke(context, "actions_correct", {"action_id": tail[1], "tags": tags}, self.options(body, revision=current.get("revision"))))
        raise DomainError("not_found", "Endpoint is not registered")

    async def asset_upload(self, request, context):
        from qorqut_operations.assets.operations import MAX_BYTES
        check_body_length(request, MAX_BYTES + 65536)
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise DomainError("invalid_request", "Asset upload requires multipart/form-data")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BYTES + 65536:
                raise DomainError("payload_too_large", "Asset exceeds the upload size limit")
        message = BytesParser(policy=email_policy.default).parsebytes(("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode() + body)
        if not message.is_multipart() or message.defects:
            raise DomainError("invalid_request", "Malformed multipart upload")
        fields, file = {}, None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if part.get_content_disposition() != "form-data" or payload is None or part.defects:
                raise DomainError("invalid_request", "Malformed multipart field")
            if name == "file" and file is None and part.get_filename():
                file = {"filename": part.get_filename(), "content_b64": base64.b64encode(payload).decode(), "media_type": part.get_content_type()}
            elif name in {"group", "topic", "notes", "batch", "task_ids"} and name not in fields and len(payload) <= (16384 if name == "task_ids" else 4096):
                try:
                    fields[name] = json.loads(payload) if name == "task_ids" else payload.decode("utf-8")
                except (UnicodeError, ValueError):
                    raise DomainError("invalid_request", "Upload metadata must be UTF-8") from None
            else:
                raise DomainError("invalid_request", "Unknown or repeated upload field")
        if file is None:
            raise DomainError("invalid_request", "One asset file is required")
        fields = {key: value for key, value in fields.items() if value}
        result = await anyio.to_thread.run_sync(lambda: self.invoke(context, "assets_upload", {**fields, **file}, self.options({})))
        if result.status != "ok":
            return response(result)
        asset = plain(result.data)["asset"]
        return JSONResponse({"ok": True, **asset, **asset.get("metadata", {})})

    @staticmethod
    def log_view(row):
        """The dashboard's log entry over an Action view: the Action's one text is the summary, its tags the labels."""
        occurred = row["occurred"]
        instant = occurred.get("instant") or occurred.get("date") or ""
        actor = (row.get("performed_by") or row.get("recorded_by") or {}).get("id", "")
        return {**row, "id": row["action_id"], "detail": "" if row["text"] == row["summary"] else row["text"], "labels": row["tags"],
            "occurred_at": instant, "occurred_date": occurred.get("date"), "ts": instant, "refs_json": row["refs"],
            "occurred_hour": occurred.get("hour"), "time_precision": occurred.get("precision"), "time_zone": occurred.get("time_zone"),
            "refs": row["refs"], "actor_id": actor, "actor": actor}

    def select_business(self, context, selector):
        if not isinstance(selector, str) or not selector:
            raise DomainError("scope_required", "Select an explicit review project")
        with self.runtime.database.read() as uow:
            row = uow.connection.execute("SELECT id FROM businesses WHERE id=? OR slug=?", (selector, selector)).fetchone()
            if row is None or row["id"] not in context.allowed_business_ids:
                raise DomainError("not_found", "Selected project is not available to this principal")
            return self.runtime.policy.context_for(context.principal_id, business_id=row["id"], review_business_id=row["id"], uow=uow)

    def routine_view(self, context, row):
        scope = "global" if row["scope"] == "global" else "biz"
        preview = self.data(context, "routines_preview", {"schedule": row["schedule"], "timezone": row["timezone"],
            **({"scope": "global"} if scope == "global" else {})}) if row["status"] == "active" else {"slots": []}
        return {**row, "scope": scope, "key": row["namespace"] + "/" + row["id"],
            "location": "global" if scope == "global" else "business:" + context.business_id,
            "review_scope": {"kind": "global"} if scope == "global" else {"kind": "business", "business_id": context.business_id}, "when": row["schedule"], "tz": row["timezone"], "zone": row["timezone"],
            "next": preview["slots"][0]["utc"] if preview["slots"] else None, "timeout": row["timeout_seconds"],
            "why": row["metadata_json"].get("why", ""), "run": [row["program"]], "commit": []}

    run_view = staticmethod(run_view)

    @staticmethod
    def _routine_context(uow):
        businesses = {row["id"]: row["slug"] for row in uow.connection.execute("SELECT id,slug FROM businesses")}
        definitions = {(row["namespace"], row["id"]): {"name": row["name"], "timezone": row["timezone"]}
            for row in uow.connection.execute("SELECT namespace,id,name,timezone FROM routines")}
        return businesses, definitions

    def routine_history(self, context, *, limit=50, global_scope=False, routine_id=None, hourly=False):
        args = {"routine_id": routine_id} if routine_id else {}
        args.update(view="hourly" if hourly else "recent", limit=limit or 200)
        records = [] if global_scope else self.data(context, "routines_runs", args)["items"]
        if global_scope or "manage_global_routines" in context.global_capabilities:
            records.extend(self.data(context, "routines_runs", {**args, "scope": "global"})["items"])
        # A business history read may contain this business's global executions.
        # Global aggregation requires the explicit capability above; never label
        # a partial business-only cost as the total for a global routine.
        if not global_scope and "manage_global_routines" not in context.global_capabilities:
            records = [row for row in records if row["namespace"] != "global"]
        if hourly:
            return records
        with self.runtime.database.read() as uow:
            businesses, definitions = self._routine_context(uow)
        return recent_runs(records, limit=limit, businesses=businesses, definitions=definitions)

    def live_routine_failures(self, context, now):
        """Failed-routine groups from the last 24h, without routine_history()'s
        exhaustive scan: an index-backed time window, queried directly for
        state='failed' rows only (not decoded and discarded like every other
        state in the window would be), fed through the same recent_runs()
        grouping the Routines page itself uses so identities match and
        js/routines.js's rtFailureToasts never double-toasts. A business row
        groups singly and a global row's group key needs no sibling row, so
        both group identity and the group's "failed" state are already fully
        determined by the failed row alone -- filtering to it here changes
        neither.
        """
        since = (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
        with self.runtime.database.read() as uow:
            rows = [decode_row(row) for row in uow.connection.execute(
                "SELECT * FROM routine_runs WHERE business_id IS ? AND namespace!='global' AND started_at>=? AND state='failed'",
                (context.business_id, since))]
            if "manage_global_routines" in context.global_capabilities:
                rows.extend(decode_row(row) for row in uow.connection.execute(
                    "SELECT * FROM routine_runs WHERE namespace='global' AND started_at>=? AND state='failed'", (since,)))
            businesses, definitions = self._routine_context(uow)
        return recent_runs(rows, limit=None, businesses=businesses, definitions=definitions)

    def live_platform_signoffs(self, context, since):
        """Pending platform_action signoffs, always, plus -- once the client has
        a cursor from a prior call -- rows that changed since then, any state,
        so a pending-to-applied/rejected transition still reaches the client.

        The pending set and the change stream are deliberately two different
        filter sets, not one shared no-"states" browse the way this used to
        work: SignoffService._list_operation's updated_cursor is bound to the
        exact filter set it was minted under and rejects any other set on
        redemption, so a cursor minted under {"states": ["pending"]} can never
        be redeemed by a no-"states" updated_after call or vice versa. The
        pending half is therefore its own bounded {"states": ["pending"]} call
        -- cheap, since it only ever reads and enriches actually-pending rows,
        never the resolved remainder of a business's full signoff history.
        The change-stream half keeps the old no-"states" filter set for its
        whole life so a pending-to-resolved transition is still visible to it:
        with no prior cursor, mint one with a throwaway limit:1 call (only
        updated_cursor is used, not its items); once minted, redeem it with
        updated_after exactly as before. Cost per call is pending rows plus
        changed rows only, never a full browse page of mostly-resolved rows.
        """
        args = {"category": "platform_action", "states": ["pending"], "limit": 100}
        pending = []
        for _ in range(100):
            page = self.data(context, "signoffs_list", args)
            pending.extend(page["items"])
            if not page.get("has_more"):
                break
            args = {**args, "cursor": page["next_cursor"]}
        else:
            raise DomainError("collection_too_large", "Use the paginated signoffs endpoint for this project")
        merged = {row["id"]: row for row in pending}
        if since:
            changed = self.data(context, "signoffs_list", {"category": "platform_action", "updated_after": since, "limit": 100})
            cursor = changed["updated_cursor"]
            for row in changed["items"]:
                merged[row["id"]] = row
        else:
            seed = self.data(context, "signoffs_list", {"category": "platform_action", "limit": 1})
            cursor = seed["updated_cursor"]
        return list(merged.values()), cursor

    def routine_processes_view(self, context, *, global_scope):
        """current/queued/invalid only, straight from BackgroundService.status():
        no routines_list, no routine_history(), no schedule previews. Root asks
        for the global-scoped job pool (business_id=None); a business asks for
        its own. Unlike routine_status(), status() is not routine-linked-jobs-
        only -- current/recent can be any job kind for that scope -- which is
        why this is a distinct view rather than a trimmed routine_status().
        """
        if not self.background:
            return {"current": None, "queued": 0, "invalid": [], "error": "Background service is not attached"}
        status = self.background.status(None if global_scope else context.business_id)
        return {key: status[key] for key in ("current", "running", "queues", "queued", "invalid")}

    def routine_get(self, request, context, tail, *, global_scope=False):
        scope = {"scope": "global"} if global_scope else {}
        if not tail:
            if request.query_params.get("view") == "processes":
                return revalidated(request, self.runtime.changes, {"jobs_routines"}, context.business_id,
                    lambda: JSONResponse(self.routine_processes_view(context, global_scope=global_scope)))
            rows = self.collection(context, "routines_list", scope)
            if not global_scope and "manage_global_routines" in context.global_capabilities:
                rows.extend(self.collection(context, "routines_list", {"scope": "global"}))
            status = self.background.routine_status(context) if self.background else {"current": None, "queued": [], "invalid": [], "error": "Background service is not attached"}
            current = status["current"]
            programs = [{key: value for key, value in program.definition.items() if key in {"id", "version", "input_schema", "scope_kinds", "timeout_seconds", "unavailable_reason"}}
                for program in self.background.programs] if self.background else []
            return JSONResponse({"enabled": self.background is not None, "routines": [self.routine_view(context, row) for row in rows],
                "current": current, "queued": status["queued"], "error": status["error"],
                "root": "manage_global_routines" in context.global_capabilities, "invalid": status["invalid"], "programs": programs,
                "hourly": hourly_runs(self.routine_history(context, global_scope=global_scope, hourly=True))})
        if tail == ["next"]:
            args = {**scope, "schedule": request.query_params.get("when", request.query_params.get("schedule", "")),
                "timezone": request.query_params.get("tz") or request.query_params.get("timezone") or "UTC"}
            return JSONResponse({"next": self.data(context, "routines_preview", args)["slots"]})
        if tail == ["runs"] or len(tail) == 2 and tail[1] == "runs":
            limit = integer_parameter(request.query_params.get("n", "50"), "Run limit", 1, 200)
            return JSONResponse(self.routine_history(context, limit=limit, global_scope=global_scope,
                routine_id=tail[0] if len(tail) == 2 else None))
        if len(tail) == 1:
            return response(self.invoke(context, "routines_get", {**scope, "routine_id": tail[0]}))
        raise DomainError("not_found", "Routine endpoint is not registered")

    def routine_post(self, context, tail, body, *, global_scope=False):
        if tail:
            raise DomainError("not_found", "Routine action is not registered")
        scope = {"scope": "global"} if global_scope else {}
        allowed = {"op", "id", "definition", "review_business_id", "job_id", "target_business_id"} | OPTIONS
        checked_fields(body, allowed, {"op"})
        if "review_business_id" in body and body["review_business_id"] != context.business_id:
            raise DomainError("validation_error", "Review destination is determined by routine ownership")
        operation = {"add": "routines_create", "set": "routines_update", "pause": "routines_pause", "resume": "routines_resume",
            "rm": "routines_delete", "run": "routines_run", "stop": "routines_stop"}.get(body["op"])
        if operation is None:
            raise DomainError("validation_error", "Unknown routine operation")
        args, revision = dict(scope), None
        if operation != "routines_create":
            before = self.data(context, "routines_get", {**scope, "routine_id": body.get("id")})["routine"]
            args["routine_id"], revision = before["id"], before["revision"]
        if operation in {"routines_create", "routines_update"}:
            if "definition" not in body:
                raise DomainError("validation_error", "Routine editing requires a registered program and typed definition; shell commands are retired")
            args["definition"] = body["definition"]
        for key in ("job_id", "target_business_id"):
            if key in body:
                args[key] = body[key]
        result = self.invoke(context, operation, args, self.options(body, revision=revision))
        return response(result)

    def background_global(self, request, context, parts, body):
        if self.background is None:
            raise DomainError("operation_unavailable", "Background service is not attached")
        location = body.get("location", "")
        if not isinstance(location, str):
            raise DomainError("validation_error", "Routine location must be a string")
        global_scope = parts[:2] == ["root", "routines"] or location == "global" or location.startswith("global:")
        if global_scope:
            if any(key in body or key in request.query_params for key in ("review_business_id", "biz")) or location.startswith("global:"):
                raise DomainError("validation_error", "Global routines no longer accept a review project; use the Global workspace")
            context = replace(context, business_id=None, review_business_id=None)
        selector = body.get("review_business_id") or request.query_params.get("review_business_id") or request.query_params.get("biz")
        if location.startswith(("business:", "global:")):
            selected = location.split(":", 1)[1]
            if selector and selector != selected:
                raise DomainError("validation_error", "Routine location and review project disagree")
            selector = selected
        if not global_scope:
            context = self.select_business(context, selector)
        if parts[:2] == ["root", "routines"]:
            if "manage_global_routines" not in context.global_capabilities:
                raise DomainError("forbidden", "Global routines require the registered global capability")
            return self.routine_get(request, context, parts[2:], global_scope=True) if request.method == "GET" else self.routine_post(context, parts[2:], body, global_scope=True)
        if parts == ["routines", "run"] and request.method == "POST":
            checked_fields(body, {"location", "id", "review_business_id"} | OPTIONS, {"location", "id"})
            return self.routine_post(context, [], {key: value for key, value in {**body, "op": "run"}.items() if key != "location"}, global_scope=global_scope)
        if parts == ["routines", "kill"] and request.method == "POST":
            checked_fields(body, {"location", "id", "job_id", "review_business_id", "target_business_id"} | OPTIONS, {"id", "job_id"})
            return self.routine_post(context, [], {key: value for key, value in {**body, "op": "stop"}.items() if key != "location"}, global_scope=global_scope)
        if parts == ["routines", "costs"] and request.method == "GET":
            args = {}
            if request.query_params.get("days"):
                days = integer_parameter(request.query_params["days"], "Cost interval", 1, 3660)
                args["since"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            elif request.query_params.get("since"):
                args["since"] = request.query_params["since"]
            return JSONResponse(self.data(context, "routines_costs", args))
        if parts == ["routines"] and request.method == "GET":
            return self.routine_get(request, context, [])
        if parts in (["filer"], ["bridge", "status"]) and request.method == "GET":
            status = self.background.status(context.business_id)
            kinds = {"context.file"} if parts == ["filer"] else {"platform.dispatch", "platform.reconcile", "platform.cancel"}
            status["recent"] = [row for row in status["recent"] if row["kind"] in kinds]
            if status["current"] and status["current"]["kind"] not in kinds:
                status["current"] = None
            status["queued"] = sum(row["state"] == "queued" for row in status["recent"])
            return JSONResponse(status)
        raise DomainError("not_found", "Background endpoint is not registered")
