"""Real isolated Global agent terminals; the test owner uses a shell, never Claude."""
import argparse
import json
from pathlib import Path
import socket
import tempfile

import uvicorn

from qorqut_operations.contracts import new_id
from bazarlyk.service.main import load_terminal_broker
from bazarlyk.service.web import create_app
from bazarlyk.transport.compatibility import DashboardCompatibility
from bazarlyk.transport.terminals import TerminalService
from test_global_agents import global_agent
from test_service_transports import ServiceFixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dashboard-root', type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='global-agents-browser-') as temporary, socket.socket() as listener:
        root = Path(temporary)
        fixture = ServiceFixture(root, full=True)
        applier = global_agent(fixture)
        second = global_agent(fixture, 'second-applier')
        fixture.fixture.create('learning-applier')  # Same name, different owning scope.
        listener.bind(('127.0.0.1', 0)); listener.listen(128)
        origin = 'http://127.0.0.1:' + str(listener.getsockname()[1])
        broker = load_terminal_broker(args.dashboard_root.parent)
        if not broker.tmux_available(): raise RuntimeError('Isolated terminal fixture needs tmux')
        broker.TMUX_SOCK = 'global-browser-' + new_id()
        config = root / 'tmux.conf'; config.write_text(broker.TMUX_CONF)
        broker._conf_path = lambda: str(config)
        terminals = TerminalService(fixture.runtime, broker, service_url=origin + '/mcp', version_provider=lambda: '2.1.263')
        app = create_app(fixture.runtime, dashboard_root=args.dashboard_root, origins=[origin], authentication=fixture.authentication,
            compatibility=DashboardCompatibility(fixture.runtime, background=fixture.runtime.background), terminals=terminals)
        print(json.dumps({'url': origin, 'origin': origin, 'workspace': str(fixture.runtime.workspace),
            'applier_id': applier.id, 'second_id': second.id}), flush=True)
        try:
            uvicorn.Server(uvicorn.Config(app, log_level='warning', access_log=False)).run(sockets=[listener])
        finally:
            for key in broker.keys(): broker.stop(key)
            broker.tmux('kill-server')


if __name__ == '__main__': main()
