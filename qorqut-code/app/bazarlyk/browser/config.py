"""Public browser settings; proxy credentials never enter operational records.

Two identities share one ``businesses.config_json.browser`` block. The legacy
block (no ``mode``) describes the headless host Chrome and its relay. The
explicit ``isolated-desktop-v1`` mode describes one persistent headed Chrome in a
loopback-only network namespace (see ``bazarlyk.browser.isolated``). Only an
explicit mode change moves a business between them.
"""

import ipaddress
import fcntl
import json
import socket
from urllib.parse import urlsplit

from bazarlyk.bootstrap.client import path_has_symlink
from bazarlyk.composition.locations import business_hq
from qorqut_operations.contracts import DomainError, new_id

PORTS = range(19400, 20400)
GENERATED = {"cdp_port", "user_data_dir", "downloads_dir", "credentials_ref", "generation"}
ISOLATED = "isolated-desktop-v1"
LEGACY_MODE = "legacy"
LEGACY_KEYS = GENERATED | {"enabled", "proxy_server"}
ISOLATED_KEYS = LEGACY_KEYS | {"mode", "expected_exit_ip"}
_PROPOSAL_KEYS = ISOLATED_KEYS


def validate_proxy(value):
    try:
        parsed = urlsplit(value)
        valid = (isinstance(value, str) and len(value) <= 2048
                 and parsed.scheme in {"http", "https", "socks5"}
                 and parsed.hostname and parsed.port and not parsed.username and not parsed.password
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                 and not any(c.isspace() or ord(c) < 32 for c in value))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise DomainError("validation_error", "Browser proxy must be an HTTP, HTTPS or SOCKS5 server with an explicit port, without credentials or a path")
    return value.rstrip("/")


def numeric_ipv4(value, *, what):
    """One canonical, globally routable IPv4 address; never a name or alias."""
    try:
        address = ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, ValueError, TypeError):
        raise DomainError("validation_error", f"{what} must be a numeric IPv4 address, not a name or alias") from None
    if str(address) != value or not address.is_global:
        raise DomainError("validation_error", f"{what} must be a canonical, globally routable IPv4 address")
    return str(address)


def validate_isolated_proxy(value):
    """The isolated mode supports one numeric, authenticated-HTTP upstream."""
    server = validate_proxy(value)
    parsed = urlsplit(server)
    if parsed.scheme != "http":
        raise DomainError("validation_error", "The isolated browser mode supports an HTTP proxy only")
    address = numeric_ipv4(parsed.hostname, what="The isolated browser proxy host")
    canonical = f"http://{address}:{parsed.port}"
    if server != canonical:
        raise DomainError("validation_error", "The isolated browser proxy must be written as http://<IPv4>:<port>")
    return canonical


def validate_exit_ip(value):
    return numeric_ipv4(value, what="expected_exit_ip")


def _normalized(value):
    try:
        address = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        return None
    return str(getattr(address, "ipv4_mapped", None) or address)


def reserved_ips(browser):
    """Normalized IPs one browser block claims: its exit IP and a literal-IP proxy host."""
    claims = set()
    if not isinstance(browser, dict):
        return claims
    claims.add(_normalized(browser.get("expected_exit_ip")))
    try:
        claims.add(_normalized(urlsplit(browser.get("proxy_server")).hostname))
    except (ValueError, TypeError, AttributeError):
        pass
    claims.discard(None)
    return claims


def assigned_elsewhere(connection, business_id, ips):
    """Which of these IPs another business's browser block (enabled or not) already holds."""
    clashes = {}
    if not ips:
        return clashes
    for identity, raw in connection.execute("SELECT id, config_json FROM businesses WHERE id<>?", (business_id,)):
        try:
            other = json.loads(raw).get("browser") if isinstance(raw, str) else (raw or {}).get("browser")
        except (ValueError, AttributeError):
            continue
        for address in ips & reserved_ips(other):
            clashes[address] = identity
    return clashes


