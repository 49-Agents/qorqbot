"""Dashboard HTTP tests use the real shared application in temporary storage."""

from pathlib import Path
import os
import sys

import pytest
from starlette.testclient import TestClient

DASHBOARD = Path(__file__).resolve().parents[1]
CORE = Path(os.environ.get("BAZARLYK_CORE_ROOT", DASHBOARD.parent / "biz-core"))
sys.path.insert(0, str(DASHBOARD))
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(CORE / "tests"))

from test_service_transports import ServiceFixture


@pytest.fixture(scope="module")
def connected(tmp_path_factory):
    fixture = ServiceFixture(tmp_path_factory.mktemp("connected-dashboard"), full=True)
    with TestClient(fixture.web) as client:
        fixture.login(client)
        yield fixture, client
