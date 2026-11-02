"""The configured browsers an agent can name: the catalog, an exact selection, and the owner's viewer for it.

Two trusted sources are joined here and nothing else:

* ``browser-surfaces.json`` (schema 2, ``bazarlyk.service.browser_surfaces``): each surface's label and viewer URL and which
  agent's own browsers a Global surface belongs to;
* the operator's desktop configs (``/etc/qorqut-browser-desktops/<name>.json``, read with ``desktop_session.load``), which are the
  only place a managed desktop's loopback CDP port is named. The viewer URL is never a CDP source.

Every browser is Global and is named by its surface ``id``; projects have no browsers, so a reference that names a project
(``business_id``) resolves to nothing. Naming a browser grants nothing; control is the existing ``browser_turn`` path, with
its own authorization."""

from __future__ import annotations

from pathlib import Path
import sys
import types

from qorqut_operations.contracts import DomainError

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
        from bazarlyk.native_paths import root, folder_root
        if (root() is not None or folder_root() is not None) and desktops == DESKTOP_DIRECTORY:
            desktops = runtime.paths.instance_root / "config/browser-desktops"
        self.runtime, self.desktops = runtime, Path(desktops)
        self._session_module = None

    # ----- configuration ---------------------------------------------------------------------------------------------

    def registry(self):
        """The parsed surface registry, or ``None`` when the operator has not configured one. A defective file raises."""
        return browser_surfaces.read(self.runtime.paths)

    # ----- resolution ------------------------------------------------------------------------------------------------

    def resolve(self, uow, principal_id, surface_id, business_id, *, viewer=False):
        """The configured Global browser ``surface_id`` as ``principal_id`` reaches it, or ``not_found``. A project
        (``business_id``) has no browser. With ``viewer`` the viewer URL is part of the answer."""
        if business_id is not None:
            raise unavailable()
        registry = self.registry()
        surface = registry and registry["surfaces"].get(surface_id)
        if not surface or surface["kind"] != "browser":
            raise unavailable()
        found = {"id": surface_id, "label": surface["label"], "business_id": None, "viewer_url": surface["viewer_url"]}
        if "width" in surface:
            found.update(width=surface["width"], height=surface["height"])
        return found

    def catalog(self, uow, principal_id):
        """Every browser the agent can name, as ``{id, label}``: the managed Global desktops the registry configures. Empty
        when no registry is configured."""
        if sys.platform != 'linux':
            return []
        registry = self.registry()
        entries = []
        if registry is not None:
            for surface_id in DESKTOP_TARGETS:
                surface = registry["surfaces"].get(surface_id)
                if surface and surface["kind"] == "browser":
                    entries.append({"id": surface_id, "label": surface["label"]})
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
        if sys.platform != 'linux':
            raise DomainError("operation_unavailable", "Managed X11 browser desktops are supported only on Linux; "
                              "use the agent's headless browser on this platform")
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