def assert_ip_unassigned(connection, business_id, browser, before=None):
    """Newly claimed IPs must be free everywhere; an unchanged claim is never re-litigated."""
    fresh = reserved_ips(browser) - reserved_ips(before)
    clashes = assigned_elsewhere(connection, business_id, fresh)
    if clashes:
        raise DomainError("configuration_conflict",
            "Proxy IP " + sorted(clashes)[0] + " is already assigned to another project browser")


def port_available(port):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def browser_state_directory(paths, business_id):
    """Per-business runtime state (lease, readiness, gateway, legacy profile) under the instance runtime directory."""
    return paths.runtime / "browsers" / business_id


def legacy_paths(paths, business_id):
    directory = browser_state_directory(paths, business_id)
    return {"user_data_dir": str(directory / "profile"), "downloads_dir": str(directory / "downloads"),
            "credentials_ref": "browser-proxy/" + business_id}


def isolated_paths(paths, business):
    """Profile, downloads and setup projection under the business's HQ, selected by its registered folder."""
    hq = business_hq(paths, business)
    base = hq / "browser"
    if base.is_symlink():
        raise DomainError("invalid_configuration", "Browser folders cannot follow symlinks")
    return {"hq": str(hq), "browser_dir": str(base), "user_data_dir": str(base / "profile"),
            "downloads_dir": str(base / "downloads"), "setup_path": str(base / "setup.json")}


def isolated_reference(business_id):
    return "browser-proxy/" + business_id + "-isolated"


def assert_service_stopped(paths, business_id):
    """A mode change never happens under a running, ready or leased service."""
    directory = browser_state_directory(paths, business_id)
    if not directory.exists():
        return
    if path_has_symlink(directory):
        raise DomainError("invalid_configuration", "Browser state cannot follow symlinks")
    if (directory / "ready.json").exists() or (directory / "gateway.json").exists():
        raise DomainError("environment_conflict", "Stop this project browser before changing its mode")
    lease = directory / "lease.json"
    try:
        if lease.exists() and json.loads(lease.read_text() or "{}"):
            raise DomainError("environment_conflict", "This project browser is leased; release it before changing its mode")
    except ValueError:
        raise DomainError("environment_conflict", "Browser ownership state needs operator recovery first") from None
    lock = directory / "service.lock"
    if lock.exists():
        if path_has_symlink(lock):
            raise DomainError("invalid_configuration", "Browser lock cannot follow symlinks")
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DomainError("environment_conflict", "This project browser service is running; stop it first") from None


def _assert_no_sandbox(connection, business_id):
    try:
        assigned = connection.execute("SELECT 1 FROM business_sandboxes WHERE business_id=?", (business_id,)).fetchone()
    except Exception:
        return
    if assigned is not None:
        raise DomainError("environment_conflict", "A project assigned to a sandbox cannot select the isolated desktop browser")


def prepare_config(value, before, *, paths, business_id, principal_kind, uow, delegated=False):
    """Merge this reserved block into otherwise replacement-style biz settings.

    A unique SQLite index arbitrates concurrent prepared allocations at apply;
    an occupied port is never silently reassigned after identity is persisted.
    """
    old = before.get("browser")
    if "browser" not in value:
        if old is not None:
            value["browser"] = old
        return
    proposed = value["browser"]
    if proposed == old:
        return
    if principal_kind != "human" and not delegated:
        raise DomainError("forbidden", "Browser identity and proxy settings require the owner or scoped Ordubashy")
    value["browser"] = prepare_settings(proposed, old, paths=paths, business_id=business_id, uow=uow)


def _generated_only_from(proposed, old, derived):
    for key in GENERATED & proposed.keys():
        if key == "generation":
            allowed = {old.get("generation")} - {None}
        else:
            allowed = ({old.get(key)} | {derived.get(key)}) - {None}
        if proposed[key] not in allowed:
            raise DomainError("validation_error", "Browser ports, paths, credential references and generations are automatically assigned")


