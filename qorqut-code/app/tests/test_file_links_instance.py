"""File links round-trip through the v1.2 operations layout without granting file reads."""

from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
import os

import pytest

from bazarlyk.file_viewer.service import FileViewer
from host_foundation_support import build_instance, owner_global_context
from qorqut_operations.contracts import DomainError


def test_business_and_agent_links_keep_the_operations_relative_prefix(tmp_path, monkeypatch):
    instance, _ = build_instance(tmp_path, businesses=("acme",))
    viewer = FileViewer(instance.runtime, lambda business: instance.human(business) if business else owner_global_context(instance))
    handler = instance.runtime.registry.get("files_link").read
    try:
        for relative in ("businesses/acme/hq/notes/report.md", "agents/researcher/notes/report.md"):
            absolute = instance.paths.operations / relative
            for path in (str(absolute), relative):
                # Link construction succeeds even before creation and must not inspect the filesystem.
                def no_filesystem(*args, **kwargs):
                    raise AssertionError("files_link inspected the filesystem")
                with monkeypatch.context() as patch:
                    for name in ("stat", "lstat", "listdir", "scandir"):
                        patch.setattr(os, name, no_filesystem)
                    patch.setattr(Path, "open", no_filesystem)
                    result = handler(owner_global_context(instance), {"path": path}, None, None)
                assert result["checked"] == "not_checked"
                assert result["target"]["path"] == relative
                absolute.parent.mkdir(parents=True, exist_ok=True)
                absolute.write_text("# Report\n")
                selector = viewer.selector(parse_qsl(urlsplit(result["viewer_url"]).query))
                assert viewer.metadata(selector)["path"] == relative
                assert "Report" in viewer.content(selector)["html"]
        old = viewer.selector([("path", "acme/hq/notes/report.md")])
        with pytest.raises(DomainError) as failure:
            viewer.metadata(old)
        assert failure.value.code == "not_found", "Do not silently guess another root"
    finally:
        instance.runtime.stop()
