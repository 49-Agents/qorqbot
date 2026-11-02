"""Process identity checks shared by the agents' managed browsers."""

from pathlib import Path
import socket


def process_identity(pid):
    """Linux start ticks + boot ID prevent mistaking a reused PID for Chrome."""
    stat = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + stat[19]


def owns_listener(pid, port, address=None):
    """The process holds a listening TCP socket on ``port`` (optionally only on ``address``)."""
    wanted = None
    if address is not None:
        wanted = "".join(f"{byte:02X}" for byte in reversed(socket.inet_aton(address)))
    sockets = set()
    for path in Path(f"/proc/{int(pid)}/fd").iterdir():
        try:
            sockets.add(path.readlink().as_posix())
        except FileNotFoundError:
            continue  # Unrelated Chrome descriptors can close while inspecting.
    for protocol in ("tcp", "tcp6"):
        for line in Path(f"/proc/{int(pid)}/net/{protocol}").read_text().splitlines()[1:]:
            fields = line.split()
            host, _, number = fields[1].rpartition(":")
            if (fields[3] == "0A" and int(number, 16) == port and (wanted is None or host == wanted)
                and "socket:[" + fields[9] + "]" in sockets):
                return True
    return False
