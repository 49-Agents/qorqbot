"""Safe structured YAML: the one loader for authored manifests, schemas and document frontmatter."""

from __future__ import annotations

from typing import Any

import yaml

from qorqut_operations.contracts import DomainError


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _mapping(loader: UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError(f"Duplicate or non-string YAML key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
# Dates remain portable JSON strings, never implicit datetime values.
UniqueKeyLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in values if tag != "tag:yaml.org,2002:timestamp"]
    for key, values in UniqueKeyLoader.yaml_implicit_resolvers.items()
}


def parse_yaml(text: str) -> Any:
    try:
        return yaml.load(text, Loader=UniqueKeyLoader)
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        raise DomainError("invalid_configuration", f"Invalid structured YAML: {exc}") from exc
