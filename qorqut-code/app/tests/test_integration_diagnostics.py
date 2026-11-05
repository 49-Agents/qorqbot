from types import SimpleNamespace

from bazarlyk.bootstrap.diagnostics import helper_probe, integration_facts, command_executables


def test_missing_executable_and_failed_import_are_separate(tmp_path):
    assert helper_probe(tmp_path / "gone")["reason"] == "missing_executable"
    executable = tmp_path / "python"
    executable.write_text("fixture")
    executable.chmod(0o700)
    result = helper_probe(executable, run=lambda *a, **kw: SimpleNamespace(returncode=1))
    assert result["reason"] == "helper_import_failed" and not result["ready"]


def test_a_nudge_state_never_decides_readiness_and_a_waiting_composer_is_not_ready():
    base = dict(helper={"ready": True}, process="verified", backend="connected", delivery="ready")
    assert integration_facts(**base)["nudge"] == "unknown" and integration_facts(**base)["ready"]
    for state in ("idle", "pending", "deferred", "submitted", "uncertain", "stale", "unavailable"):
        facts = integration_facts(**base, nudge={"state": state})
        assert facts["nudge"] == state and facts["ready"]
    waiting = integration_facts(**{**base, "delivery": "waiting_for_composer"}, nudge={"state": "deferred"})
    assert waiting["nudge"] == "deferred" and not waiting["ready"]


def test_archived_and_remote_observations_remain_distinct():
    facts = integration_facts(helper={"ready": False}, scope="archived", target="remote",
        process="unreachable", backend="connected")
    assert facts["execution_target"] == "remote" and facts["scope"] == "archived"
    assert not facts["ready"]
    assert command_executables({"hooks": [{"command": "/gone/python -I -m helper --token private"}]}) == ["/gone/python"]


def test_timed_out_probe_recovers_without_restart_and_only_success_is_cached(monkeypatch):
    import subprocess
    from bazarlyk.bootstrap import diagnostics as d
    d._verified_imports.cache_clear()
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
        return SimpleNamespace(returncode=0, stdout='["/fixture/helper.py"]', stderr='')
    monkeypatch.setattr(d.subprocess, 'run', run)
    try:
        assert d._prepared_imports('/fixture/python', 1, 1)['reason'] == 'helper_probe_unavailable'
        assert d._prepared_imports('/fixture/python', 1, 1)['ready']
        assert d._prepared_imports('/fixture/python', 1, 1)['ready']
        assert len(calls) == 2 and calls[0]['timeout'] > 1
    finally:
        d._verified_imports.cache_clear()


def test_confirmed_import_failure_is_not_cached_or_treated_as_healthy(monkeypatch):
    from bazarlyk.bootstrap import diagnostics as d
    d._verified_imports.cache_clear()
    results = iter([SimpleNamespace(returncode=1, stdout='', stderr='ModuleNotFoundError: fixture'),
                    SimpleNamespace(returncode=0, stdout='["/fixture/helper.py"]', stderr='')])
    monkeypatch.setattr(d.subprocess, 'run', lambda *a, **kw: next(results))
    try:
        assert d._prepared_imports('/fixture/python', 1, 1) == {'ready': False, 'reason': 'helper_import_failed'}
        assert d._prepared_imports('/fixture/python', 1, 1)['ready']
    finally:
        d._verified_imports.cache_clear()


def test_prepared_helpers_still_detects_removed_import_after_success(tmp_path, monkeypatch):
    from bazarlyk.bootstrap import diagnostics as d
    executable = tmp_path/'python'; executable.write_text('fixture'); executable.chmod(0o700)
    helper = tmp_path/'helper.py'; helper.write_text('fixture')
    d._verified_imports.cache_clear()
    monkeypatch.setattr(d.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=__import__('json').dumps([str(helper)]), stderr=''))
    try:
        projection = {'helper_runtime': {'executable': str(executable)}}
        assert d.prepared_helpers(projection)['ready']
        helper.unlink()
        assert d.prepared_helpers(projection)['reason'] == 'helper_import_missing'
    finally:
        d._verified_imports.cache_clear()


def test_bootstrap_timeout_is_retryable_but_does_not_hide_real_configuration_drift(monkeypatch):
    from bazarlyk.agents.resolver import AgentConfigResolver
    from bazarlyk.bootstrap import skill_refresh
    monkeypatch.setattr(skill_refresh, 'refresh', lambda *a: False)
    for drift, code in [(['helper_probe_unavailable'], 'bootstrap_unavailable'),
                        (['helper_probe_unavailable', 'scoped_credential_unavailable'], 'reprepare_required'),
                        (['helper_import_failed'], 'reprepare_required')]:
        prep = {'ready': False, 'runtimes': {'codex': {'ready': False, 'projection_drift': drift,
                                                    'compatibility': {'supported': True}}}}
        config = SimpleNamespace(effective_config_id='fixture', to_dict=lambda: {'preparation': prep})
        resolver = SimpleNamespace(resolve_agent_config=lambda *a, **kw: config)
        result = AgentConfigResolver._bootstrap(resolver, SimpleNamespace(id='fixture'), registration_id='fixture',
            session_id='fixture', event='prompt', last_effective_config_id=None, runtime='codex')
        assert result['ready'] is False and result['error']['code'] == code
        if code == 'bootstrap_unavailable':
            assert result['error']['retryable'] is True
            assert result['preparation']['status'] == code
            assert 'reprepare' not in result['error']['message']
