#!/usr/bin/env python3
"""Keep the names of extension tables and core tables apart: three rules for us at release time, not limits for an owner.

An extension's own tables, indexes and triggers are named ``<id>_...``. So that a later core release can never take a
name an extension already uses, and two extensions can never claim one name:

- rule (a): no core table, index or trigger name starts with ``<id>_`` for an id ever published in an index;
- rule (b): no new extension id is one for which a core object name already starts with ``<id>_``;
- the id rule: no id equals another published id followed by ``_`` and more (``ab`` and ``ab_c``), whichever came
  first; the ids of the extensions the app ships count as published.

The ids ever published are the entries of the released index files (``--released``, each an index as
``extension_index.py`` writes it); an index about to be released is given with ``--new``, and its ids that no released
index has are the new ones. The core names come from the composition the app itself builds with no extension, compiled
as a store is (``host_assembly`` and ``compile_target``), never from a list kept by hand: core, and every extension
this app ships today (``shipped_factories``: the release tools' own lists of the optional connectors and the bundled
extensions), composed from this checkout's packages in a process of its own. Needs the app on the import path, as the
index tool does. Exits 1 with one line per clash, 0 when there is none.

The bundle builder and ``extension_contract --load`` refuse an id by rule (b) (``taken_prefix``), and the index builder
two ids by the id rule (``id_clashes``), through the functions here.
"""
import argparse
import functools
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

DISTRIBUTION = Path(__file__).resolve().parent
REPOSITORY = DISTRIBUTION.parents[2]
# Where this checkout keeps the packages of the extensions the app ships.
PACKAGE_ROOTS = (REPOSITORY / 'qorqut-code' / 'extensions', REPOSITORY / 'extensions')
_COMPOSE = (
    "import importlib, inspect, json, sys; sys.path[:0] = json.loads(sys.argv[1]); "
    "from qorqut.composition.assembly import host_assembly; from qorqut.extensions import Extension, ExtensionSet; "
    "from qorqut.service.changes import ChangeFeed; from qorqut_operations.errors import DomainError; "
    "from qorqut_operations.storage.inventory import compile_target\n"
    "made, ids = [], []\n"
    "for factory in json.loads(sys.argv[2]):\n"
    "    module_name, _, attribute = factory.partition(':'); module = importlib.import_module(module_name)\n"
    "    try:\n"
    "        made.append(getattr(module, attribute)()); ids.append(made[-1].id)\n"
    "    except DomainError:\n"
    "        own = [value for value in vars(module).values() if inspect.isclass(value) and issubclass(value, Extension)\n"
    "               and value.__module__ == module.__name__]\n"
    "        if not own or any(value.storage is not Extension.storage for value in own):\n"
    "            raise SystemExit('needs a configuration to be composed: ' + factory)\n"
    "        ids.extend(value.id for value in own)\n"
    "print(json.dumps({'names': sorted({name for _kind, name in compile_target(host_assembly(ChangeFeed(), "
    "ExtensionSet(tuple(made)))).objects}), 'ids': sorted(set(ids))}))\n")


