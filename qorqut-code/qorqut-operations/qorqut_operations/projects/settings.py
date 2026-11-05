"""Project settings: the pure recursive merge of a partial configuration update."""

from copy import deepcopy


def merge_settings(previous, changes):
    value = deepcopy(previous) if isinstance(previous, dict) else {}
    for key, item in changes.items():
        if item is None:
            value.pop(key, None)
        elif isinstance(item, dict):
            value[key] = merge_settings(value.get(key), item)
        else:
            value[key] = deepcopy(item)
    return value
