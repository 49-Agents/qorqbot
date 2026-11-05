"""Operator-owned host deployment for one isolated business browser.

The business assignment (proxy, exit IP, CDP port, profile) lives in the
business's saved browser block. This file carries only what is specific to the
host: display, VNC address, runtime directory and installed tools. It never
names commands, proxies, credentials or profile paths. Its ``root`` is the instance root: every state and secret
location below is selected through the one ``InstancePaths`` it names.
"""

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from qorqut_operations.contracts import DomainError
from bazarlyk.instance_paths import InstancePathError, InstancePaths

SCHEMA_VERSION = 1
DEFAULT_DIRECTORY = Path("/etc/qorqut-browser-isolated")
CGNAT = ipaddress.ip_network("100.64.0.0/10")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
DEFAULT_EXIT_CHECK = ("api.ipify.org", "/?format=json")


def refuse(message):
    return DomainError("invalid_configuration", "Isolated browser deployment: " + message)


@dataclass(frozen=True)
class Binaries:
    """Fixed system executables. Only tests inject replacements; configs cannot."""
    xvnc: str = "/usr/bin/Xtigervnc"
    xauth: str = "/usr/bin/xauth"
    xdpyinfo: str = "/usr/bin/xdpyinfo"
    xset: str = "/usr/bin/xset"
    window_manager: str = "/usr/bin/openbox"
    chrome: str = "/usr/bin/google-chrome-stable"


@dataclass(frozen=True)
class Deployment:
    business_id: str
    root: Path
    display: int
    width: int
    height: int
    vnc_interface: str
    vnc_port: int
    runtime_dir: Path
    tools_dir: Path
    recheck_seconds: int = 60
    exit_check: tuple = DEFAULT_EXIT_CHECK
    node: str = "node"

    @property
    def display_name(self):
        return f":{self.display}"

    @property
    def paths(self):
        try:
            return InstancePaths(self.root, {})
        except InstancePathError as exc:
            raise DomainError("invalid_configuration", f"The deployment names an unusable instance root: {exc}") from exc

    @property
    def state_dir(self):
        return self.paths.runtime / "browsers" / self.business_id

    @property
    def secrets_dir(self):
        return self.paths.runtime / "secrets" / "browser-desktop" / self.business_id

    @property
    def vnc_password_file(self):
        return self.secrets_dir / "vnc.passwd"

    @property
    def upstream_socket(self):
        return self.runtime_dir / "upstream.sock"

    @property
    def cdp_socket(self):
        return self.runtime_dir / "cdp.sock"

    @property
    def vnc_socket(self):
        return self.runtime_dir / "vnc.sock"

    @property
    def xauthority(self):
        return self.runtime_dir / "Xauthority"


def _fields(value, where, required, optional=()):
    if not isinstance(value, dict):
        raise refuse(f"{where} must be an object")
    missing, unknown = set(required) - set(value), set(value) - set(required) - set(optional)
    if missing or unknown:
        raise refuse(f"{where} has missing {sorted(missing)} or unsupported {sorted(unknown)} fields")
    return value


def _integer(value, where, low, high):
    if type(value) is not int or not low <= value <= high:
        raise refuse(f"{where} must be an integer from {low} to {high}")
    return value


def _absolute(value, where):
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value or len(value) > 512 or "/../" in value + "/":
        raise refuse(f"{where} must be an absolute path")
    return Path(os.path.normpath(value))


def _interface(value):
    try:
        address = ipaddress.ip_address(value)
    except (TypeError, ValueError):
        raise refuse("vnc.interface must be one specific IP address") from None
    if address.version != 4 or address.is_unspecified or address.is_multicast or not (
            address.is_loopback or address.is_private or address in CGNAT):
        raise refuse("vnc.interface must be a loopback or private IPv4 address reachable from guacd")
    return str(address)


def _exit_check(value):
    fields = _fields(value, "exit_check", {"host", "path"})
    host, path = fields["host"], fields["path"]
    if not isinstance(host, str) or not HOST.fullmatch(host) or _is_literal(host):
        raise refuse("exit_check.host must be a DNS name")
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 256 or any(ord(c) < 33 for c in path):
        raise refuse("exit_check.path must be an absolute URL path")
    return host, path


def _is_literal(host):
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def open_deployment_database(deployment):
    """The existing operational database of the deployment's instance, validated by the engine (never created)."""
    from bazarlyk.composition.assembly import host_assembly, open_database
    from bazarlyk.service.changes import ChangeFeed
    return open_database(deployment.paths, host_assembly(ChangeFeed()))


def parse(value):
    value = _fields(value, "config", {"schema_version", "business_id", "root", "display", "geometry", "vnc",
                                      "runtime_dir", "tools_dir"}, {"recheck_seconds", "exit_check", "node"})
    if value["schema_version"] != SCHEMA_VERSION:
        raise refuse("schema_version must be 1")
    if not isinstance(value["business_id"], str) or not UUID.fullmatch(value["business_id"]):
        raise refuse("business_id must be the registered project UUID")
    geometry = _fields(value["geometry"], "geometry", {"width", "height"})
    vnc = _fields(value["vnc"], "vnc", {"interface", "port"})
    return Deployment(
        business_id=value["business_id"], root=_absolute(value["root"], "root"),
        display=_integer(value["display"], "display", 1, 999),
        width=_integer(geometry["width"], "geometry.width", 320, 8192),
        height=_integer(geometry["height"], "geometry.height", 240, 8192),
        vnc_interface=_interface(vnc["interface"]), vnc_port=_integer(vnc["port"], "vnc.port", 1024, 65535),
        runtime_dir=_absolute(value["runtime_dir"], "runtime_dir"), tools_dir=_absolute(value["tools_dir"], "tools_dir"),
        recheck_seconds=_integer(value.get("recheck_seconds", 60), "recheck_seconds", 10, 3600),
        exit_check=_exit_check(value["exit_check"]) if "exit_check" in value else DEFAULT_EXIT_CHECK,
        node=str(_absolute(value["node"], "node")) if "node" in value else "node")


def load(path):
    """An ordinary operator-owned file that no other user can write."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as error:
        raise refuse(f"{path}: cannot open config ({error.strerror})") from None
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_uid not in {0, os.getuid()} or info.st_size > 65536:
            raise refuse(f"{path}: config must be an ordinary operator-owned file, not group/world writable")
        try:
            value = json.loads(source.read().decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise refuse(f"{path}: config is not JSON") from None
    return parse(value)


def collisions(deployments, desktops=()):
    """Duplicate businesses, displays, ports, runtime dirs; ``desktops`` are (name, display, ports)."""
    problems, owners = [], {}
    claims = []
    for item in deployments:
        label = "isolated " + item.business_id
        claims += [(label, "display", item.display), (label, "runtime directory", str(item.runtime_dir)),
                   (label, "business", item.business_id), (label, "VNC port", item.vnc_port)]
    for name, display, ports in desktops:
        claims.append(("desktop " + name, "display", display))
        claims += [("desktop " + name, "VNC port", port) for port in ports]
    for owner, kind, value in claims:
        other = owners.setdefault((kind, value), owner)
        if other != owner:
            problems.append(f"{kind} {value} is used by both {other} and {owner}")
    return sorted(set(problems))


def proxy_endpoint(proxy_server):
    """The frozen numeric HTTP proxy address (host, port); no name resolution ever happens."""
    parsed = urlsplit(proxy_server)
    return parsed.hostname, parsed.port