def _tool(name):
    spec = importlib.util.spec_from_file_location(f'extension_names_{name}', DISTRIBUTION / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shipped_factories():
    """The factories of every today's extension this app ships: the optional connectors (``native_extension.FACTORIES``)
    and the extensions the app's bundle carries (``native_launch.BUNDLED_FACTORIES``)."""
    return tuple(sorted({*_tool('native_extension').FACTORIES.values(), *_tool('native_launch').BUNDLED_FACTORIES}))


def _package_folder(factory):
    """The folder of this checkout that holds the package of ``factory``."""
    package = factory.partition(':')[0].split('.')[0]
    found = [root / folder.name for root in PACKAGE_ROOTS if root.is_dir() for folder in sorted(root.iterdir())
             if (folder / package).is_dir()]
    if len(found) != 1:
        raise ValueError(f'This checkout does not hold the package {package} exactly once.')
    return str(found[0])


def _shipped_names(factories):
    return _shipped(factories)['names']


def shipped_ids(factories=None):
    """The ids of the extensions the app ships (``factories``, ``shipped_factories()`` when not given)."""
    return _shipped(tuple(shipped_factories() if factories is None else factories))['ids']


@functools.cache
def _shipped(factories):
    """``{"names": ..., "ids": ...}``: the names of the core store with ``factories`` selected and the ids of their
    extensions, composed in a process of its own from this checkout's packages, so nothing of them is imported here. A
    factory that needs a configuration is left out of the names only when its module declares no storage; otherwise
    its names cannot be known and ``ValueError`` is raised."""
    if not factories:
        return {'names': (), 'ids': ()}
    folders = sorted({_package_folder(factory) for factory in factories})
    done = subprocess.run([sys.executable, '-I', '-B', '-c', _COMPOSE, json.dumps([*folders, *sys.path]),
                           json.dumps(list(factories))], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if done.returncode != 0:
        raise ValueError('The extensions the app ships could not be composed: '
                         + (done.stderr.strip().splitlines() or ['no reason given'])[-1])
    found = json.loads(done.stdout)
    return {'names': tuple(found['names']), 'ids': tuple(found['ids'])}


def core_names(extra=(), factories=None):
    """The names of every table, index and trigger this checkout can put into a store: the app's own composition with
    no extension and no recorded extension table, then ``extra`` contributions (tests add a core table this way), and
    the names of the extensions the app ships (``factories``, ``shipped_factories()`` when not given)."""
    from qorqut.composition.assembly import HostComposition, host_contributions
    from qorqut.service.changes import ChangeFeed
    from qorqut_operations.storage.inventory import compile_target

    composition = HostComposition()
    for contribution in (*host_contributions(ChangeFeed()), *extra):
        composition.add(contribution)
    names = {name for _kind, name in compile_target(composition.freeze()).objects}
    return sorted(names | set(_shipped_names(tuple(shipped_factories() if factories is None else factories))))


def taken_prefix(ext_id, names=None):
    """Rule (b) for one id: the first name of ``names`` (``core_names()`` when not given) that starts with ``<id>_``, or
    None."""
    names = _every_name() if names is None else names
    return next((name for name in names if name.startswith(ext_id + '_')), None)


@functools.cache
def _every_name():
    """``core_names()``, composed once in a process: the bundle builder asks for every bundle it builds."""
    return tuple(core_names())


def taken_sentence(ext_id, name):
    """The sentence of rule (b) for ``ext_id`` and the name that starts with it."""
    return (f'the new extension id {ext_id} cannot be used, as the name {name}, which Qorqbot can put into a database, '
            f'starts with {ext_id}_.')


def id_sentence(short, long):
    return f'the extension id {long} is the id {short} followed by _ and more.'


def id_clashes(ids, shipped=None):
    """The id rule: ``(short, long)`` for each two of ``ids`` and the ids of the extensions the app ships (``shipped``,
    ``shipped_ids()`` when not given), at least one of them of ``ids``, where ``long`` is ``short`` followed by ``_``
    and more: a shipped id counts as a published one."""
    ids = set(ids)
    every = sorted(ids | set(shipped_ids() if shipped is None else shipped))
    return [(short, long) for short in every for long in every if long.startswith(short + '_')
            and (short in ids or long in ids)]


def index_ids(paths):
    """The extension ids of the index files at ``paths``; ``ValueError`` when one cannot be read."""
    from qorqut import extension_index
    found = set()
    for path in paths:
        try:
            found.update(entry.id for entry in extension_index.load(Path(path)).entries)
        except extension_index.ExtensionIndexError as error:
            raise ValueError(f'The index {Path(path).name} cannot be used: {error}') from None
    return found


def clashes(released, new=(), names=None):
    """Every break of the three rules, one sentence each, for the ids ``released`` (ever published) and ``new`` (about
    to be), against the core ``names`` (``core_names()`` when not given)."""
    names = core_names() if names is None else names
    released, new = set(released), set(new) - set(released)
    found = []
    for ext_id in sorted(released | new):
        for name in names:
            if name.startswith(ext_id + '_'):
                found.append(f'Rule (a): the name {name}, which Qorqbot can put into a database, starts with {ext_id}_, '
                             f'and {ext_id} is a published extension id.' if ext_id in released else
                             f'Rule (b): {taken_sentence(ext_id, name)}')
    for short, long in id_clashes(released | new):
        found.append(f'The id rule: {id_sentence(short, long)}')
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--released', action='append', type=Path, default=[],
                        help='a released index file; give it once for each')
    parser.add_argument('--new', action='append', type=Path, default=[], help='an index file about to be released')
    args = parser.parse_args(argv)
    try:
        found = clashes(index_ids(args.released), index_ids(args.new))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    for line in found:
        print(line)
    if not found:
        print('No extension id and name clash.')
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
