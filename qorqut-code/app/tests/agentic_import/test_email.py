"""Canonical email guide imports, managed discovery and three-way preservation."""
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest
from qorqut_operations.contracts import DomainError
from qorqut_agentic import AgenticLayer

from bazarlyk.agentic import ImportFailure, inventory_sources, build_import_plan, apply_import_plan
from bazarlyk.agentic.support import decoded
from bazarlyk.bootstrap import email_guidance
from conftest import write
from text_support import FakeView

ATTR = dict(actor_id='test-owner', actor_kind='owner', reason='Synthetic email guide')
CANONICAL = 'packages/imported-email-management/guidance/email/'
REQUIRED = email_guidance.TEMPLATE_FILES


def snapshot(layer):
    return {path.relative_to(layer.root).as_posix(): path.read_bytes() for path in layer.root.rglob('*')
            if path.is_file() and '.git' not in path.parts}


@pytest.fixture
def layer(tmp_path):
    return AgenticLayer.initialize(tmp_path/'authored', tmp_path/'runtime',
        git_user=dict(name='Fixture',email='fixture@example.invalid'))


def test_seed_copies_have_exact_physical_provenance_and_inactive_originals(roots, layer):
    inventory = inventory_sources(roots, ['email-management'])
    assert inventory['coverage']['complete'], inventory['diagnostics']
    main = next(p for p in inventory['packages'] if p['id'] == 'imported-email-management')
    inactive = next(p for p in inventory['packages'] if p['id'] == 'imported-email-management-templates')
    assert not inactive['native']['enabled']
    for name in REQUIRED:
        original = 'skills/email-management/instance-template/'+name
        active = next(a for a in inventory['assets'] if a['destination'] == CANONICAL+name)
        seed = next(a for a in inventory['assets'] if a['path'] == original and a['symbol'] is None)
        assert (active['source_id'],active['path'],active['symbol']) == ('email',original,'email-guidance')
        assert active['sha256'] == seed['sha256']
        assert not active['template'] and active['enabled']
        assert seed['template'] and not seed['enabled']
        assert decoded(main['files']['guidance/email/'+name]['content_base64']) == decoded(inactive['files']['native/'+original]['content_base64'])
    apply_import_plan(layer, build_import_plan(inventory), ATTR)
    assert not layer.catalog.validate()['issues']
    assert layer.catalog.read_resource('imported-email-management','guidance/email/SKILL.md')['content'] == 'Template instructions'


def test_explicit_custom_snapshot_preserves_resources_and_reimport_edits(roots, tmp_path, layer):
    custom = tmp_path/'offline-guide'; custom.mkdir()
    for name in REQUIRED:
        write(custom,name,'Custom '+name)
    write(custom,'inboxes/extra.md','Synthetic extra account')
    write(custom,'resources/diagram.bin',b'\x00\xff')
    write(custom,'.env','synthetic private fixture excluded')
    first = inventory_sources(roots,['email-management'],email_guidance_root=str(custom))
    assert first['coverage']['complete'], first['diagnostics']
    source = next(s for s in first['sources'] if s['id'] == 'email-guidance')
    assert source['root'] == str(custom) and source['commit'] is None
    assert '.env' not in source['files']
    asset = next(a for a in first['assets'] if a['destination'] == CANONICAL+'SKILL.md')
    assert asset['source_id'] == 'email-guidance' and asset['path'] == 'SKILL.md' and asset['symbol'] is None
    receipt = apply_import_plan(layer,build_import_plan(first),ATTR)['import_manifest']
    assert (layer.root/CANONICAL/'resources/diagram.bin').read_bytes() == b'\x00\xff'
    target = layer.root/CANONICAL/'instructions.md'
    target.write_text('Locally edited current guide')
    write(custom,'instructions.md','Upstream changed custom guide')
    update = build_import_plan(inventory_sources(roots,['email-management'],email_guidance_root=str(custom)),snapshot(layer),receipt)
    assert any(c['path'] == CANONICAL+'instructions.md' for c in update['conflicts'])
    with pytest.raises(ImportFailure,match='locally owned'):
        apply_import_plan(layer,update,ATTR)
    assert target.read_text() == 'Locally edited current guide'


@pytest.mark.parametrize('defect',['missing','symlink','placeholder'])
def test_invalid_custom_snapshot_blocks_import(roots,tmp_path,defect):
    custom = tmp_path/'offline-guide'; custom.mkdir()
    for name in REQUIRED:
        write(custom,name,'Custom '+name)
    if defect == 'missing':
        (custom/'SKILL.md').unlink()
    elif defect == 'symlink':
        (custom/'SKILL.md').unlink();(custom/'SKILL.md').symlink_to(custom/'instructions.md')
    else:
        write(custom,'SKILL.md','Invalid {{PYTHON()}} template')
    inventory = inventory_sources(roots,[],email_guidance_root=str(custom))
    assert not inventory['coverage']['complete']
    assert any(d['code'] in {'missing_email_guidance','unsafe_path','invalid_email_guidance'} for d in inventory['diagnostics'])


def test_managed_email_home_and_discovery_have_no_old_guide_fallback(tmp_path,layer,roots):
    # The test layer has a real public catalog; the view's resource method is an explicit fake.
    apply_import_plan(layer, build_import_plan(inventory_sources(roots,['email-management'])), ATTR)
    discovery='Read `{{EMAIL_MANAGEMENT_HOME}}/SKILL.md` now.\n'
    view = FakeView({('email','skills/email-management/SKILL.md',None):discovery})
    view.layer=layer
    package=SimpleNamespace(files={'skills/email-management/SKILL.md':b'Stale source',
                                  'skills/email-management/instance-template/SKILL.md':b'Stale seed'})
    paths=SimpleNamespace(extension_home=lambda name:tmp_path/'must-not-be-read')
    expected=layer.root/'packages/imported-email-management/guidance/email'
    assert email_guidance.home(paths,agentic_view=view) == expected
    assert email_guidance.skill_files(package,paths,agentic_view=view) == {'SKILL.md':f'Read `{expected}/SKILL.md` now.\n'.encode()}
    view.resources['email','skills/email-management/SKILL.md',None]='Changed {{EMAIL_MANAGEMENT_HOME}}'
    assert email_guidance.skill_files(package,paths,agentic_view=view)['SKILL.md'] == f'Changed {expected}'.encode()
    with pytest.raises(DomainError):
        email_guidance.render(b'Invalid {{OTHER}}',paths,agentic_view=view)
    del view.resources['email','skills/email-management/SKILL.md',None]
    with pytest.raises(DomainError):
        email_guidance.skill_files(package,paths,agentic_view=view)
    (expected/'SKILL.md').unlink()
    with pytest.raises(Exception):
        email_guidance.home(paths,agentic_view=view)
    assert not (tmp_path/'must-not-be-read').exists()
