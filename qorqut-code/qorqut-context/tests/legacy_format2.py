"""A compact synthetic format 2 Context: the exact records the frozen format 2 engine (Git commit 291b9ce) wrote.

Two source events (a node pair with an edge, then a node edit citing the first commit) generated once by that engine
with fixed IDs, and replayed here byte for byte through ``git fast-import`` so every commit ID is the original one.
Nothing imports or copies the old engine; ``build`` fails if a replayed commit differs from its recorded ID.
"""
from pathlib import Path

from qorqut_context.git_store import Git
from qorqut_context.schema import encode

COMMITS = [{'commit': 'e52a1a38c57516b986e0b3d12c5a309d0e61aa49',
  'time': 1792190024,
  'message': 'Initialize context vocabulary',
  'files': {'graph/catalogs/edges.json': {},
            'graph/catalogs/nodes.json': {'basic_entity_type': ['person']},
            'graph/config.json': {'format_version': 2, 'published_ref': 'refs/heads/main'}}},
 {'commit': '6dcab3fcda692f243e9e2d95e3e6af1d90219d84',
  'time': 1792190025,
  'message': 'Context event leg01: add',
  'files': {'foundation/one.md': 'Jesse manages Alice at Northstar.',
            'graph/catalogs/edges.json': {'relation': ['manager']},
            'graph/catalogs/nodes.json': {'basic_entity_type': ['person'], 'name': ['Alice', 'Jesse']},
            'graph/edges/mgr01.json': {'attributes': {'relation': ['manager']},
                                       'from_id': 'jesse',
                                       'id': 'mgr01',
                                       'summary': 'Jesse manages Alice',
                                       'to_id': 'alice'},
            'graph/events/leg01.json': {'base_revision': 'e52a1a38c57516b986e0b3d12c5a309d0e61aa49',
                                        'changes': [{'citations': ['self'],
                                                     'developments': [],
                                                     'explanation': 'The source states the management line.',
                                                     'id': 'mgr01',
                                                     'kind': 'edge',
                                                     'operation': 'add',
                                                     'support_after': ['self']},
                                                    {'citations': ['self'],
                                                     'developments': [],
                                                     'explanation': 'The source names Alice.',
                                                     'id': 'alice',
                                                     'kind': 'node',
                                                     'operation': 'add',
                                                     'support_after': ['self']},
                                                    {'citations': ['self'],
                                                     'developments': [],
                                                     'explanation': 'The source names Jesse.',
                                                     'id': 'jesse',
                                                     'kind': 'node',
                                                     'operation': 'add',
                                                     'support_after': ['self']}],
                                        'format_version': 2,
                                        'handoff_note': {'disposition': 'empty', 'text': ''},
                                        'id': 'leg01',
                                        'input_digest': 'a8a532b2e053f1863f2a552db2dc413b8f6887e0cf39258bcd4afe7f1694292e',
                                        'input_mode': 'GENERAL',
                                        'operation': 'add',
                                        'previous_source_event': None,
                                        'source': {'created_at': '2026-01-01T00:00:00Z',
                                                   'id': 'tyaxd',
                                                   'kind': 'note',
                                                   'origin': 'legacy-fixture',
                                                   'path': 'foundation/one.md',
                                                   'title': 'one.md'},
                                        'submission_digest': '80bf362e1363c60386e37015dd4560886107a3b83e4ca675373d9ace8c553776',
                                        'submitted_by': 'legacy-owner'},
            'graph/nodes/alice.json': {'attributes': {'basic_entity_type': ['person'], 'name': ['Alice']},
                                       'id': 'alice',
                                       'summary': 'Alice'},
            'graph/nodes/jesse.json': {'attributes': {'basic_entity_type': ['person'], 'name': ['Jesse']},
                                       'id': 'jesse',
                                       'summary': 'Jesse'}}},
 {'commit': '4a55ddc251622d6c1ca82505eaa38e84cd91819b',
  'time': 1792190026,
  'message': 'Context event leg02: add',
  'files': {'foundation/two.md': 'Jesse also leads the Berlin studio.',
            'graph/catalogs/nodes.json': {'basic_entity_type': ['person'],
                                          'name': ['Alice', 'Jesse'],
                                          'role': ['studio lead']},
            'graph/events/leg02.json': {'base_revision': '6dcab3fcda692f243e9e2d95e3e6af1d90219d84',
                                        'changes': [{'citations': ['self',
                                                                   '6dcab3fcda692f243e9e2d95e3e6af1d90219d84'],
                                                     'developments': [],
                                                     'explanation': "The second source adds Jesse's studio role.",
                                                     'id': 'jesse',
                                                     'kind': 'node',
                                                     'operation': 'edit',
                                                     'support_after': ['6dcab3fcda692f243e9e2d95e3e6af1d90219d84',
                                                                       'self']}],
                                        'format_version': 2,
                                        'handoff_note': {'disposition': 'empty', 'text': ''},
                                        'id': 'leg02',
                                        'input_digest': '19630eb0f3d279d49aa9546f202732050fdef5ce47cdeca969a14a521a1e9401',
                                        'input_mode': 'GENERAL',
                                        'operation': 'add',
                                        'previous_source_event': None,
                                        'source': {'created_at': '2026-01-01T00:00:00Z',
                                                   'id': 'ai84x',
                                                   'kind': 'note',
                                                   'origin': 'legacy-fixture',
                                                   'path': 'foundation/two.md',
                                                   'title': 'two.md'},
                                        'submission_digest': '291e1684b4e4dc86ef4b919a5f5678420ee6edd7ef360fc3a65b395820726525',
                                        'submitted_by': 'legacy-owner'},
            'graph/nodes/jesse.json': {'attributes': {'basic_entity_type': ['person'],
                                                      'name': ['Jesse'],
                                                      'role': ['studio lead']},
                                       'id': 'jesse',
                                       'summary': 'Jesse, who also leads the Berlin studio'}}}]

INIT, FIRST, SECOND = (commit["commit"] for commit in COMMITS)
HEAD = SECOND
AUTHOR = b"Qorqut Context <context@qorqut.invalid>"


def build(root: Path) -> Git:
    """Initialize ``root`` as a checked-out repository holding exactly the recorded format 2 history."""
    root.mkdir(parents=True)
    git = Git(root)
    git.run("init", "-b", "main")
    stream, parent = b"", None
    for number, commit in enumerate(COMMITS, 1):
        message = commit["message"].encode() + b"\n"
        stream += (b"commit refs/heads/main\nmark :%d\nauthor %s %d +0000\ncommitter %s %d +0000\ndata %d\n%s"
                   % (number, AUTHOR, commit["time"], AUTHOR, commit["time"], len(message), message))
        stream += b"from :%d\n" % (number - 1) if parent else b""
        for path, value in commit["files"].items():
            content = encode(value) if isinstance(value, dict) else value.encode()
            stream += b"M 100644 inline %s\ndata %d\n%s\n" % (path.encode(), len(content), content)
        stream += b"\n"
        parent = number
    git.run("fast-import", "--quiet", data=stream)
    if git.run("rev-list", "--reverse", "refs/heads/main").decode().split() != [item["commit"] for item in COMMITS]:
        raise AssertionError("the replayed format 2 history differs from the recorded commits")
    git.checkout(None, HEAD)
    return git
