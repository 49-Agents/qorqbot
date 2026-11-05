"""Fixed authored areas and public mutation boundary."""
from .errors import AgenticError
from .files import split_path
from .util import is_id

AREAS = ("roles", "operations", "overseer", "plugins", "user-preferences")


def editable(path: str) -> bool:
    parts = split_path(path)
    return ((len(parts) >= 3 and parts[0] == "plugins" and is_id(parts[1]))
            or (len(parts) >= 2 and parts[0] == "user-preferences"))


def require_editable(paths):
    paths = list(paths)
    for path in paths:
        split_path(path)
    protected = sorted({path for path in paths if not editable(path)})
    if protected:
        raise AgenticError("paths are in a protected agentic area", code="protected_agentic_area",
                           details={"paths": protected})


def import_dir(package_id: str) -> str:
    return "user-preferences" if package_id == "user-preferences" else f"plugins/{package_id}"
