"""The configured browsers an agent can name: the catalog, an exact selection, and the owner's viewer for it.

Three trusted sources are joined here and nothing else:

* ``browser-surfaces.json`` (schema 2, ``bazarlyk.service.browser_surfaces``): each surface's label and viewer URL, which
  agent's own browsers a Global surface belongs to and which business holds a business browser;
* a sandboxed business's ready owner viewer (the sandbox viewer service, never the registry);
* the operator's desktop configs (``/etc/qorqut-browser-desktops/<name>.json``, read with ``desktop_session.load``), which are the
  only place a managed desktop's loopback CDP port is named. The viewer URL is never a CDP source.

A browser is named by ``(id, business_id)``: ``business_id`` is null for a Global surface, or the owning business. The sandbox
business browser has the one synthetic id ``business-sandbox-browser``, so its business disambiguates it. Naming a browser grants
nothing; control is the existing ``browser_turn`` path, with its own authorization."""

from __future__ import annotations

from pathlib import Path
import sys
import types

from qorqut_operations.business_lifecycle import active_business_ids
from qorqut_operations.contracts import DomainError

from bazarlyk.agents.global_scope import active_business_participant
from bazarlyk.service import browser_surfaces

DESKTOP_DIRECTORY = Path("/etc/qorqut-browser-desktops")
# The managed Global desktops and the operator config each is run from (``desktop_session.py``); order is the catalog order.
DESKTOP_TARGETS = {"ordubashy-browser": "ordubashy", "outlook-browser": "outlook"}
DESKTOP_KEY = "desktop:"
SESSION_MODULE = "qorqut_desktop_session"


def unavailable(message="The browser is unavailable"):
    return DomainError("not_found", message)


