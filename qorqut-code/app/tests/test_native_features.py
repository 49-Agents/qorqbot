from pathlib import Path
from bazarlyk.bootstrap.features import probe,record_contract,checkpoint_supported,browser_supported


def executable(tmp_path,text):
    path=tmp_path/'client';path.write_text('#!/bin/sh\ncat <<\'HELP\'\n'+text+'\nHELP\n');path.chmod(0o755)
    return path


def test_compatible_patch_is_selected_by_features_and_cache_changes_with_build(tmp_path):
    path=executable(tmp_path,'codex --strict-config --dangerously-bypass-hook-trust --json --ignore-user-config --thread --message')
    first=probe(path)
    assert all(first[name] for name in ('json_events','resume','isolated_configuration','queue','hooks','scoped_mcp'))
    path.write_text('#!/bin/sh\necho unrelated\n')
    assert not probe(path).get('queue')


def test_checkpoint_requires_observed_contract_for_this_executable(tmp_path):
    path=executable(tmp_path,'codex --json');records=tmp_path/'contracts'
    assert not checkpoint_supported(path,records)
    record_contract(path,records,stop_hook_correction=True)
    assert checkpoint_supported(path,records)
    path.write_text('#!/bin/sh\necho updated-build\n')
    assert not checkpoint_supported(path,records)


def test_browser_patch_version_does_not_replace_required_features(tmp_path):
    path=executable(tmp_path,'snapshot connect eval --session --cdp --config')
    assert browser_supported(path,tmp_path)
    path.write_text('#!/bin/sh\necho snapshot\n')
    assert not browser_supported(path,tmp_path)
