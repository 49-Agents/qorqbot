"""Packaged seeds for fresh profiles and standalone native-library metadata.

Active sessions use the managed catalog or their prepared snapshot, never this
module as a fallback. Saved assignments and historical records keep their text.
"""
from functools import lru_cache
from importlib.resources import files

from .instructions import from_catalog


@lru_cache(maxsize=1)
def runtime_defaults():
    root = files("qorqut_agentic").joinpath("resources/defaults/operations/runtime-instructions")
    return from_catalog(lambda path: root.joinpath(path).read_text(encoding="utf-8"))


def runtime_text(key, **values):
    return runtime_defaults().render(key, **values)