class BrowserTargets:
    def __init__(self, runtime, *, desktops=DESKTOP_DIRECTORY):
        self.runtime, self.desktops = runtime, Path(desktops)
        self._session_module = None

    # ----- configuration ---------------------------------------------------------------------------------------------

    def registry(self):
        """The parsed surface registry, or ``None`` when the operator has not configured one. A defective file raises."""
        return browser_surfaces.read(self.runtime.paths)

    @staticmethod
    def _claimed(registry, surface_id):
        """The business a registry surface belongs to, or ``None`` for a Global surface."""
        return next((business for business, binding in registry["businesses"].items() if binding["browser"] == surface_id), None)

    def _sandbox_viewer(self, uow, business):
        """The ready viewer of a sandboxed business's browser, or ``None``. Read only; it starts nothing."""
        sandbox = uow.repository("business_sandboxes").get(business["id"])
        if sandbox is None:
            return None, False
        viewers = getattr(getattr(self.runtime, "previews", None), "browser_viewers", None)
        config = viewers.config_loader(self.runtime.paths) if viewers is not None else None
        configured = config is not None and config.viewer_port is not None
        return (viewers.link(business["id"]) if configured and sandbox["state"] == "ready" else None), True

    # ----- resolution ------------------------------------------------------------------------------------------------

    def resolve(self, uow, principal_id, surface_id, business_id, *, viewer=False):
        """The configured browser ``(surface_id, business_id)`` as ``principal_id`` reaches it, or ``not_found``. With
        ``viewer`` the viewer URL is part of the answer and a browser without a ready one is unavailable."""
        if business_id is None:
            registry = self.registry()
            surface = registry and registry["surfaces"].get(surface_id)
            if not surface or surface["kind"] != "browser" or self._claimed(registry, surface_id) is not None:
                raise unavailable()
            found = {"id": surface_id, "label": surface["label"], "business_id": None, "viewer_url": surface["viewer_url"]}
            if "width" in surface:
                found.update(width=surface["width"], height=surface["height"])
            return found
        business = uow.connection.execute("SELECT id, name FROM businesses WHERE id=? AND archived_at IS NULL", (business_id,)).fetchone()
        if business is None or not active_business_participant(uow.connection, business_id, principal_id):
            raise unavailable()
        url, sandboxed = self._sandbox_viewer(uow, business)
        if sandboxed:
            if surface_id != browser_surfaces.SANDBOX_SURFACE_ID or (viewer and not url):
                raise unavailable()
            return {"id": surface_id, "label": business["name"], "business_id": business_id, "viewer_url": url}
        registry = self.registry()
        binding = registry and registry["businesses"].get(business_id)
        surface = binding and binding["browser"] == surface_id and registry["surfaces"].get(surface_id)
        if not surface or surface["kind"] != "browser":
            raise unavailable()
        found = {"id": surface_id, "label": surface["label"], "business_id": business_id, "viewer_url": surface["viewer_url"]}
        if "width" in surface:
            found.update(width=surface["width"], height=surface["height"])
        return found

    def catalog(self, uow, principal_id):
        """Every browser the agent can name, as ``{id, label, business_id}``: the managed Global desktops the registry configures,
        then each reachable business's one browser. Empty when no registry is configured."""
        registry = self.registry()
        entries = []
        if registry is not None:
            for surface_id in DESKTOP_TARGETS:
                surface = registry["surfaces"].get(surface_id)
                if surface and surface["kind"] == "browser" and self._claimed(registry, surface_id) is None:
                    entries.append({"id": surface_id, "label": surface["label"], "business_id": None})
        active = active_business_ids(uow.connection)
        for business in uow.connection.execute("SELECT id, name FROM businesses WHERE archived_at IS NULL ORDER BY name, id"):
            if business["id"] not in active or not active_business_participant(uow.connection, business["id"], principal_id):
                continue
            if uow.repository("business_sandboxes").get(business["id"]) is not None:
                entries.append({"id": browser_surfaces.SANDBOX_SURFACE_ID, "label": business["name"], "business_id": business["id"]})
            elif registry is not None and business["id"] in registry["businesses"]:
                surface = registry["surfaces"][registry["businesses"][business["id"]]["browser"]]
                entries.append({"id": surface["id"], "label": surface["label"], "business_id": business["id"]})
        return entries

    def own_surfaces(self, registry, principal_id):
        """The browsers the registry binds to this agent as its own."""
        binding = registry["agents"].get(principal_id) if registry else None
        return set(binding["browsers"]) if binding else set()

    # ----- control targets ------------------------------------------------------------------------------------------

    def desktop_target(self, browser_key, runtime_dir):
        """The CDP target of a managed Global desktop other than the caller's own configured browser: its loopback port from the
        operator's session config (loaded with the helper's own validator) and the installation's browser runtime."""
        surface_id = browser_key[len(DESKTOP_KEY):]
        name = DESKTOP_TARGETS.get(surface_id)
        if name is None or not browser_key.startswith(DESKTOP_KEY):
            raise DomainError("forbidden", "The browser target is not a configured managed desktop")
        module = self._session()
        try:
            session = module.load(str(self.desktops / f"{name}.json"))
        except module.ConfigError:
            raise DomainError("operation_unavailable", "The selected browser desktop is not configured") from None
        if session.name != name:
            raise DomainError("operation_unavailable", "The selected browser desktop configuration names another session")
        return {"cdp_endpoint": f"http://127.0.0.1:{session.cdp_port}", "generation": "configured", "instance": browser_key,
                "runtime_dir": runtime_dir, "browser_key": browser_key}

    def _session(self):
        """``tools/browser/desktop_session.py`` from the explicit app component, executed in memory so that nothing is written
        beside an immutable source."""
        if self._session_module is None:
            from bazarlyk.installation import component
            path = component(self.runtime.paths, "app") / "tools/browser/desktop_session.py"
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                raise DomainError("operation_unavailable", "The browser desktop helper is unavailable") from None
            module = types.ModuleType(SESSION_MODULE)
            module.__file__ = str(path)
            # The helper defines dataclasses, which resolve their module through ``sys.modules`` while the class is built.
            sys.modules[SESSION_MODULE] = module
            try:
                exec(compile(source, str(path), "exec"), module.__dict__)
            except BaseException:
                sys.modules.pop(SESSION_MODULE, None)
                raise
            self._session_module = module
        return self._session_module
