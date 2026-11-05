"""Native agent readiness checks performed by the trusted host reconciler."""

from __future__ import annotations

import json
from types import SimpleNamespace

from bazarlyk.bootstrap.preparation import prepare_agent
from bazarlyk.composition.locations import agent_workspace
from qorqut_operations.contracts import DomainError, Principal
from .docker import Docker, Ownership, trusted_host_docker
from .tmux import BOOT_COMMAND, DockerTmuxBroker


# This script is sent as a fixed command to the observed workstation. Its JSON
# stdin contains only a registration path and endpoint; the credential is read
# by the script inside the container and never crosses the Docker argv.
_PROBE = r'''
import json, sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, build_opener

from bazarlyk.bootstrap.client import auth_headers, load_registration
import bazarlyk.bootstrap.claude_session
import bazarlyk.bootstrap.codex_session
import bazarlyk.bootstrap.mcp_headers
import bazarlyk.bootstrap.transcript_snapshot

opener = build_opener(ProxyHandler({}))

def fail(code, status):
    # Status-only diagnostics are safe for the trusted Docker boundary and
    # keep URLs, response bodies, credentials, and server diagnostics private.
    print("QORQUT_PROBE_FAIL:%s:%s" % (code, status), file=sys.stderr)
    raise SystemExit(code)

def call(base, authorization, path, method, body=None, session=None):
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    request = Request(base + path, data=data, method=method, headers={
        "Authorization": authorization, "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json", "Host": urlsplit(base).netloc})
    if session:
        request.add_header("Mcp-Session-Id", session)
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read(1_000_001)
    except HTTPError as error:
        return error.code, dict(error.headers), error.read(1_000_001)
    except (OSError, URLError):
        return 599, {}, b""

payload = json.load(sys.stdin)
registration = load_registration(Path(payload["registration"]))
authorization = auth_headers(registration)["Authorization"]
base = payload["endpoint"].rstrip("/")
status, _, data = call(base, authorization, "/api/agent/readiness", "POST", {
    "registration_id": registration["registration_id"]})
if status != 200 or not json.loads(data).get("data", {}).get("ready"):
    fail(10, status)
status, _, data = call(base, authorization, "/api/root/agents", "GET")
if status != 404:
    fail(11, status)
status, headers, data = call(base, authorization, "/mcp", "POST", {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "qorqut-readiness", "version": "1"}}})
mcp_session = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
if status != 200 or not mcp_session or not json.loads(data).get("result", {}).get("serverInfo"):
    fail(12, status)
status, _, _ = call(base, authorization, "/mcp", "POST", {
    "jsonrpc": "2.0", "method": "notifications/initialized"}, mcp_session)
if status not in (200, 202):
    fail(15, status)
status, _, data = call(base, authorization, "/mcp", "POST", {
    "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, mcp_session)
if status != 200 or not json.loads(data).get("result", {}).get("tools"):
    fail(13, status)
status, _, _ = call(base, authorization, "/mcp", "DELETE", None, mcp_session)
if status not in (200, 202, 204):
    fail(14, status)
'''


class AgentReadiness:
    """Prepare and probe every active business registration in one workstation."""

    def __init__(self, resolver, *, docker: Docker | None = None):
        self.resolver = resolver
        self.docker = docker or trusted_host_docker()
        self._verified_versions = {}

    @staticmethod
    def _ownership(root, business_id):
        import hashlib
        return Ownership(hashlib.sha256(str(root).encode()).hexdigest()[:20], business_id)

    def _probe(self, broker, endpoint, registration):
        command = (BOOT_COMMAND[0], "-I", "-c", _PROBE)
        # DockerTmuxBroker revalidates the exact observed ID, ownership labels,
        # role, generation and running state immediately before exec.
        broker._exec(*command,
            input=json.dumps({"endpoint": endpoint, "registration": str(registration)}, separators=(",", ":")))

    def __call__(self, business, paths, config, environment):
        if getattr(config, "enabled", True) is not True:
            raise DomainError("configuration_missing", "Business sandbox execution is disabled")
        ownership = self._ownership(paths.root, paths.business_id)
        workstation_id = environment.workstation_id
        broker = DockerTmuxBroker(self.docker, ownership, workstation_id, environment.generation, SimpleNamespace())
        # The driver verifies the immutable image and confinement before every
        # readiness call. Re-running heavy bundled CLI binaries on each health
        # pass can turn host I/O contention into loss of an otherwise ready
        # browser/MCP binding. Cache only successful version observations for
        # this exact installation, container and generation, never agent grants.
        current = broker._running()
        cacheable = current.get("HostConfig", {}).get("ReadonlyRootfs") is True
        if not cacheable:
            self._verified_versions.pop(business["id"], None)
        key = (ownership.installation, workstation_id, environment.generation)
        cached = self._verified_versions.get(business["id"])
        if cached is not None and cached[0] == key:
            versions = dict(cached[1])
        else:
            versions = broker.installed_versions()
            if cacheable and versions and all(versions.values()):
                self._verified_versions[business["id"]] = (key, dict(versions))
        with self.resolver.database.read() as uow:
            rows = [dict(row) for row in uow.connection.execute(
                "SELECT * FROM agent_profiles WHERE home_business_id=? AND status='active' ORDER BY name",
                (business["id"],)).fetchall()]
        for row in rows:
            profile = self.resolver.profile(Principal(row["agent_id"], "agent"))[0]
            workspace = agent_workspace(paths.instance, row["agent_id"], business)
            registration = workspace / ".bazarlyk/registration.json"
            prepare_agent(self.resolver, Principal(profile.agent_id, "agent"), endpoint=environment.agent_api_url,
                          versions=versions, interpreter=BOOT_COMMAND[0], interpreter_validator=broker.verify_interpreter)
            self._probe(broker, environment.agent_api_url, registration)
        return True