def _allocate_port(uow):
    used = {row[0] for row in uow.connection.execute(
        "SELECT json_extract(config_json, '$.browser.cdp_port') FROM businesses")}
    port = next((port for port in PORTS if port not in used and port_available(port)), None)
    if port is None:
        raise DomainError("operation_unavailable", "No project browser port is available")
    return port


def prepare_settings(proposed, old, *, paths, business_id, uow):
    """Freeze validated settings without launching Chrome or writing credentials.

    Callers must enforce owner/Ordubashy authority or exact-change owner signoff.
    Absent ``mode`` keeps the block's current mode; ``mode`` changes are explicit.
    """
    if not isinstance(proposed, dict) or set(proposed) - _PROPOSAL_KEYS:
        raise DomainError("validation_error", "Browser settings accept enabled, proxy_server, mode and expected_exit_ip only; credentials belong in the protected credential file")
    old = old or {}
    was_isolated = old.get("mode") == ISOLATED
    if "mode" in proposed and proposed["mode"] not in {ISOLATED, LEGACY_MODE}:
        raise DomainError("validation_error", "Browser mode must be isolated-desktop-v1 or legacy")
    wants_isolated = (proposed["mode"] == ISOLATED) if "mode" in proposed else was_isolated
    transition = bool(old) and was_isolated != wants_isolated
    if wants_isolated:
        return _prepare_isolated(proposed, old, transition, paths=paths, business_id=business_id, uow=uow)
    if "expected_exit_ip" in proposed:
        raise DomainError("validation_error", "expected_exit_ip belongs to the isolated-desktop-v1 mode")
    return _prepare_legacy(proposed, old, transition, paths=paths, business_id=business_id, uow=uow)


def _prepare_legacy(proposed, old, transition, *, paths, business_id, uow):
    derived = legacy_paths(paths, business_id)
    _generated_only_from(proposed, old, derived)
    if transition and not {"enabled", "proxy_server"} <= proposed.keys():
        raise DomainError("validation_error", "Returning to the legacy mode needs explicit enabled and proxy_server values")
    enabled = proposed.get("enabled", old.get("enabled", False))
    if type(enabled) is not bool:
        raise DomainError("validation_error", "Browser enabled must be a boolean")
    proxy = validate_proxy(proposed.get("proxy_server", old.get("proxy_server")))
    if transition:
        assert_service_stopped(paths, business_id)
        browser = {"enabled": enabled, "proxy_server": proxy, "cdp_port": old["cdp_port"], **derived}
    else:
        browser = {**old, "enabled": enabled, "proxy_server": proxy}
        if not old:
            browser.update(cdp_port=_allocate_port(uow), **derived)
    assert_ip_unassigned(uow.connection, business_id, browser, old)
    if browser != old:
        browser["generation"] = new_id()
    return browser


