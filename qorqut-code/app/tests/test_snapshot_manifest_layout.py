"""Authored snapshot business-path validation: exactly the offline source shape and the target-instance shape."""

import pytest

from qorqut_operations.contracts import canonical_hash

from bazarlyk.migration.authority import AuthorityError
from bazarlyk.migration.snapshots import SourceManifest
from bazarlyk.storage.recovery import require_target_businesses

SHA = "a" * 64
FILE = {"path": "first/hq/business.yaml", "size": 3, "mode": 0o644, "sha256": SHA}


def manifest(*businesses, files=(FILE,)):
    return {"schema_version": 1, "importer_version": "1", "source_root": "/srv/root", "files": [dict(item) for item in files],
            "businesses": [dict(item) for item in businesses]}


def source(slug):
    return {"slug": slug, "hq": f"{slug}/hq"}


def target(slug):
    return {"slug": slug, "hq": f"projects/{slug}/hq", "root": f"projects/{slug}"}


def previous(slug):
    return {"slug": slug, "hq": f"businesses/{slug}/hq", "root": f"businesses/{slug}"}


def test_the_offline_source_shape_keeps_its_exact_fingerprint_and_bytes():
    data = manifest(source("first"), source("second"))
    parsed = SourceManifest(data)
    assert parsed.to_dict() == data and parsed.fingerprint == canonical_hash(data)
    # Computed with the validator exactly as it was before the target shape existed (host 805da0e8).
    assert parsed.fingerprint == "35091547e6b1d3291686af0ff960d1f55d9bf9478b0fa11ea4eb507b10634b7e"


def test_the_previous_instance_layout_is_read_and_fingerprinted_unchanged():
    data = manifest(previous("first"), previous("second"))
    parsed = SourceManifest(data)
    assert parsed.to_dict() == data and parsed.fingerprint == canonical_hash(data)


def test_the_target_instance_shape_is_accepted_and_serialized_unchanged():
    data = manifest(target("first"), target("second"))
    parsed = SourceManifest(data)
    assert parsed.to_dict() == data and parsed.fingerprint == canonical_hash(data)
    assert SourceManifest(manifest()).to_dict() == manifest()               # no businesses: neither shape is implied


@pytest.mark.parametrize("businesses", [
    [source("first"), target("second")],                                      # mixed shapes
    [previous("first"), target("second")],
    [target("first"), previous("second")],
    [previous("first"), previous("first")],
    [target("first"), source("second")],
    [target("first"), target("first")],                                       # duplicate slug
    [source("first"), source("first")],
    [{"slug": "first", "hq": "projects/other/hq"}],                         # path / slug disagreement
    [{"slug": "first", "hq": "other/hq"}],
    [{"slug": "first", "hq": "projects/first"}],
    [{"slug": "first", "hq": "projects/first/hq/extra"}],
    [{"slug": "first", "hq": "other/projects/first/hq"}],
    [{"slug": "first", "hq": "/projects/first/hq"}],                        # absolute
    [{"slug": "first", "hq": "projects/../first/hq"}],                      # traversal
    [{"slug": "first", "hq": "projects\\first\\hq"}],
    [{"slug": "first", "hq": "projects//first/hq"}],
    [{"slug": "first", "hq": ""}],
    [{"slug": "first", "hq": None}],
    [{"slug": 7, "hq": "projects/7/hq"}],
    [{"hq": "projects/first/hq"}],
    ["first"],
])
def test_invalid_mixed_or_duplicate_business_metadata_is_refused(businesses):
    with pytest.raises(AuthorityError) as raised:
        SourceManifest({**manifest(), "businesses": businesses})
    assert raised.value.code == "snapshot_invalid"


def test_every_other_manifest_protection_is_unchanged():
    for broken in ({"schema_version": 2}, {"importer_version": "2"}, {"source_root": "relative"}, {"files": "no"},
                   {"files": [FILE, FILE]}, {"files": [{**FILE, "path": "../escape"}]}, {"files": [{**FILE, "sha256": "x"}]},
                   {"files": [{**FILE, "mode": 0o10000}]}):
        with pytest.raises(AuthorityError):
            SourceManifest({**manifest(target("first")), **broken})


def test_a_current_backup_must_bind_its_businesses_to_the_captured_database_and_the_target_layout(tmp_path):
    import context_completion_support as support
    host = support.build(tmp_path)
    snapshot = tmp_path / "snapshot.sqlite3"
    host.database.backup(snapshot)
    from bazarlyk.storage.recovery import business_entries
    from bazarlyk.storage.exports import snapshot_connection
    connection = snapshot_connection(snapshot)
    try:
        expected = business_entries(connection, host.paths)
    finally:
        connection.close()
    assert {item["hq"] for item in expected} == {"projects/first/hq", "projects/second/hq"}
    require_target_businesses(SourceManifest(manifest(*expected)), snapshot, host.paths)
    tampered = [
        [source("first"), source("second")],                                   # a recognized source archive is not a target backup
        [previous("first"), previous("second")],                               # nor is a previous-layout archive: it needs an explicit conversion
        expected[:1],                                                          # a missing registered business
        [*expected, target("third")],                                          # an unregistered one
        list(reversed(expected)),
        [{**expected[0], "root": "projects/elsewhere"}, expected[1]],
        [{k: v for k, v in expected[0].items() if k != "root"}, expected[1]],
    ]
    for businesses in tampered:
        with pytest.raises(ValueError):
            require_target_businesses(SourceManifest(manifest(*businesses)), snapshot, host.paths)
