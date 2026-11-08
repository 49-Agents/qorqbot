"""Explicit local reader setup and pairing; never invoked by installation or core startup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

from qorqut.private_files import require_private_directory, write_private_file
from . import binding


def install_reader(instance, agent_id, *, binary=None):
    """Copy the bundled helper and create one new private read-only binding, refusing replacement."""
    instance = Path(instance).absolute()
    if str(UUID(agent_id)) != agent_id or not instance.is_dir() or not binding.regular_path(instance):
        raise ValueError("Use an existing plain instance directory and an exact agent UUID")
    target = instance / "config" / (binding.NAME + ".json")
    if os.path.lexists(target):
        raise ValueError("A WhatsApp binding already exists; preserve it and use the documented offline update procedure")
    if len(os.fsencode(binding.socket_path(instance))) >= 108:
        raise ValueError("Use a shorter Qorqbot folder path for Unix socket support")
    binary = Path(binary) if binary else Path(__file__).parent / "bin" / binding.NAME
    payload = binary.read_bytes()
    sha = hashlib.sha256(payload).hexdigest()
    installed = instance / "runtime" / "connectors" / binding.NAME / sha[:40] / binding.NAME
    require_private_directory(installed.parent, create=True)
    if installed.exists() and installed.read_bytes() != payload:
        raise ValueError("Installed helper identity does not match its payload")
    if not installed.exists():
        descriptor = os.open(installed, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
    require_private_directory(target.parent, create=True)
    value = {"version": 1, "agent_id": agent_id, "binary": str(installed), "sha256": sha,
             "socket": str(binding.socket_path(instance))}
    if len(os.fsencode(value["socket"])) >= 108:
        raise ValueError("Use a shorter Qorqbot folder path for Unix socket support")
    binding.validate_binding(value, instance)
    write_private_file(target.parent, target.name, (json.dumps(value, indent=2) + "\n").encode())
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Optional WhatsApp reader setup. No account is contacted by install-reader.")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install-reader")
    install.add_argument("--instance", type=Path, required=True)
    install.add_argument("--agent", required=True)
    pair = commands.add_parser("pair", help="Explicitly start QR pairing; stop Qorqbot's receiver first")
    pair.add_argument("--instance", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "install-reader":
        install_reader(args.instance, args.agent)
        print("Read-only helper installed. Pair the account before starting the receiver.")
        return 0
    instance = args.instance.absolute()
    settings = binding.load_binding(instance)
    if settings is None:
        parser.error("Install the reader binding first")
    data = require_private_directory(instance / "private" / "whatsapp", create=True)
    temporary = require_private_directory(data / "tmp", create=True)
    require_private_directory(Path(settings["socket"]).parent, create=True)
    # No --enable-send or managed transport is permitted during pairing.
    return subprocess.call([settings["binary"], "serve", "--data-dir", str(data), "--socket", settings["socket"], "--pair", "--pair-only"],
                           cwd=data, env=dict(os.environ, HOME=str(data), TMPDIR=str(temporary)))


if __name__ == "__main__":
    raise SystemExit(main())
