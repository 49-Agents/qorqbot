"""The supervisor must distinguish repair-required startup faults from retries."""

from types import SimpleNamespace

import pytest

from bazarlyk.browser import service
from qorqut_operations.contracts import DomainError


@pytest.mark.parametrize("code,expected", [("unsupported_schema", 78), ("invalid_configuration", 78), ("busy", 1)])
def test_startup_failures_do_not_launch_chrome_or_disclose_details(monkeypatch, capsys, code, expected):
    monkeypatch.setattr("sys.argv", ["browser-service", "--root", "/unused", "--business-id", "business", "--runtime", "/unused"])
    monkeypatch.setattr(service.signal, "signal", lambda *args: None)

    def open_existing():
        raise DomainError(code, "sensitive upstream proxy credentials")

    monkeypatch.setattr(service, "Database", SimpleNamespace(
        for_root=lambda root: SimpleNamespace(open_existing=open_existing)))
    monkeypatch.setattr(service, "serve", lambda *args, **kwargs: pytest.fail("Startup failed before Chrome admission"))
    assert service.main() == expected
    assert "sensitive" not in capsys.readouterr().out
