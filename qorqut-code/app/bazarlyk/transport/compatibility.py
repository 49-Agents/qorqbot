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

from bazarlyk.composition.locations import business_hq
from bazarlyk.agents.roles import BUSINESS_AGENT_ROLE, FOUNDER_ROLE
from bazarlyk.departments import department as department_info, profile_department, department_id, BY_ID
from qorqut_operations.contracts import DomainError, InvocationOptions, new_id, plain
from qorqut_operations.storage.repositories import decode_row
from .http import check_body_length, checked_fields, read_json, revalidated
from .results import response
from .routine_views import recent_runs, run_view, hourly_runs

OPTIONS = {"expected_revision", "idempotency_key", "supersedes_signoff_id", "expected_superseded_status_revision"}


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
            from bazarlyk.service.local import LocalFiles
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
        department = profile_department(profile)
        look = department_info(department)
        icon, color = look["icon"], look["color"]
        if profile.get("role", {}).get("id") == FOUNDER_ROLE:
            icon = "lightbulb"
        elif profile.get("role", {}).get("id") == BUSINESS_AGENT_ROLE:
            icon = "business"  # the dashboard draws the business's own letter avatar
        return {"department": department, "icon": presentation.get("icon", icon), "color": color, "kind": profile.get("role", {}).get("id", "")}

    def registered_agent(self, context, selector, *, include_inactive=False):
        with self.runtime.database.read() as uow:
            row = uow.connection.execute("SELECT agent_id FROM agent_profiles WHERE home_business_id=? AND (agent_id=? OR name=?) AND (status='active' OR ?)", (context.business_id, selector, selector, include_inactive)).fetchone()
        if row is None:
            raise DomainError("not_found", "Active registered agent was not found in this business")
        return self.data(context, "agents_get", {"agent_id": row["agent_id"]})["agent"]

    def agent_view(self, context, profile):
        if profile.get("archival_record"):
            return profile
        plan = self.data(context, "plans_get", {"agent_id": profile["agent_id"]})["document"] or {}
        notes = self.data(context, "notes_list", {"agent_id": profile["agent_id"], "limit": 30})["items"]
        return {**profile, **self.appearance(profile), "agent_yaml": yaml.safe_dump({"generated": True, **profile}, sort_keys=False),
                "plan": plan.get("body", ""), "notes": [Path(note["path"]).name for note in notes],
                "sessions": [path.name for path in self.local_files.session_paths(context.business_id, profile["agent_id"])], "learnings": ""}

    def assignees(self, context):
        return self.data(context, "assignees_list")["assignees"]

    def assignee_id(self, context, value):
        if value in (None, ""):
            return None
        candidates = self.assignees(context)
        matches = [row for row in candidates if value == row["id"] or value == row["kind"] + ":" + (row.get("name") or row["display_name"])]
        if len(matches) != 1:
            raise DomainError("invalid_assignee", "Select one active member of this business")
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
            options = self.options(body)
            operation = 'context_retry' if parts[3] == 'retry' else 'context_retract'
            args = {'upload_id': parts[2]} if operation == 'context_retry' else {'upload_id': parts[2], 'force': bool(body.get('force'))}
            return await anyio.to_thread.run_sync(lambda: response(self.invoke(context, operation, args, options)))
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
        if len(parts) == 3 and parts[:2] == ["root", "desktop-servers"] and request.method == "GET":
            data = await anyio.to_thread.run_sync(lambda: self.local_files.desktop_server(context, parts[2]))
            return JSONResponse(data, headers={"Cache-Control": "no-store"})
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
            from bazarlyk.sandbox.state import environment_status
            with self.runtime.database.read() as uow:
                registered = uow.connection.execute("SELECT b.* FROM businesses b JOIN business_memberships m ON m.business_id=b.id WHERE m.principal_id=? AND m.active=1 AND b.archived_at IS NULL ORDER BY b.slug", (context.principal_id,)).fetchall()
                environments = {row["id"]: environment_status(uow, row["id"]) for row in registered}
            businesses = []
            for row in registered:
                selected = context.select_business(row["id"])
                profiles = self.data(selected, "agents_list")["agents"]
                metadata = json.loads(row["config_json"])
                environment = environments[row["id"]]
                viewer = getattr(getattr(self.runtime, "previews", None), "browser_viewers", None)
                if viewer is not None:
                    link = viewer.link(row["id"])
                    if link is not None:
                        environment = {**environment, "browser_viewer_url": link}
                businesses.append({"slug": row["slug"], "path": str(business_hq(self.runtime.paths, row)), "name": row["name"], "id": row["id"], "business_id": row["id"],
                    "environment": environment,
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
                entries = self.collection(context, "logs_list", key="entries") if limit == 0 else self.data(context, "logs_list", {"limit": limit})["entries"]
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
                from bazarlyk.domains.learnings.work import STATES
                counts = {state: sum(row["state"] == state for row in items) for state in STATES}
                counts["open_by_tag"] = {tag: sum(row["state"] == "open" and department_id(row.get("tag")) == tag for row in items) for tag in BY_ID}
                return JSONResponse({"items": items, "counts": counts})
            return revalidated(request, self.runtime.changes, {"learnings"}, context.business_id, produce)
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
            checked_fields(body, {"title", "owner", "owners", "status", "due", "tags", "note", "blocked_by", "fields"} | OPTIONS, {"title"})
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
            checked_fields(body, {"name", "display_name", "role", "assignment_brief", "department", "kind", "plugins", "presentation"} | OPTIONS,
                {"role"})
            # A business agent's name is allocated; agents_create rejects a chosen one.
            args = {key: body[key] for key in ("name", "display_name", "role", "assignment_brief", "plugins", "presentation") if key in body}
            if "department" in body:
                args["presentation"] = {**args.get("presentation", {}), "department": body["department"]}
            return response(self.invoke(context, "agents_create", args, self.options(body)), created=True)
        if len(tail) == 3 and tail[0] == "agent" and tail[2] == "meta":
            checked_fields(body, {"department", "icon"} | OPTIONS)
            profile = self.registered_agent(context, tail[1])
            presentation = {**profile["presentation"], **{key: body[key] for key in ("department", "icon") if key in body}}
            result = self.invoke(context, "agents_update", {"agent_id": profile["agent_id"], "changes": {"presentation": presentation}}, self.options(body, revision=profile["profile_revision"]))
            return response(result) if result.status != "ok" else JSONResponse({"ok": True, **self.appearance({**profile, "presentation": presentation})})
        return self.document_post(request, context, business, tail, body)

    @staticmethod
    def filing_status(upload, uow):
        """Project the latest attempt, including failures after model computation.

        Source revision orders retries; completion time must not let an older
        attempt hide a newer queued/running attempt. Processed uploads win.
        """
        meta = upload.get("metadata_json", upload.get("metadata", {}))
        # The legacy filer's job decides the shown state when it exists; in base-only mode the context base job does.
        row = uow.connection.execute(
            "SELECT id,state,error_json,attempts,updated_at FROM jobs WHERE business_id IS ? "
            "AND kind IN ('context.file','context.base_file') AND json_extract(arguments_json,'$.upload_id')=? "
            "ORDER BY CASE kind WHEN 'context.file' THEN 0 ELSE 1 END, "
            "CAST(json_extract(arguments_json,'$.expected_revision') AS INTEGER) DESC, created_at DESC, rowid DESC LIMIT 1",
            (upload["business_id"], upload["id"])).fetchone()
        state, error = "unavailable", None
        if upload["kind"] == "context_dump" or meta.get("status") == "processed":
            state = "processed"
        elif row:
            state = {"applying": "running", "cancel_requested": "cancelling"}.get(row["state"], row["state"])
            if state == "succeeded":
                state, error = "unavailable", "Filing finished without a filed upload"
            elif state == "failed":
                error = (json.loads(row["error_json"] or "null") or {}).get("message") or "Filing failed"
        elif meta.get("status") == "failed":
            state, error = "failed", meta.get("error")
        return {"id": upload["id"], "state": state, "error": error, "revision": upload["revision"],
            "context_base": meta.get("context_base"),
            "job_id": row["id"] if row else None, "attempts": row["attempts"] if row else 0,
            "updated_at": row["updated_at"] if row else upload.get("updated_at"),
            "retryable": upload["kind"] == "context_upload" and state in {"failed", "cancelled", "unavailable"}}

    def page_view(self, document, uow=None):
        from bazarlyk.domains.documents.note_snapshot import snapshot_hash
        from bazarlyk.domains.documents.journal_access import JOURNAL_KINDS
        meta = document["metadata"]
        def resolve_filing(uow):
            upload = uow.repository("documents", business_id=document["business_id"]).get(meta["upload_id"])
            return self.filing_status(upload, uow) if upload and upload["kind"] in {"context_upload", "context_dump"} else None
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
            from qorqut_operations.storage.repositories import decode_row
            found = uow.connection.execute(
                "SELECT * FROM documents WHERE kind='context_upload' "
                "AND json_extract(metadata_json, '$.journal_source_id')=? ORDER BY created_at, id",
                (document["id"],)).fetchall()
            result = []
            for saved in found:
                upload = decode_row(saved)
                upload_meta = upload["metadata_json"]
                status = self.filing_status(upload, uow)
                receipt = status.get("context_base") or {}
                active_attempt = status["state"] in {"queued", "running", "cancelling"}
                # A retry keeps its prior receipt until the new filer attempt
                # finishes. During that window show and poll the live job, and
                # keep the old receipt in a separate explicitly historical field.
                current_receipt = {} if active_attempt else receipt
                outcome = current_receipt.get("status")
                summary = current_receipt.get("summary") or {}
                partial = outcome == "filed" and bool(summary.get("denied") or summary.get("unresolved_checks"))
                result.append({"uploadId": upload["id"], "sourceRevision": upload_meta.get("journal_source_revision"),
                    "sourceHash": upload_meta.get("journal_source_hash"), "sourceDate": upload_meta.get("journal_source_date"),
                    "createdAt": upload_meta.get("journal_uploaded_at"),
                    "changedSinceSubmission": (upload_meta.get("journal_source_revision") != document.get("revision")
                        or upload_meta.get("journal_source_hash") != document.get("content_hash")),
                    "state": status["state"] if active_attempt else "partial" if partial else outcome or status["state"],
                    "filing": {**status, "context_base": None if active_attempt else status.get("context_base")},
                    "summary": summary, "previousReceipt": receipt if active_attempt and receipt else None,
                    "retryable": status.get("retryable", False)})
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
            "category": meta.get("category", ""), "authorId": meta.get("author_id", ""),
            "date": meta.get("date", document.get("created_at", "")[:10]), "pinned": meta.get("pinned") in (True, "true"),
            "statuses": meta.get("statuses", {}), "createdAt": meta.get("created", document.get("created_at", "")),
            "updatedAt": meta.get("updated", document.get("updated_at", "")),
            "uploaded": meta.get("uploaded", ""), "uploadId": meta.get("upload_id", "")}

    def document_selector(self, context, selector, operation="context_list", *, prefix=None, kinds=None):
        if not isinstance(selector, str) or not selector or "\\" in selector or ".." in PurePosixPath(selector).parts or selector.startswith("/"):
            raise DomainError("validation_error", "A registered document selector is required")
        path = prefix + "/" + selector if prefix else selector
        documents = self.collection(context, operation)
        matches = [row for row in documents if (kinds is None or row["kind"] in kinds) and
            (row["id"] == selector or row["path"] == path or (prefix is None and PurePosixPath(row["path"]).stem == selector))]
        if len(matches) != 1:
            raise DomainError("not_found", "Registered document was not found unambiguously in this business")
        return matches[0]

    def context_item(self, document):
        meta, path = document["metadata"], PurePosixPath(document["path"])
        if document["kind"] in {"context_upload", "context_dump"}:
            with self.runtime.database.read() as uow:
                filing = self.filing_status(document, uow)
            meta = {**meta, "filing": filing, "status": filing["state"], "error": filing["error"],
                "failed": filing["state"] == "failed", "retryable": filing["retryable"], "attempts": filing["attempts"]}
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

    def context_processes_view(self, context):
        """filing/summarizing/has_filer only, the way context_view builds them
        (see its jobs()/attempts construction below) but without context_list,
        the tree, or the graph: current/queued come straight from an indexed
        jobs query instead of filing_status() over every inbox/dump document.

        filing mirrors context_view's exact bucketing: current is a job whose
        state is running or applying (filing_status() folds "applying" into
        "running"); queued counts only literal "queued" jobs, not
        waiting_signoff. summarizing mirrors jobs()'s own bucketing instead:
        current is running or applying; queued counts queued or
        waiting_signoff. topic only ever needs to answer "is this a map job",
        the one thing contextRows() (js/processes.js) branches on, so it comes
        straight from the job's own kind instead of a document lookup.
        """
        def started_at(value):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else 0
        with self.runtime.database.read() as uow:
            filing_jobs = [decode_row(row) for row in uow.connection.execute(
                "SELECT id,state,updated_at FROM jobs WHERE business_id=? AND kind='context.file' "
                "AND state IN ('queued','running','applying')", (context.business_id,))]
            summary_jobs = [decode_row(row) for row in uow.connection.execute(
                "SELECT id,kind,state,updated_at FROM jobs WHERE business_id=? AND kind IN ('context.summarize','context.map') "
                "AND state IN ('queued','running','applying','waiting_signoff')", (context.business_id,))]
        filing_current = next((row for row in filing_jobs if row["state"] in {"running", "applying"}), None)
        filing = {"current": {"id": filing_current["id"], "started": started_at(filing_current["updated_at"])} if filing_current else None,
            "queued": sum(row["state"] == "queued" for row in filing_jobs)}
        summary_current = next((row for row in summary_jobs if row["state"] in {"running", "applying"}), None)
        summarizing = {"current": {"id": summary_current["id"], "started": started_at(summary_current["updated_at"]),
            "topic": "map:" if summary_current["kind"] == "context.map" else ""} if summary_current else None,
            "queued": sum(row["state"] in {"queued", "waiting_signoff"} for row in summary_jobs)}
        return {"filing": filing, "summarizing": summarizing, "has_filer": self.background is not None}

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
            "inbox": [{**item, "attempts": int(item.get("attempts", 0)), "failed": item.get("failed", False)} for item in items if item["kind"] == "inbox"],
            "dumps": [{**item, "map": item["id"] in mapped} for item in items if item["kind"] == "dump"], "maps": maps,
            "people": people}
        status = self.background.status(context.business_id) if self.background else {"current": None, "queued": 0, "recent": [], "error": None}
        def jobs(kinds):
            selected = []
            for row in status["recent"]:
                if row["kind"] not in kinds:
                    continue
                args = row["arguments_json"]
                source = by_id.get(args.get("upload_id", args.get("document_id")), {})
                path = PurePosixPath(source.get("path", "context/unknown"))
                topic = "map:" + path.stem if row["kind"] == "context.map" else "person:" + path.stem if source.get("kind") == "context_person" else str(path.parent).removeprefix("context/")
                selected.append({**row, "job_id": row["id"], "id": path.stem, "topic": topic,
                    "started": datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")).timestamp()})
            failed = [row for row in selected if row["state"] == "failed"]
            return {"current": next((row for row in selected if row["state"] in {"running", "applying"}), None),
                "queued": sum(row["state"] in {"queued", "waiting_signoff"} for row in selected), "items": selected,
                "failed": failed, "done": [row for row in selected if row["state"] == "succeeded"],
                "error": (failed[0].get("error_json") or {}).get("message") if failed else None,
                "every": self.background.interval if self.background else None}
        attempts = [{**item["filing"], "id": item["id"],
            "started": datetime.fromisoformat(item["filing"]["updated_at"].replace("Z", "+00:00")).timestamp()
                if item["filing"].get("updated_at") else 0}
            for item in items if item.get("filing")]
        failures = [item for item in attempts if item["state"] == "failed"]
        filing = {"current": next((item for item in attempts if item["state"] == "running"), None),
            "queued": sum(item["state"] == "queued" for item in attempts), "items": attempts,
            "failed": failures, "done": [item for item in attempts if item["state"] == "processed"],
            "error": failures[0]["error"] if failures else None,
            "every": self.background.interval if self.background else None}
        result.update(filing=filing, summarizing=jobs({"context.summarize", "context.map"}), has_filer=self.background is not None)
        result["graph"] = self.data(context, "context_graph_get", {})
        return result

    def document_get(self, request, context, business, tail):
        if tail == ["notes"]:
            def produce():
                category = request.query_params.get("category")
                rows = self.collection(context, tail[0] + "_list", {"category": category} if category else None)
                with self.runtime.database.read() as uow:
                    return JSONResponse([self.page_view(row, uow) for row in rows])
            # page_view()'s filing status reads `jobs` directly (coarse jobs_routines).
            return revalidated(request, self.runtime.changes, {"documents", "jobs_routines"}, context.business_id, produce)
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
            # read context_list (documents), processes/default read `jobs`
            # directly (coarse jobs_routines); the query string is already part
            # of the ETag's own identity, so the three variants never collide.
            return revalidated(request, self.runtime.changes, {"documents", "jobs_routines"}, context.business_id, produce)
        if tail == ["context", "file"]:
            tree = request.query_params.get("dir", "context")
            if tree not in {"context", "marketing-content"}:
                raise DomainError("validation_error", "Unknown document tree")
            doc = self.document_selector(context, request.query_params.get("p", ""), "context_list" if tree == "context" else "content_list", prefix=tree)
            return JSONResponse({"path": request.query_params["p"], "dir": tree, "text": doc["body"], "document_id": doc["id"], "revision": doc["revision"], "content_hash": doc["content_hash"],
                "material": {key: doc[key] for key in ("material_type", "origin", "tags") if key in doc}})
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
                | {("marketing-content/" if note.get("tree") == "marketing-content" else "") + str(PurePosixPath(note["path"]).parent) for note in notes})
            person_refs = {key: row for row in documents.values() if row["kind"] == "context_person" for key in (row["id"], PurePosixPath(row["path"]).stem)}
            people = {person_refs[key]["id"]: {"slug": PurePosixPath(person_refs[key]["path"]).stem, "name": person_refs[key]["metadata"].get("name", "")}
                for key in [*filed.get("people", []), *(key for note in notes for key in note.get("people", []))] if key in person_refs}
            jobs = [row for row in self.background.status(context.business_id)["recent"] if row["kind"] == "context.map" and row["arguments_json"].get("upload_id") == upload["id"]] if self.background else []
            current = jobs[0] if jobs else None
            identity = PurePosixPath(upload["path"]).stem
            return JSONResponse({"id": identity, "document_id": upload["id"], "map": self.map_view(max(maps, key=lambda row: row["created_at"]), upload) if maps else None,
                "trace": {"id": identity, "document_id": upload["id"], "where": "_dumps" if upload["kind"] == "context_dump" else "_inbox",
                    "notes": notes, "topics": topics, "people": list(people.values()), "map": bool(maps)},
                "mapping": bool(current and current["state"] in {"queued", "running", "applying", "waiting_signoff"}),
                "error": (current.get("error_json") or {}).get("message", "") if current else ""})
        if tail == ["content"]:
            return JSONResponse(self.content_view(context, request.query_params))
        raise DomainError("not_found", "Endpoint is not registered")

    @staticmethod
    def context_upload_args(body):
        """The context_upload field mapping shared by the business and Global routes."""
        checked_fields(body, {"text", "notes", "topic", "story_ids", "origin", "occurred_on", "participants"} | OPTIONS, {"text"})
        args = {"body": body["text"], "title": body.get("topic") or body.get("notes") or "Context upload",
                "source": "dashboard", "story_ids": body.get("story_ids", [])}
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
            checked_fields(body, {"title", "content", "pinned", "date", "statuses", "ordubashy_visible", "expected_content_hash", "story_ids", "category"} | OPTIONS)
            # The business in the route is the explicit target of a business-owned note.
            target = {"target_business_id": context.business_id} if context.business_id else {}
            if len(tail) == 1:
                args = {"title": body.get("title") or "Daily", "body": body.get("content", "")}
                args.update({key: body[key] for key in ("pinned", "date", "story_ids", "category") if key in body})
                if "category" in body:
                    args.update(target)
                operation, revision = domain + "_create", None
            else:
                doc = self.document_selector(context, tail[1], domain + "_list")
                if "category" in body and body["category"] != doc["metadata"].get("category"):
                    raise DomainError("validation_error", "A note's category cannot be changed")
                revision = doc["revision"]
                args = {"document_id": doc["id"], "expected_content_hash": body.get("expected_content_hash", doc["content_hash"]),
                        **(target if doc["kind"] == "business_owned_note" else {})}
                if len(tail) == 2:
                    operation = domain + "_update"
                    if "content" in body:
                        args["body"] = body["content"]
                    args["metadata"] = {key: body[key] for key in ("title", "pinned", "statuses", "ordubashy_visible") if key in body}
                elif len(tail) == 3 and tail[2] in {"delete", "upload"}:
                    operation = ("journal_upload_to_filer" if context.business_id is None and tail[2] == "upload"
                                 else domain + "_" + tail[2])
                else:
                    raise DomainError("not_found", "Document action is not registered")
            if "story_ids" in body and operation == "notes_upload":
                args["story_ids"] = body["story_ids"]
            elif "story_ids" in body and operation != "notes_create":
                raise DomainError("validation_error", "Use story link operations to change associations on an existing note")
            result = self.invoke(context, operation, args, self.options(body, revision=revision))
            if result.status != "ok":
                return response(result)
            result_data = plain(result.data)
            if result_data.get("document"):
                doc = self.data(context, domain + "_get", {"document_id": result_data["document"]["id"]})["document"]
                return JSONResponse(self.page_view(doc))
            return JSONResponse({"ok": True, **result_data})
        if len(tail) == 2 and tail[0] == "context":
            action, revision = tail[1], None
            if action == "upload":
                operation, args = "context_upload", self.context_upload_args(body)
            elif action == "save":
                checked_fields(body, {"path", "text", "expected_content_hash"} | OPTIONS, {"path", "text"})
                doc = self.document_selector(context, body["path"], prefix="context")
                operation, args, revision = "context_document_update", {"document_id": doc["id"], "body": body["text"], "expected_content_hash": body.get("expected_content_hash", doc["content_hash"])}, doc["revision"]
            elif action in {"retry", "map"}:
                checked_fields(body, {"id"} | OPTIONS, {"id"})
                doc = self.document_selector(context, body["id"], kinds={"context_upload", "context_dump"})
                operation, args, revision = ("context_retry" if action == "retry" else "context_map_generate"), {"upload_id": doc["id"]}, doc["revision"]
            elif action == "summarize":
                checked_fields(body, {"person", "note", "topic"} | OPTIONS)
                selectors = [key for key in ("person", "note", "topic") if body.get(key)]
                if len(selectors) != 1:
                    raise DomainError("validation_error", "Select exactly one summary target")
                key = selectors[0]
                selector = "_people/" + body[key] + ".md" if key == "person" else body[key] + "/_topic.md" if key == "topic" else body[key]
                doc = self.document_selector(context, selector, prefix="context")
                operation, args, revision = "context_summarize", {"document_id": doc["id"]}, doc["revision"]
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
            current = self.data(context, "logs_get", {"log_id": tail[1]})["entry"]
            return response(self.invoke(context, "logs_labels_update", {"log_id": tail[1], "add": body.get("add", []), "remove": body.get("remove", [])}, self.options(body, revision=current.get("revision"))))
        raise DomainError("not_found", "Endpoint is not registered")

    async def asset_upload(self, request, context):
        from bazarlyk.domains.assets.operations import MAX_BYTES
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
            elif name in {"group", "topic", "notes", "batch", "story_ids"} and name not in fields and len(payload) <= (16384 if name == "story_ids" else 4096):
                try:
                    fields[name] = json.loads(payload) if name == "story_ids" else payload.decode("utf-8")
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
        return {**row, "ts": row.get("occurred_at", row.get("created_at", "")),
            "refs": row.get("refs_json", {}), "actor": row.get("actor_id", "")}

    def content_view(self, context, query):
        documents = self.collection(context, "content_list")
        logs = self.collection(context, "logs_list", {"tags": ["marketing"]}, key="entries")
        items, by_path = [], {}
        for document in documents:
            meta = document["metadata"]
            relative = str(PurePosixPath(document["path"]).relative_to("marketing-content"))
            item = {"kind": "note", "id": relative, "document_id": document["id"], "date": meta.get("date", document.get("created_at", "")[:10]),
                "ts": document.get("created_at", ""), "platform": meta.get("platform", relative.split("/")[0]), "channel": meta.get("channel", ""),
                "target": meta.get("target", ""), "title": meta.get("title", ""), "kindTag": meta.get("kind", ""), "url": meta.get("url", ""),
                "signoff_id": document.get("signoff_id"), "campaign_id": document.get("campaign_id"),
                "campaign_title": document.get("campaign_title"), "by": meta.get("by", ""),
                "actor": meta.get("filed_by", meta.get("by", "")), "log": "", "logId": "", "logSummary": "", "logDetail": "",
                "labels": [], "preview": " ".join(document["body"].split())[:160], "_body": document["body"]}
            items.append(item)
            by_path[document["path"]] = item
        for entry in logs:
            refs = entry.get("refs_json", {})
            item = by_path.get(refs.get("file"))
            if item and not item["logId"]:
                item.update(log=entry["id"][-7:], logId=entry["id"], logSummary=entry["summary"], logDetail=entry["detail"],
                    labels=entry["labels"], actor=entry["actor_id"], ts=entry["occurred_at"])
            else:
                items.append({"kind": "log", "id": entry["id"], "date": entry.get("occurred_date") or entry["occurred_at"][:10], "ts": entry["occurred_at"],
                    "platform": refs.get("channel", "").split("-")[0], "channel": refs.get("channel", ""), "target": refs.get("target", ""),
                    "title": entry["summary"], "kindTag": refs.get("kind", ""), "url": refs.get("url", ""), "signoff_id": refs.get("signoff_id", ""),
                    "campaign_id": None, "campaign_title": None, "by": entry["actor_id"], "actor": entry["actor_id"], "log": entry["id"][-7:],
                    "logId": entry["id"], "logSummary": entry["summary"], "logDetail": entry["detail"], "labels": entry["labels"], "preview": "", "_body": ""})
        campaigns = sorted({item["campaign_id"]: {"value": item["campaign_id"], "label": item["campaign_title"]}
            for item in items if item["campaign_id"]}.values(), key=lambda item: (item["label"], item["value"]))
        names = [item["label"] for item in campaigns]
        for item in campaigns:
            if names.count(item["label"]) > 1:
                item["label"] += " · " + item["value"][:8]
        labels = sorted({label for item in items for label in item["labels"]})
        filters = {key: set(query.get(key, "").split(",")) - {""} for key in ("platform", "channel", "kind", "campaign_id", "label")}
        needle = query.get("q", "").strip().casefold()
        out = []
        for item in items:
            if any(values and item["kindTag" if key == "kind" else key] not in values for key, values in filters.items() if key != "label"):
                continue
            if filters["label"] and not filters["label"].intersection(item["labels"]):
                continue
            if needle and needle not in (item["title"] + " " + item["target"] + " " + item["_body"]).casefold():
                continue
            out.append({key: value for key, value in item.items() if key != "_body"})
        return {"items": sorted(out, key=lambda item: (item["date"], item["ts"]), reverse=True), "campaigns": campaigns, "labels": labels,
            "kinds": ["post", "comment", "reply", "dm", "email", "ad", "video", "image", "launch", "decision"]}

    def select_business(self, context, selector):
        if not isinstance(selector, str) or not selector:
            raise DomainError("scope_required", "Select an explicit review business")
        with self.runtime.database.read() as uow:
            row = uow.connection.execute("SELECT id FROM businesses WHERE id=? OR slug=?", (selector, selector)).fetchone()
            if row is None or row["id"] not in context.allowed_business_ids:
                raise DomainError("not_found", "Selected business is not available to this principal")
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
            raise DomainError("collection_too_large", "Use the paginated signoffs endpoint for this business")
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
        return {"current": status["current"], "queued": status["queued"], "invalid": status["invalid"]}

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
                raise DomainError("validation_error", "Global routines no longer accept a review business; use the Global workspace")
            context = replace(context, business_id=None, review_business_id=None)
        selector = body.get("review_business_id") or request.query_params.get("review_business_id") or request.query_params.get("biz")
        if location.startswith(("business:", "global:")):
            selected = location.split(":", 1)[1]
            if selector and selector != selected:
                raise DomainError("validation_error", "Routine location and review business disagree")
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
