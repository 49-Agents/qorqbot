#!/usr/bin/env python3
"""Keep the names of extension tables and core tables apart: three rules for us at release time, not limits for an owner.

An extension's own tables, indexes and triggers are named ``<id>_...``. So that a later core release can never take a
name an extension already uses, and two extensions can never claim one name:

- rule (a): no core table, index or trigger name starts with ``<id>_`` for an id ever published in an index;
- rule (b): no new extension id is one for which a core object name already starts with ``<id>_``;
- the id rule: no id equals another published id followed by ``_`` and more (``ab`` and ``ab_c``), whichever came
  first.

The ids ever published are the entries of the released index files (``--released``, each an index as
``extension_index.py`` writes it); an index about to be released is given with ``--new``, and its ids that no released
index has are the new ones. The core names come from the composition the app itself builds with no extension, compiled
as a store is (``host_assembly`` and ``compile_target``), never from a list kept by hand. Needs the app on the import
path, as the index tool does. Exits 1 with one line per clash, 0 when there is none.
"""
import argparse
from pathlib import Path
import sys


def core_names(extra=()):
    """The names of every table, index and trigger of the core store: the app's own composition with no extension and no
    recorded extension table, then ``extra`` contributions (tests add a core table this way)."""
    from qorqut.composition.assembly import HostComposition, host_contributions
    from qorqut.service.changes import ChangeFeed
    from qorqut_operations.storage.inventory import compile_target

    composition = HostComposition()
    for contribution in (*host_contributions(ChangeFeed()), *extra):
        composition.add(contribution)
    return sorted({name for _kind, name in compile_target(composition.freeze()).objects})


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
                found.append(f'Rule (a): the core name {name} starts with {ext_id}_, and {ext_id} is a published extension id.'
                             if ext_id in released else
                             f'Rule (b): the new extension id {ext_id} cannot be used, as the core name {name} starts with '
                             f'{ext_id}_.')
    every = sorted(released | new)
    for short in every:
        for long in every:
            if long.startswith(short + '_'):
                found.append(f'The id rule: the extension id {long} is the id {short} followed by _ and more.')
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
        print('No extension id and core name clash.')
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
