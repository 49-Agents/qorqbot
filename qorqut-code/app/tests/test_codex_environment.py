import tomllib

from bazarlyk.bootstrap.codex_config import configuration, role_model_default


def test_generated_codex_inherits_environment_without_copying_secrets(tmp_path, monkeypatch):
    sentinel = "synthetic-secret-not-for-generated-files"
    monkeypatch.setenv("FASTMAIL_API_TOKEN", sentinel)
    config, _ = configuration(
        workspace=tmp_path, endpoint="http://127.0.0.1:1049",
        header_command="headers", command="bootstrap", snapshot_command="snapshot",
        protected=[tmp_path / ".codex"], read_denied=[tmp_path / ".bazarlyk/credential"],
    )
    data = tomllib.loads(config.decode())
    assert data["shell_environment_policy"] == {"inherit": "all", "ignore_default_excludes": True}
    assert sentinel.encode() not in config
    assert b"FASTMAIL_API_TOKEN" not in config
    assert data["approval_policy"] == "never"
    assert data["permissions"]["qorqut-agent"]["filesystem"][str(tmp_path / ".bazarlyk/credential")] == "deny"


def test_scoped_auto_approves_mcp_without_removing_filesystem_denies(tmp_path):
    config, _ = configuration(workspace=tmp_path, endpoint='http://localhost:1071',
        header_command='headers', command='bootstrap', snapshot_command='snapshot',
        protected=[], read_denied=[tmp_path / 'private'],
        external_servers={'outreach': {'command': 'synthetic-mcp'}})
    data = tomllib.loads(config.decode())
    assert data['default_permissions'] == 'qorqut-agent'
    assert data['approval_policy'] == 'never'
    assert data['permissions']['qorqut-agent']['filesystem'][str(tmp_path/'private')] == 'deny'
    assert all(s['default_tools_approval_mode']=='approve' for s in data['mcp_servers'].values())


def test_role_model_defaults_and_unchanged_service_tier(tmp_path):
    args = dict(workspace=tmp_path, endpoint='http://localhost:1071',
        header_command='headers', command='bootstrap', snapshot_command='snapshot',
        protected=[], read_denied=[])
    expected = {('ecosystem', 'ordubashy'): ('gpt-6-luna', 'high'), ('ecosystem', 'learning-applier'): ('gpt-6-astra', 'low')}
    for (scope, role), pair in expected.items():
        data = tomllib.loads(configuration(**args, model_default=role_model_default(scope, role))[0].decode())
        assert (data['model'], data['model_reasoning_effort']) == pair
        assert 'service_tier' not in data
    # Only a supported global role in ecosystem scope has a startup default: none for retired Founder or Global Marketing, no business-scope or Dev fallback.
    for scope, role in (('ecosystem', 'founder'), ('ecosystem', 'global-marketing'), ('ecosystem', 'other'), ('business', 'business-agent'), ('business', 'retired-role'),
                        ('business', 'dev'), ('business', 'ordubashy'), ('ecosystem', 'business-agent')):
        unknown = tomllib.loads(configuration(**args, model_default=role_model_default(scope, role))[0].decode())
        assert 'model' not in unknown and 'model_reasoning_effort' not in unknown, (scope, role)
    parent = tomllib.loads(configuration(**args, model_default=role_model_default('ecosystem', 'ordubashy'),
        delegate_operations=True)[0].decode())
    assert (parent['model'], parent['model_reasoning_effort']) == ('gpt-6-luna', 'high')
    assert parent['agents']['default_subagent_model'] == 'gpt-6-luna'
