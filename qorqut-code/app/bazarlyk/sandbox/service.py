"""Explicit trusted service composition for business execution environments.

Assembly creates no process, socket, thread, directory or provisioning intent.
The worker consumes committed intents only after normal service startup.
"""

from pathlib import Path
import re
from types import SimpleNamespace

from bazarlyk.browser.socket import BrowserBinding
from qorqut_operations.contracts import DomainError
from .browser import BrowserRuntime, SandboxBrowser
from .config import BusinessPaths, HostConfig, business_identity
from .docker import trusted_host_docker
from .lifecycle import SandboxLifecycle
from .linux import LinuxNetwork
from .listener import BusinessAgentListeners
from .readiness import AgentReadiness
from .runtime import DockerEnvironmentDriver
from .terminal import environment_fingerprint
from .worker import SandboxAdministration, SandboxWorker


class BusinessSandboxService:
    def __init__(self, runtime, *, docker=None, network=None, listeners=None,
                 agent_readiness=None, browser_factory=SandboxBrowser,
                 config_loader=HostConfig.read, seccomp_profile=None):
        self.runtime = runtime
        self.docker = docker if docker is not None else trusted_host_docker()
        self.network = network if network is not None else LinuxNetwork(self.docker, command=("sudo", "-n"))
        self.listeners = listeners if listeners is not None else BusinessAgentListeners(runtime)
        self.agents = agent_readiness if agent_readiness is not None else AgentReadiness(runtime.resolver, docker=self.docker)
        self.browser_factory, self._browsers = browser_factory, {}
        self.config_loader = config_loader
        self.seccomp_profile = Path(seccomp_profile) if seccomp_profile else (
            Path(__file__).with_name("browser-seccomp-moby-29.1.3.json"))
        self.driver = DockerEnvironmentDriver(docker=self.docker, network=self.network,
            readiness=self.readiness, browser_runtime=self.browser_runtime, before_stop=self.close_browser)
        self.lifecycle = SandboxLifecycle(runtime.database, runtime.paths, self.driver, config_loader=config_loader)
        self.lifecycle.set_rollback_boundary(stop_listener=self.listeners.stop_business,
                                              profile_lock_unheld=SandboxBrowser.profile_lock_unheld,
                                              require_authority=runtime.require_authority)
        self.administration = SandboxAdministration(self.lifecycle)
        self.worker = SandboxWorker(self.lifecycle, require_authority=runtime.require_authority)

    def browser_runtime(self, config):
        return BrowserRuntime(config.browser_image, self.seccomp_profile)

    def browser(self, image):
        browser = self._browsers.get(image)
        if browser is None:
            browser = self.browser_factory(self.docker, BrowserRuntime(image, self.seccomp_profile))
            self._browsers[image] = browser
        return browser

    def readiness(self, business, paths, config, environment):
        # Listener first: preparation and native probes must reach the same
        # business-bound endpoint that ordinary terminals will use.
        self.listeners.ensure(business, config, environment)
        if self.agents(business, paths, config, environment) is not True:
            raise DomainError("environment_unhealthy", "Registered agent preparation did not pass")
        if self.browser(config.browser_image).browser_readiness(business, paths, config, environment) is not True:
            raise DomainError("environment_unhealthy", "Business browser readiness did not pass")
        return True

    def close_browser(self, business, paths, current):
        labels = current.get("Config", {}).get("Labels", {})
        try:
            generation = int(labels["qorqut.generation"])
            image = current["Config"]["Image"]
        except (KeyError, TypeError, ValueError):
            raise DomainError("environment_conflict", "Observed browser identity is incomplete") from None
        environment = SimpleNamespace(ownership=self.driver.ownership(paths), generation=generation,
                                      browser_id=current["Id"])
        # Uses the previous image's loopback close helper; no replacement image,
        # proxy lookup or current HostConfig is needed for an emergency stop.
        if self.browser(image).close_observed(business, paths, environment) is not True:
            raise DomainError("environment_conflict", "Browser profile writer did not close")

    def _observed_browser(self, business_id):
        """Read one committed generation and inspect it without reconciling it."""
        business_identity(business_id)
        with self.runtime.database.read() as uow:
            business = uow.repository("businesses").get(business_id, required=True)
            state = uow.repository("business_sandboxes").get(business_id)
        if (state is None or state.get("state") != "ready" or state.get("desired_state") != "running"
                or state.get("observed_generation") != state.get("desired_generation")):
            raise DomainError("operation_unavailable", "Sandbox browser generation is not committed ready", retryable=True)
        generation, workstation_id = state.get("observed_generation"), state.get("environment_id")
        if (type(generation) is not int or generation < 1
                or not isinstance(workstation_id, str) or not re.fullmatch(r"[a-f0-9]{64}", workstation_id)):
            raise DomainError("environment_conflict", "Committed sandbox browser generation is incomplete")
        paths = BusinessPaths.registered(self.runtime.paths, business)
        config = self.config_loader(self.runtime.paths)
        if not isinstance(config, HostConfig) or not config.enabled:
            raise DomainError("configuration_missing", "Sandbox browser runtime configuration is unavailable")
        ownership = self.driver.ownership(paths)
        fingerprint = environment_fingerprint(config, business, paths, proxy_loader=self.driver.proxy_loader)
        workstation = self.docker.owned("container", workstation_id, ownership)
        browser_name, network_name = (paths.resource_prefix + "-g" + str(generation) + "-browser",
                                      paths.resource_prefix + "-browser")
        browser = self.docker.owned("container", browser_name, ownership)
        network = self.docker.owned("network", network_name, ownership)
        labels = (browser or {}).get("Config", {}).get("Labels")
        agent_labels = (workstation or {}).get("Config", {}).get("Labels")
        attached = (browser or {}).get("NetworkSettings", {}).get("Networks", {})
        members = (network or {}).get("Containers", {})
        browser_id, workstation_name, observed_browser_name = ((browser or {}).get("Id"),
                                                                (workstation or {}).get("Name"),
                                                                (browser or {}).get("Name"))
        if (not isinstance(labels, dict) or not isinstance(agent_labels, dict)
                or not isinstance(attached, dict) or not isinstance(members, dict)
                or workstation is None or workstation.get("Id") != workstation_id
                or not isinstance(workstation_name, str)
                or workstation_name.lstrip("/") != paths.resource_prefix + "-g" + str(generation) + "-agent"
                or not workstation.get("State", {}).get("Running")
                or agent_labels.get("qorqut.role") != "agent" or agent_labels.get("qorqut.generation") != str(generation)
                or browser is None or not isinstance(browser_id, str) or not re.fullmatch(r"[a-f0-9]{64}", browser_id)
                or not isinstance(observed_browser_name, str) or observed_browser_name.lstrip("/") != browser_name
                or not browser.get("State", {}).get("Running")
                or labels.get("qorqut.role") != "browser" or labels.get("qorqut.generation") != str(generation)
                or not isinstance(labels.get("qorqut.plan"), str) or not labels["qorqut.plan"]
                or labels.get("qorqut.plan") != fingerprint or agent_labels.get("qorqut.plan") != fingerprint
                or browser.get("HostConfig", {}).get("NetworkMode") != network_name
                or network is None or network.get("Internal") is not True
                or network_name not in attached or browser_id not in members
                or workstation.get("Config", {}).get("Image") != config.workstation_image):
            raise DomainError("environment_conflict", "Observed sandbox browser is not its committed ready generation")
        environment = SimpleNamespace(ownership=ownership, generation=generation, browser_id=browser_id,
                                      workstation_id=workstation_id, plan_fingerprint=fingerprint)
        return business, paths, config, environment

    def agent_origin(self, business_id, binding):
        """Return only the listener belonging to this exact observed environment."""
        from ipaddress import ip_interface
        _business, paths, config, environment = self._observed_browser(business_id)
        with self.listeners._guard:
            current = self.listeners._servers.get(business_id)
        if (not current or binding.business_id != business_id or binding.browser_id != environment.browser_id
                or binding.generation != environment.generation):
            raise DomainError("environment_conflict", "Business browser listener binding is stale")
        saved, server = current
        gateway = self.docker.owned("container", paths.resource_prefix + f"-g{environment.generation}-gateway",
                                    environment.ownership)
        network = self.docker.owned("network", paths.resource_prefix + "-agent", environment.ownership)
        try:
            labels = gateway["Config"]["Labels"]
            peers = tuple(str(ip_interface(network["Containers"][identity]["IPv4Address"]).ip)
                          for identity in (environment.workstation_id, gateway["Id"]))
            valid = (saved.business_id == business_id and saved.generation == environment.generation
                and saved.workstation_id == environment.workstation_id and saved.gateway_id == gateway["Id"]
                and gateway["State"]["Running"] and labels.get("qorqut.role") == "gateway"
                and labels.get("qorqut.generation") == str(environment.generation)
                and labels.get("qorqut.plan") == environment.plan_fingerprint
                and network.get("Internal") is True and tuple(saved.peers) == peers
                and saved.address == network["IPAM"]["Config"][0]["Gateway"]
                and saved.port == config.agent_api_port and server.running)
        except (KeyError, TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise DomainError("environment_conflict", "Business browser listener is outside its current environment")
        return f"http://{saved.address}:{saved.port}"

    def observed_browser_binding(self, business_id) -> BrowserBinding:
        """Return an exact lock-owned CDP binding; never launch or reconnect Chrome."""
        business, paths, config, environment = self._observed_browser(business_id)
        return self.browser(config.browser_image).observed_browser_binding(business, paths, config, environment)

    def account_proxy(self, business_id):
        """Resolve host account helpers through the owning, verified gateway."""
        from .network import ACCOUNT_PROXY
        with self.runtime.database.read() as uow:
            if uow.repository("business_sandboxes").get(business_id) is None:
                return None  # Explicit legacy host execution has no sandbox route.
        _business, _paths, _config, environment = self._observed_browser(business_id)
        binding = SimpleNamespace(business_id=business_id, browser_id=environment.browser_id,
                                  generation=environment.generation)
        with self.listeners._guard:
            self.agent_origin(business_id, binding)
            saved, _server = self.listeners._servers[business_id]
            return f"http://{saved.peers[1]}:{ACCOUNT_PROXY}"

    def owns_browser_lock(self, business_id) -> bool:
        """Whether the cached service controller owns this current profile's flock."""
        try:
            _business, paths, config, environment = self._observed_browser(business_id)
            return self.browser(config.browser_image).owns_profile_lock(paths, environment)
        except (DomainError, KeyError, TypeError):
            return False

    def start(self):
        self.runtime.require_authority()
        self.worker.start()

    def stop(self, *, timeout=5):
        # A reconciler still in flight can require its listener. Keep listeners
        # alive if bounded join fails, and retain all business processes/files.
        self.worker.stop(timeout=timeout)
        self.listeners.stop(timeout=timeout)
