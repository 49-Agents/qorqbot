"""Orchestration for inspect → plan → check/build → stage → deploy → adopt."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable

from .adapters import AdapterContext, adapter_for, executor_for_component, executor_for_installation
from .errors import ReconciliationRequired, ReleaseError, UncertainRemoteEffect, UnsupportedAdapter
from .gitops import (
    cleanup_component,
    merge_component,
    prepare_cleanup,
    prepare_merge,
    reconcile_cleanup,
    reconcile_merge,
)
from .io import InstallationLock, RecordStore, digest, redact
from .manifest import load_manifest, load_plan, plan_manifest


class DestinationLocks:
    """Acquire one owner-recorded lock per frozen mutation destination."""

    def __init__(
        self,
        locks: dict[str, InstallationLock],
        component_keys: dict[tuple[str, str], str],
        primary_key: str,
    ) -> None:
        self.locks = locks
        self.component_keys = component_keys
        self.primary_key = primary_key
        self._entered: list[str] = []

    @property
    def primary(self) -> InstallationLock:
        return self.locks[self.primary_key]

    def for_component(self, component_id: str, action: str) -> InstallationLock:
        return self.locks[self.component_keys.get((component_id, action), self.primary_key)]

    def __enter__(self) -> "DestinationLocks":
        try:
            for key in sorted(self.locks):
                self.locks[key].__enter__()
                self._entered.append(key)
        except BaseException:
            for key in reversed(self._entered):
                self.locks[key].__exit__(None, None, None)
            self._entered.clear()
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        for key in reversed(self._entered):
            self.locks[key].__exit__(exc_type, exc, traceback)
        self._entered.clear()


class ReleaseWorker:
    def __init__(self, plan: dict[str, Any], *, records_root: Path, installation_config: dict[str, Any] | None = None, executor: Any = None):
        self.plan = plan
        if isinstance(plan.get("migration"), dict) and plan["migration"].get("required"):
            raise UnsupportedAdapter(
                "required migrations are not supported by release-worker; execute a separately reviewed migration lifecycle before deployment"
            )
        self.records_root = records_root
        self.store = RecordStore(records_root, plan["release_id"])
        frozen_config = plan.get("installation", {})
        frozen_digest = plan.get("installation_digest")
        if frozen_digest != digest(frozen_config):
            raise ReleaseError("plan is missing a valid frozen installation configuration digest")
        if installation_config is not None and digest(installation_config) != frozen_digest:
            raise ReleaseError("installation configuration override does not match the frozen plan")
        self.installation_config = frozen_config
        self._executor_override = executor
        self.executor = executor or executor_for_installation(self.installation_config)
        self.artifact_root = records_root / plan["release_id"] / "artifacts"

    @classmethod
    def from_plan_path(cls, path: Path, **kwargs: Any) -> "ReleaseWorker":
        return cls(load_plan(path), **kwargs)

    @classmethod
    def from_manifest_path(cls, path: Path, *, records_root: Path, **kwargs: Any) -> "ReleaseWorker":
        return cls(plan_manifest(load_manifest(path)), records_root=records_root, **kwargs)

    def initialize(self) -> dict[str, Any]:
        return self.store.create(self.plan)

    def _context(self) -> AdapterContext:
        return AdapterContext(self.plan, self.artifact_root, self.installation_config, self.executor)

    def _components(self) -> list[dict[str, Any]]:
        return list(self.plan["components"])

    def _approval(self, scope: str, *, confirm: bool) -> None:
        record = self.store.load()
        approval = record.get("approvals", {}).get(scope)
        if not confirm:
            raise ReleaseError(f"{scope} requires explicit confirmation after recorded owner authorization")
        if not approval or approval.get("plan_digest") != self.plan["plan_digest"]:
            raise ReleaseError(f"no current recorded {scope} authorization is bound to this plan")

    def _require_steps(self, components: list[dict[str, Any]], *actions: str) -> None:
        record = self.store.load()
        for component in components:
            for action in actions:
                step = record.get("steps", {}).get(f"{action}:{component['id']}", {})
                before = step.get("before", {})
                if not self._step_matches_component(step, before, component):
                    for candidate_id, candidate in record.get("steps", {}).items():
                        if not candidate_id.startswith(f"{action}:"):
                            continue
                        candidate_before = candidate.get("before", {})
                        if candidate.get("status") == "succeeded" and component["id"] in candidate_before.get("component_ids", []):
                            if self._step_matches_component(candidate, candidate_before, component):
                                step, before = candidate, candidate_before
                                break
                if not self._step_matches_component(step, before, component) or before.get("plan_digest") != self.plan["plan_digest"]:
                    raise ReleaseError(
                        f"{action} evidence is required for exact component input {component['id']} before advancing"
                    )

    @staticmethod
    def _step_matches_component(
        step: dict[str, Any],
        before: dict[str, Any],
        component: dict[str, Any],
    ) -> bool:
        if step.get("status") != "succeeded":
            return False
        source_commits = before.get("source_commits", {})
        source_commit = source_commits.get(component["id"], before.get("source_commit"))
        return source_commit == component["source_commit"]

    def _deployment_units(self) -> list[tuple[str, list[dict[str, Any]]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for component in self._components():
            group = component["id"]
            if group not in groups:
                order.append(group)
                groups[group] = []
            groups[group].append(component)
        component_group = {component["id"]: group for group, items in groups.items() for component in items}
        dependencies = {
            group: {
                component_group[dependency]
                for component in items
                for dependency in component.get("dependencies", [])
                if component_group[dependency] != group
            }
            for group, items in groups.items()
        }
        ordered: list[str] = []
        remaining = {group: set(values) for group, values in dependencies.items()}
        while remaining:
            ready = [group for group in order if group in remaining and not remaining[group]]
            if not ready:
                raise ReleaseError(f"deployment-unit dependency cycle after runtime grouping: {sorted(remaining)}")
            ordered.extend(ready)
            for group in ready:
                remaining.pop(group)
            for values in remaining.values():
                values.difference_update(ready)
        return [(f"deploy:{group}", groups[group]) for group in ordered]

    def _units(self, action: str) -> list[tuple[str, list[dict[str, Any]]]]:
        if action == "deploy":
            return self._deployment_units()
        if action == "merge":
            groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
            order: list[tuple[str, str, str, str]] = []
            for component in self._components():
                key = (
                    str(Path(component["repository"]).resolve()),
                    component["source_commit"],
                    component.get("base_commit", ""),
                    component["target_branch"],
                )
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                groups[key].append(component)
            return [(f"merge:{groups[key][0]['id']}", groups[key]) for key in order]
        if action == "cleanup":
            groups: dict[str, list[dict[str, Any]]] = {}
            order: list[str] = []
            for component in self._components():
                key = str(Path(component["repository"]).resolve())
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                groups[key].append(component)
            return [(f"cleanup:{groups[key][0]['id']}", groups[key]) for key in order]
        return [(f"{action}:{component['id']}", [component]) for component in self._components()]

    def _step_components(self, step_id: str, before: dict[str, Any]) -> list[dict[str, Any]]:
        ids = before.get("component_ids")
        if not ids:
            ids = [step_id.split(":", 1)[1]]
        by_id = {component["id"]: component for component in self._components()}
        try:
            return [by_id[identifier] for identifier in ids]
        except KeyError as exc:
            raise ReconciliationRequired(f"{step_id}: step names a component not present in the frozen plan") from exc

    def _require_prerequisites(self, action: str, components: list[dict[str, Any]]) -> None:
        if action in {"build", "merge", "stage", "deploy", "adopt"}:
            self._require_release_checks()
        if action in {"merge"}:
            self._require_steps(components, "check", "build")
        elif action == "stage":
            self._require_steps(components, "check", "build")
        elif action == "deploy":
            # A runtime switch may change which package map every component
            # observes, so all frozen release inputs need exact evidence before
            # any deployment unit is allowed to move the service.
            self._require_steps(self._components(), "check", "build", "stage", "merge")
        elif action == "adopt":
            self._require_steps(components, "check", "build", "stage", "merge")

    def _require_release_checks(self) -> None:
        if not self.plan.get("checks") and not self.plan.get("configuration_requirements"):
            return
        step = self.store.load().get("steps", {}).get("check:release", {})
        before = step.get("before", {})
        if step.get("status") != "succeeded" or before.get("plan_digest") != self.plan["plan_digest"]:
            raise ReleaseError("release-level checks and configuration requirements must succeed before advancing")

    def _run_release_checks(self, context: AdapterContext) -> None:
        step_id = "check:release"
        existing = self.store.load().get("steps", {}).get(step_id, {})
        if existing.get("status") == "succeeded":
            return
        declarations = {
            "checks": self.plan.get("checks", []),
            "configuration_requirements": self.plan.get("configuration_requirements", []),
        }
        before = {"plan_digest": self.plan["plan_digest"], "declarations_digest": digest(declarations)}
        self.store.step_started(step_id, before=before)
        try:
            observed: dict[str, Any] = {"notes": [], "checks": [], "configuration_requirements": []}
            for item in declarations["checks"]:
                if "note" in item:
                    observed["notes"].append(item["note"])
                    continue
                output = context.executor.run(item["command"])
                observed["checks"].append({
                    "argv": item["command"],
                    "output_digest": hashlib.sha256(output.encode()).hexdigest(),
                })
            for item in declarations["configuration_requirements"]:
                output = context.executor.run(item["check"])
                observed["configuration_requirements"].append({
                    "name": item["name"],
                    "argv": item["check"],
                    "output_digest": hashlib.sha256(output.encode()).hexdigest(),
                })
        except Exception as exc:
            self.store.step_failed(step_id, error=str(exc), after={"external_effect": False})
            raise
        self.store.step_finished(step_id, after=observed)

    def authorize(self, scope: str, evidence_path: Path) -> dict[str, Any]:
        if scope not in {"merge", "deploy", "adopt", "recover", "cleanup"}:
            raise ReleaseError(f"unsupported authorization scope: {scope}")
        evidence = json.loads(evidence_path.read_text())
        with InstallationLock(self.installation_config, self.plan["installation"]["id"], executor=self.executor) as lock:
            with lock.hold_scope("authorize:" + scope):
                return self.store.authorize(scope, evidence)

    def _destination_config(self, component: dict[str, Any], action: str) -> dict[str, Any]:
        recipe_field = {
            "adopt": "adoption_recipe",
            "deploy": "deployment_recipe",
            "inspect": "deployment_recipe",
            "recover": "recovery_recipe",
        }.get(action, "deployment_recipe")
        recipe = component.get(recipe_field, {})
        if not isinstance(recipe, dict):
            recipe = {}
        deployment_recipe = component.get("deployment_recipe", {})
        if not isinstance(deployment_recipe, dict):
            deployment_recipe = {}
        host = recipe.get("host")
        if action == "recover" and not host:
            host = deployment_recipe.get("host")
        release_root = recipe.get("release_root")
        if action == "recover" and not release_root:
            release_root = deployment_recipe.get("release_root")
        lock_path = recipe.get("lock_path") or deployment_recipe.get("lock_path")
        installation_host = self.installation_config.get("host", "localhost")
        if (
            not host or str(host) == str(installation_host)
        ) and not release_root and not lock_path:
            return self.installation_config
        destination = dict(self.installation_config)
        if host:
            destination["host"] = str(host)
        if release_root:
            destination["release_root"] = str(release_root)
        if lock_path:
            destination["lock_path"] = str(lock_path)
        return destination

    @staticmethod
    def _destination_key(lock: InstallationLock, executor: Any) -> str:
        host = getattr(executor, "host", None)
        if host:
            path = lock.remote_path or str(lock.path)
            return f"{host}\0{path}"
        return f"localhost\0{lock.path}"

    def _destination_locks(self, actions: set[str]) -> DestinationLocks:
        locks: dict[str, InstallationLock] = {}
        component_keys: dict[tuple[str, str], str] = {}
        primary = InstallationLock(
            self.installation_config,
            self.plan["installation"]["id"],
            executor=self.executor,
        )
        primary_key = self._destination_key(primary, self.executor)
        locks[primary_key] = primary
        for component in self._components():
            for action in sorted(actions):
                raw_executor = self._executor_override or executor_for_component(
                    component,
                    self.installation_config,
                    self.executor,
                    action=action,
                )
                destination = self._destination_config(component, action)
                candidate = InstallationLock(
                    destination,
                    self.plan["installation"]["id"],
                    executor=raw_executor,
                )
                key = self._destination_key(candidate, raw_executor)
                locks.setdefault(key, candidate)
                component_keys[(component["id"], action)] = key
        return DestinationLocks(locks, component_keys, primary_key)

    def _run(self, action: str, fn: Callable[[dict[str, Any], AdapterContext], Any], *, scope: str | None = None, confirm: bool = False) -> dict[str, Any]:
        locks = self._destination_locks({action})
        with locks:
            return self._run_locked(action, fn, scope=scope, confirm=confirm, locks=locks)

    def _run_locked(
        self,
        action: str,
        fn: Callable[[dict[str, Any], AdapterContext], Any],
        *,
        scope: str | None = None,
        confirm: bool = False,
        locks: DestinationLocks | None = None,
    ) -> dict[str, Any]:
        lock = locks.primary if locks else None
        if lock:
            lock.assert_owned()
        self.initialize()
        if scope:
            self._approval(scope, confirm=confirm)
        context = self._context()
        if lock:
            context.executor = lock.guarded(context.executor)
        if action == "check":
            scope_context = lock.hold_scope("check:release") if lock else nullcontext()
            with scope_context:
                self._run_release_checks(context)
        results: dict[str, Any] = {}
        for step_id, components in self._units(action):
            self._require_prerequisites(action, components)
            record = self.store.load()
            existing = record.get("steps", {}).get(step_id, {})
            if existing.get("status") == "succeeded":
                for component in components:
                    results[component["id"]] = existing.get("after")
                continue
            if existing.get("status") == "running" or (
                existing.get("status") == "failed" and existing.get("after", {}).get("external_effect") == "unknown"
            ):
                raise ReconciliationRequired(f"{step_id}: reconcile the uncertain external effect with resume before retrying")
            component = components[0]
            adapter = adapter_for(component)
            merge_intent = None
            cleanup_intent = None
            if action == "merge":
                merge_intent = existing.get("before", {}).get("merge_intent")
                if merge_intent is None:
                    merge_intent = prepare_merge(component, confirm=confirm)
            elif action == "cleanup":
                cleanup_intent = existing.get("before", {}).get("cleanup_intent") or prepare_cleanup(component, confirm=confirm)
            before = {
                "component_ids": [item["id"] for item in components],
                "source_commits": {item["id"]: item["source_commit"] for item in components},
                "source_commit": component["source_commit"],
                "plan_digest": self.plan["plan_digest"],
            }
            if merge_intent is not None:
                before["merge_intent"] = merge_intent
            if cleanup_intent is not None:
                before["cleanup_intent"] = cleanup_intent
            context.components = tuple(components)
            context.operation_intent = merge_intent or cleanup_intent
            context.phase_callback = None
            if action == "cleanup":
                def record_cleanup_phase(name: str, result: dict[str, Any]) -> None:
                    self.store.append(
                        "cleanup_phase",
                        record_patch={"steps": {step_id: {"cleanup_phases": {name: result}}}},
                        step_id=step_id,
                        phase=name,
                    )
                context.phase_callback = record_cleanup_phase
            context.stage_evidence = {
                item["id"]: record.get("steps", {}).get(f"stage:{item['id']}", {}).get("after", {})
                for item in self._components()
            }
            context.build_evidence = {
                item["id"]: record.get("steps", {}).get(f"build:{item['id']}", {}).get("after", {})
                for item in self._components()
            }
            context.executor = self._executor_override or executor_for_component(
                component, self.installation_config, self.executor, action=action
            )
            step_lock = locks.for_component(component["id"], action) if locks else None
            if step_lock:
                context.executor = step_lock.guarded(context.executor)
            step_scope = step_lock.hold_scope(f"{action}:{step_id}") if step_lock else nullcontext()
            try:
                with step_scope:
                    if step_lock:
                        step_lock.assert_owned()
                    self.store.step_started(step_id, before=before)
                    after = fn(component, context)
                    if step_lock:
                        step_lock.assert_owned()
                    safe_after = redact(after)
                    self.store.step_finished(step_id, after=safe_after)
            except Exception as exc:
                external_effect = "unknown" if (
                    isinstance(exc, UncertainRemoteEffect)
                    or scope in {"deploy", "adopt", "recover"}
                    or action == "merge"
                ) else False
                try:
                    if step_lock:
                        step_lock.assert_owned()
                    self.store.step_failed(step_id, error=str(exc), after={"external_effect": external_effect})
                except ReleaseError:
                    pass
                raise
            for item in components:
                results[item["id"]] = safe_after
        return {"release_id": self.plan["release_id"], "action": action, "results": results}

    def inspect(self) -> dict[str, Any]:
        context = self._context()
        values = {}
        for component in self._components():
            context.executor = self._executor_override or executor_for_component(
                component, self.installation_config, self.executor, action="inspect"
            )
            values[component["id"]] = adapter_for(component).inspect(component, context)
        # Inspection is intentionally read-only and does not append to the
        # release journal.  It may run concurrently with a mutating worker;
        # only lifecycle actions own the installation mutation lock.
        return values

    def check(self) -> dict[str, Any]:
        return self._run("check", lambda component, context: adapter_for(component).check(component, context))

    def build(self) -> dict[str, Any]:
        return self._run("build", lambda component, context: adapter_for(component).build(component, context))

    def stage(self) -> dict[str, Any]:
        return self._run("stage", lambda component, context: adapter_for(component).stage(component, context))

    def merge(self, *, confirm: bool) -> dict[str, Any]:
        return self._run(
            "merge",
            lambda component, context: merge_component(
                component, confirm=confirm, intent=context.operation_intent
            ),
            scope="merge",
            confirm=confirm,
        )

    def deploy(self, *, confirm: bool) -> dict[str, Any]:
        return self._run("deploy", lambda component, context: adapter_for(component).deploy_group(list(context.components) or [component], context) if adapter_for(component).runtime else {"deployed": False, "reason": "no runtime deployment"}, scope="deploy", confirm=confirm)

    def adopt(self, *, confirm: bool) -> dict[str, Any]:
        selected = set()
        for item in self.plan.get("adoption", []):
            if isinstance(item, dict) and item.get("component"):
                selected.add(item["component"])
        def apply(component: dict[str, Any], context: AdapterContext) -> Any:
            if component["id"] not in selected:
                return {"adopted": False, "reason": "not explicitly selected in frozen plan"}
            return adapter_for(component).adopt(component, context)
        return self._run("adopt", apply, scope="adopt", confirm=confirm)

    def status(self) -> dict[str, Any]:
        return self.store.load()

    def resume(self, *, confirm: bool = False) -> dict[str, Any]:
        initial_record = self.store.load()
        initial_running = [
            step_id
            for step_id, value in initial_record.get("steps", {}).items()
            if value.get("status") == "running"
            or value.get("status") == "failed" and value.get("after", {}).get("external_effect") == "unknown"
        ]
        actions = {step_id.split(":", 1)[0] for step_id in initial_running}
        locks = self._destination_locks(actions or {"check"})
        with locks:
            lock = locks.primary
            lock.assert_owned()
            record = self.store.load()
            running = [
                (step_id, value)
                for step_id, value in record.get("steps", {}).items()
                if value.get("status") == "running"
                or value.get("status") == "failed" and value.get("after", {}).get("external_effect") == "unknown"
            ]
            if running:
                context = self._context()
                context.executor = lock.guarded(context.executor)
                for step_id, value in running:
                    components = self._step_components(step_id, value.get("before", {}))
                    component = components[0]
                    context.components = tuple(components)
                    context.stage_evidence = {
                        item["id"]: record.get("steps", {}).get(f"stage:{item['id']}", {}).get("after", {})
                        for item in self._components()
                    }
                    context.build_evidence = {
                        item["id"]: record.get("steps", {}).get(f"build:{item['id']}", {}).get("after", {})
                        for item in self._components()
                    }
                    action = step_id.split(":", 1)[0]
                    raw_executor = self._executor_override or executor_for_component(
                        component, self.installation_config, self.executor, action=action
                    )
                    step_lock = locks.for_component(component["id"], action)
                    context.executor = step_lock.guarded(raw_executor)
                    with step_lock.hold_scope("resume:" + step_id):
                        if action == "merge":
                            intent = value.get("before", {}).get("merge_intent")
                            if not isinstance(intent, dict):
                                raise ReconciliationRequired(f"{step_id}: merge intent is missing; inspect the remote target before retrying")
                            try:
                                observed = reconcile_merge(component, intent)
                            except ReleaseError as exc:
                                observed = {"safe_to_retry": False, "reason": f"merge reconciliation failed: {exc}"}
                        elif action == "cleanup":
                            intent = value.get("before", {}).get("cleanup_intent")
                            if not isinstance(intent, dict):
                                raise ReconciliationRequired(f"{step_id}: cleanup intent is missing; inspect cleanup state before retrying")
                            try:
                                observed = reconcile_cleanup(component, intent)
                            except ReleaseError as exc:
                                observed = {"safe_to_retry": False, "reason": f"cleanup reconciliation failed: {exc}"}
                        else:
                            observed = adapter_for(component).reconcile(component, context, action, value.get("before"))
                        if observed.get("already_succeeded"):
                            step_lock.assert_owned()
                            self.store.step_finished(step_id, after=observed)
                            continue
                        if not observed.get("safe_to_retry"):
                            raise ReconciliationRequired(f"{step_id}: {observed.get('reason', 'external state is uncertain')}")
                        step_lock.assert_owned()
                        self.store.append("reconciled", record_patch={"steps": {step_id: {"status": "reconciled", "reconciliation": observed}}}, step_id=step_id)
            return self.status()

    def recover(self, *, confirm: bool) -> dict[str, Any]:
        return self._run("recover", lambda component, context: adapter_for(component).recover(component, context), scope="recover", confirm=confirm)

    def cleanup(self, *, confirm: bool) -> dict[str, Any]:
        return self._run(
            "cleanup",
            lambda component, context: cleanup_component(
                component,
                confirm=confirm,
                intent=context.operation_intent,
                phase_callback=context.phase_callback,
            ),
            scope="cleanup",
            confirm=confirm,
        )


def create_plan(manifest_path: Path, *, records_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    worker = ReleaseWorker.from_manifest_path(manifest_path, records_root=records_root)
    record = worker.initialize()
    return worker.plan, record
