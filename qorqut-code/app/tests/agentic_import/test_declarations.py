"""Copied converted sources must prove declarations; legacy dynamic prose stays pending."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from bazarlyk.agentic import inventory_sources, build_import_plan, apply_import_plan
from bazarlyk.agentic.support import decoded
from conftest import write
from text_support import HOST, GROUP


def save(root):
    subprocess.run(['git','-C',str(root),'add','.'],check=True)
    subprocess.run(['git','-C',str(root),'commit','-qm','Converted synthetic source'],check=True)


@pytest.fixture
def converted(roots, sources):
    # Source declarations now compose with the shipped five-area role bundles.
    # The shared fixture's pre-reorganization role tree is for legacy tests.
    from importlib.resources import files
    defaults = files('qorqut_agentic').joinpath('resources', 'defaults')
    for old in ('roles', 'skills'):
        shutil.rmtree(sources['roles'] / old)
    for area in ('roles', 'operations', 'overseer'):
        shutil.copytree(defaults / area, sources['roles'] / area)
    write(sources['host'], 'app/bazarlyk/agents/skills/cleanup/SKILL.md',
          (HOST/'app/bazarlyk/agents/skills/cleanup/SKILL.md').read_bytes())
    for rel in ('app/bazarlyk/agentic/assets',):
        shutil.copytree(HOST/rel, sources['host']/rel)
    for rel in ('agents/context.py','agents/role_workflow.py','agents/cleanup_workflow.py','agents/engineering.py','inbox/defaults.py','bootstrap/email_guidance.py'):
        path='app/bazarlyk/'+rel
        write(sources['host'],path,(HOST/path).read_bytes())
    # Only the explicit known source files are copied. Their top-level code must never execute.
    with (sources['host']/'app/bazarlyk/agents/context.py').open('a') as handle:
        handle.write('\nraise RuntimeError("the inventory must not execute source")\n')
    write(sources['email'],'skills/email-management/SKILL.md',
          (GROUP/'dependencies/email-management/skills/email-management/SKILL.md').read_bytes())
    for name in ('native.py',):
        write(sources['paper'],'qorqut_paper/'+name,(GROUP/'qorqut-paper/qorqut_paper'/name).read_bytes())
    shutil.copytree(GROUP/'qorqut-paper/qorqut_paper/agentic-assets',sources['paper']/'qorqut_paper/agentic-assets')
    for name in ('host','email','paper','roles'):
        save(sources[name])
    return roots,sources


def test_declared_sources_close_all_three_composition_gaps(converted):
    roots,_=converted
    inventory=inventory_sources(roots,['paper','email-management'])
    assert inventory['coverage']['complete'],inventory['diagnostics']
    assert not any(d['code']=='pending_host_composition' for d in inventory['diagnostics'])
    coverage=[a for a in inventory['assets'] if a['status']=='declared_composition']
    paths={a['path'] for a in coverage}
    assert {'app/bazarlyk/agents/context.py','app/bazarlyk/bootstrap/email_guidance.py','qorqut_paper/native.py'} <= paths
    declared=[a for a in inventory['assets'] if 'source_site' in a]
    assert len(declared) >= 40
    assert all(a['source_site_sha256'] and a['source_site_lines'] for a in declared)
    assert all(not p['native']['enabled'] for p in inventory['packages'] if p['id'].endswith('-templates'))
    assert all(a['package_id'].endswith('-templates') and not a['enabled'] for a in inventory['assets'] if a['template'])
    # A disabled real extension stays a whole disabled package, not an enabled procedural package.
    assert not next(p for p in inventory['packages'] if p['id']=='imported-fastmail-metadata')['native']['enabled']


@pytest.mark.parametrize('defect', ['missing-file','missing-entry','unknown-path','unknown-parameter','wrong-site','unresolved-prose','email-contract','email-renderer','paper-prose','invalid-utf8','cleanup-contract'])
def test_broken_composition_declarations_remain_incomplete(converted,defect):
    roots,sources=converted
    path=sources['host']/'app/bazarlyk/agentic/assets/index.json'
    index=json.loads(path.read_text())
    if defect=='missing-file':
        (sources['host']/index['entries'][0]['path']).unlink()
    elif defect=='missing-entry':
        index['entries'].pop()
    elif defect=='unknown-path':
        index['entries'][0]['path']='../escape.md'
    elif defect=='unknown-parameter':
        index['entries'][0]['parameters']=['UNKNOWN']
    elif defect=='wrong-site':
        index['entries'][0]['source_site']='app/bazarlyk/agents/context.py:missing_function'
    elif defect=='unresolved-prose':
        with (sources['host']/'app/bazarlyk/agents/context.py').open('a') as handle:
            handle.write('\nNEW_INSTRUCTIONS = "New embedded guidance must be declared, not silently imported."\n')
    elif defect=='cleanup-contract':
        p=sources['host']/'app/bazarlyk/agents/cleanup_workflow.py'
        p.write_text(p.read_text().replace('body = read_resource(', 'body = stale_resource('))
    elif defect=='email-contract':
        p=sources['host']/'app/bazarlyk/bootstrap/email_guidance.py'
        p.write_text(p.read_text().replace('EMAIL_COMPOSITION =','OLD_EMAIL_COMPOSITION ='))
    elif defect=='email-renderer':
        p=sources['host']/'app/bazarlyk/bootstrap/email_guidance.py'
        p.write_text(p.read_text().replace('return render_text(text, parameters, values).encode("utf-8")','return text.encode("utf-8")'))
    elif defect=='invalid-utf8':
        (sources['host']/index['entries'][0]['path']).write_bytes(b'\xff')
    elif defect=='paper-prose':
        p=sources['paper']/'qorqut_paper/native.py'
        p.write_text(p.read_text().replace('return render(agentic_view, "paper", "qorqut_paper/agentic-assets/prepared.md")','return "Forgotten native prose"'))
    path.write_text(json.dumps(index))
    save(sources['host'])
    if defect=='paper-prose': save(sources['paper'])
    inventory=inventory_sources(roots,['paper','email-management'])
    assert not inventory['coverage']['complete']
    assert any(d['severity'] in {'error','pending'} for d in inventory['diagnostics'])


def test_unsafe_index_symlink_is_rejected_without_reading_target(converted,tmp_path):
    roots,sources=converted
    path=sources['host']/'app/bazarlyk/agentic/assets/index.json'
    outside=tmp_path/'outside.json';outside.write_bytes(path.read_bytes())
    path.unlink();path.symlink_to(outside)
    save(sources['host'])
    inventory=inventory_sources(roots,[])
    assert not inventory['coverage']['complete']
    assert any(d['code']=='unsafe_path' for d in inventory['diagnostics'])