def _prepare_isolated(proposed, old, transition, *, paths, business_id, uow):
    row = uow.connection.execute("SELECT slug FROM businesses WHERE id=?", (business_id,)).fetchone()
    if row is None:
        raise DomainError("not_found", "Project is not registered")
    located = isolated_paths(paths, row)
    derived = {"user_data_dir": located["user_data_dir"], "downloads_dir": located["downloads_dir"],
               "credentials_ref": isolated_reference(business_id)}
    _generated_only_from(proposed, old, derived)
    fresh = not old or transition
    explicit = fresh or "proxy_server" in proposed or "expected_exit_ip" in proposed
    if explicit and not {"proxy_server", "expected_exit_ip"} <= proposed.keys():
        raise DomainError("validation_error", "The isolated mode needs an explicit proxy_server and expected_exit_ip; a proxy or exit IP is never inherited")
    if fresh and "enabled" not in proposed:
        raise DomainError("validation_error", "The isolated mode needs an explicit enabled value when it is first selected")
    enabled = proposed.get("enabled", old.get("enabled", False))
    if type(enabled) is not bool:
        raise DomainError("validation_error", "Browser enabled must be a boolean")
    proxy = validate_isolated_proxy(proposed.get("proxy_server", old.get("proxy_server")))
    exit_ip = validate_exit_ip(proposed.get("expected_exit_ip", old.get("expected_exit_ip")))
    if fresh:
        _assert_no_sandbox(uow.connection, business_id)
    if transition:
        assert_service_stopped(paths, business_id)
    port = old.get("cdp_port") if old else _allocate_port(uow)
    if port is None:
        raise DomainError("invalid_configuration", "The existing browser assignment has no CDP port; operator repair is required")
    browser = {"mode": ISOLATED, "enabled": enabled, "proxy_server": proxy, "expected_exit_ip": exit_ip,
               "cdp_port": port, **derived}
    assert_ip_unassigned(uow.connection, business_id, browser, old)
    if not old or {k: v for k, v in old.items() if k != "generation"} != browser:
        browser["generation"] = new_id()
    else:
        browser["generation"] = old["generation"]
    return browser


def check_identity(row, paths, browser):
    """Exact structural validation of a saved block, independent of ``enabled``."""
    if not isinstance(browser, dict):
        raise DomainError("invalid_configuration", "Project browser identity is invalid; operator repair is required")
    business_id = row["id"]
    ports_ok = type(browser.get("cdp_port")) is int and browser["cdp_port"] in PORTS
    generation_ok = isinstance(browser.get("generation"), str) and bool(browser["generation"])
    if browser.get("mode") == ISOLATED:
        located = isolated_paths(paths, row)
        if (set(browser) != ISOLATED_KEYS or type(browser["enabled"]) is not bool or not ports_ok or not generation_ok
                or browser["user_data_dir"] != located["user_data_dir"]
                or browser["downloads_dir"] != located["downloads_dir"]
                or browser["credentials_ref"] != isolated_reference(business_id)):
            raise DomainError("invalid_configuration", "Project browser identity is invalid; operator repair is required")
        validate_isolated_proxy(browser["proxy_server"])
        validate_exit_ip(browser["expected_exit_ip"])
        return browser
    derived = legacy_paths(paths, business_id)
    if ("mode" in browser or set(browser) != LEGACY_KEYS or not ports_ok
            or any(browser[key] != derived[key] for key in derived)):
        raise DomainError("invalid_configuration", "Project browser identity is invalid; operator repair is required")
    validate_proxy(browser["proxy_server"])
    return browser


def validate_applied(proposed, before, *, paths, business_id, uow):
    """Re-run identity, uniqueness and mode-change checks inside the writing transaction.

    Preparation can precede application by hours (Signoff) and concurrent
    prepared requests for different businesses each pass their own preparation.
    """
    if proposed == before or proposed is None:
        return
    row = uow.connection.execute("SELECT id, slug FROM businesses WHERE id=?", (business_id,)).fetchone()
    if row is None:
        raise DomainError("not_found", "Project is not registered")
    check_identity({"id": row[0], "slug": row[1]}, paths, proposed)
    assert_ip_unassigned(uow.connection, business_id, proposed, before)
    if (proposed.get("mode") == ISOLATED) != ((before or {}).get("mode") == ISOLATED) and before:
        assert_service_stopped(paths, business_id)


def configured_target(row, paths):
    browser = row["config_json"].get("browser")
    if not browser or browser.get("enabled") is not True:
        raise DomainError("operation_unavailable", "This project browser is not enabled")
    check_identity(row, paths, browser)
    return browser


def legacy_target(row, paths):
    """The pre-isolation contract: sandbox and legacy-host callers never accept the new mode."""
    browser = row["config_json"].get("browser")
    if isinstance(browser, dict) and "mode" in browser:
        raise DomainError("invalid_configuration", "This project uses the isolated desktop browser mode, not the legacy browser")
    return configured_target(row, paths)
