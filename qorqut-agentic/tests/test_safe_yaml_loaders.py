"""Both safe parser implementations retain the module's strict YAML contract."""
import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

from qorqut_agentic import util


@pytest.mark.parametrize("implementation", ["python", "libyaml"])
def test_safe_yaml_contract_and_global_tables_unchanged(implementation, monkeypatch):
    fast = getattr(yaml, "CSafeLoader", None)
    if implementation == "libyaml" and fast is None:
        pytest.skip("PyYAML was built without LibYAML")
    classes = [yaml.SafeLoader, *([fast] if fast else [])]
    original = [(copy.copy(cls.yaml_constructors), copy.deepcopy(cls.yaml_implicit_resolvers)) for cls in classes]
    if implementation == "python":
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    spec = importlib.util.spec_from_file_location("qorqut_agentic._loader_contract", util.__file__)
    module = importlib.util.module_from_spec(spec)
    exec(compile(Path(util.__file__).read_bytes(), util.__file__, "exec"), module.__dict__)
    assert module._SafeLoader is (yaml.SafeLoader if implementation == "python" else fast)
    expected = {"schema_version": 1, "id": "example", "name": "Example", "description": "Unicode: söz",
                "source": {"date": "2026-10-26", "time": "2026-10-26T10:11:12Z"},
                "entries": [{"id": "guide", "kind": "skill", "path": "skills/guide/SKILL.md", "requires": []}]}
    text = yaml.safe_dump(expected, allow_unicode=True)
    assert module.load_yaml(text) == expected
    matter, error = module.split_frontmatter("---\nname: guide\ndate: 2026-10-26\n---\nBody\n")
    assert matter == {"name": "guide", "date": "2026-10-26"} and error is None
    for rejected in (
        "source:\n  key: first\n  key: second\n",
        "? [unhashable, key]\n: value\n",
        "!!python/object/apply:builtins.eval ['1 + 1']",
        "value: .nan", "value: .inf", "value: !!binary ZGF0YQ==", "value: !!set {one: null}", "1: non-string-key",
    ):
        with pytest.raises(ValueError):
            module.load_yaml(rejected)
    for cls, (constructors, resolvers) in zip(classes, original):
        assert cls.yaml_constructors == constructors
        assert cls.yaml_implicit_resolvers == resolvers
