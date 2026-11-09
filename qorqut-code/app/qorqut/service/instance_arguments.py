"""Explicit launch inputs for the instance paths shared by every host command that assembles a runtime.

``--instance-root`` and repeatable ``--component NAME=ABSOLUTE_PATH`` come from the trusted launcher. Nothing here
has a default, consults the environment or working directory, or touches the filesystem.
"""

from pathlib import Path

from qorqut_operations.contracts import DomainError
from qorqut.instance_paths import COMPONENTS, InstancePathError, InstancePaths


def add_instance_arguments(parser):
    parser.add_argument("--instance-root", type=Path, default=None,
                        help="Absolute instance root holding operations/")
    parser.add_argument("--component", action="append", default=[], metavar="NAME=ABSOLUTE_PATH",
                        help=f"Explicit component location; repeatable; names: {', '.join(COMPONENTS)}")


def instance_paths_from_arguments(args, *, required):
    """Build the one InstancePaths for this launch; ``required`` names the components the command consumes."""
    if args.instance_root is None:
        raise DomainError("invalid_configuration", "Set the explicit instance root with --instance-root")
    components = {}
    for selection in args.component:
        name, separator, location = selection.partition("=")
        if not separator or not name or not location:
            raise DomainError("invalid_configuration", f"--component must be NAME=ABSOLUTE_PATH, not {selection!r}")
        if name in components:
            raise DomainError("invalid_configuration", f"--component {name} was given more than once")
        components[name] = location
    missing = [name for name in required if name not in components]
    if missing:
        raise DomainError("invalid_configuration", f"Set --component NAME=ABSOLUTE_PATH for: {', '.join(missing)}")
    try:
        return InstancePaths(args.instance_root, components)
    except InstancePathError as exc:
        raise DomainError("invalid_configuration", f"Invalid instance paths: {exc}") from exc


def instance_arguments(paths):
    """The launch arguments that select exactly these paths, for a child command that needs the same selection."""
    arguments = ["--instance-root", str(paths.instance_root)]
    for name, location in paths.components.items():
        arguments += ["--component", f"{name}={location}"]
    return arguments
