"""Packing reduces duplicate snapshot storage without changing history/content."""
from qorqut_operations.history_exports._git import Git, REGULAR


def test_pack_preserves_commits_and_content(tmp_path):
    repo=tmp_path/'history.git'
    git=Git(repo)
    git.create()
    bodies=[b'unchanged data\n'*100000+b'first', b'unchanged data\n'*100000+b'second']
    commits=[]
    for index, body in enumerate(bodies):
        blob=git.write_blob(data=body)
        tree=git.build_tree({'database/table.jsonl':(REGULAR,blob)},tmp_path)
        commit=git.commit(tree,git.head(),'snapshot','@1700000000 +0000')
        git.advance(commit,git.head());commits.append(commit)
    assert git.pack_if_needed() is False
    assert git.pack_if_needed(max_loose_kib=0) is True
    assert git.head()==commits[-1]
    assert list((repo/'objects/pack').glob('*.pack'))
    for commit,body in zip(commits,bodies):
        assert git.run('show',commit+':database/table.jsonl')[0]==body
    assert git.run('rev-list','--count','refs/heads/history')[0].strip()==b'2'
