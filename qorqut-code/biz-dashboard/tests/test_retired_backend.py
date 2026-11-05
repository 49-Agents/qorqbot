"""server.py is only a launcher: importing it starts nothing and holds no legacy backend, and main() delegates."""

import importlib.util
import sys
import threading
import types
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / 'server.py'


def test_server_is_a_delegating_entry_point_with_no_legacy_backend(monkeypatch):
    threads, modules = threading.active_count(), set(sys.modules)
    spec = importlib.util.spec_from_file_location('dashboard_server_entry', SERVER)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    assert {name for name in vars(server) if not name.startswith('_')} == {'sys', 'main'}
    assert threading.active_count() == threads and not any(name.startswith('bazarlyk') for name in set(sys.modules) - modules)

    received = []
    host = types.ModuleType('bazarlyk.service.main')
    host.main = lambda argv=None: received.append(argv) or len(received)
    for name, module in (('bazarlyk', types.ModuleType('bazarlyk')), ('bazarlyk.service', types.ModuleType('bazarlyk.service')),
                         ('bazarlyk.service.main', host)):
        monkeypatch.setitem(sys.modules, name, module)
    assert server.main(['--port', '1']) == 1 and server.main() == 2
    assert received == [['--port', '1'], None]
