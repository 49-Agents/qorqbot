"""Copyable native scaffolds validate without registration or tool grants."""
import json
import re
import shutil
from pathlib import Path

import pytest

from bazarlyk.catalog.packages import load_package
from qorqut_operations.contracts import DomainError

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('kind', ['basic'])
def test_instantiated_template_and_native_rejections(tmp_path, kind):
    source = ROOT / 'templates/plugins' / kind
    target = tmp_path / kind
    shutil.copytree(source, target)
    old, new = 'example-procedure', 'fixture-procedure'
    for file in target.rglob('*'):
        if file.is_file():
            file.write_text(file.read_text().replace(old, new))
    (target / 'skills' / old).rename(target / 'skills' / new)
    package = load_package(target, 'plugin.yaml', 'plugin', None)
    assert package.id == new
    assert {s['name'] for s in package.skills} == {f"bazarlyk-plugin-{s['id']}" for s in package.skills}
    assert json.loads((target / 'schemas/config.schema.json').read_text())['additionalProperties'] is False
    for readme in [target / 'README.md']:
        for link in re.findall(r'\]\(([^)]+)\)', readme.read_text()):
            assert (readme.parent / link).exists(), link
    manifest = target / 'plugin.yaml'
    valid = manifest.read_text()
    for invalid in [valid.replace(f'id: {new}', 'id: INVALID', 1), valid + 'unknown_field: true\n',
                    valid.replace(f'skills/{new}/SKILL.md', 'skills/missing/SKILL.md')]:
        manifest.write_text(invalid)
        with pytest.raises(DomainError):
            load_package(target, 'plugin.yaml', 'plugin', None)
    manifest.write_text(valid)
