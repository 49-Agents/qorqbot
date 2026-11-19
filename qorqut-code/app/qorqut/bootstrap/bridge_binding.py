"""Process-scoped MCP provenance; shared app-server pipes deliberately stay unbound (a managed host's own app-server is bound)."""
import json
import os
import sys
from pathlib import Path

from .client import path_has_symlink
from .codex_session import owner_alive
from .terminal_chat import native_process


class BridgeBinding:
    def __init__(self,registration,*,ancestor=native_process,proc=Path('/proc'),processes=None):
        self.registration=registration;self.ancestor=ancestor;self.proc=proc
        self.bound=None;self.reason='native_session_association_unavailable'
        self.parent=None
        for runtime in ('codex','claude-code'):
            try:
                process=ancestor(runtime)
                if not process:continue
                if sys.platform == 'darwin':
                    from qorqut import process_info
                    args=[arg.encode() for arg in process_info.cmdline(process['pid'])]
                else:
                    args=(proc/str(process['pid'])/'cmdline').read_bytes().split(b'\0')
                if b'app-server' in args:
                    # A shared server serves many threads and the bridge cannot name one: unbound. The one app-server a managed host
                    # of this agent started serves exactly one thread, so the process is the provenance (the same rule as the
                    # managed claude; the service still checks the binding against its own host identity). Anything else about it,
                    # a different host, a stale or missing record, a reused pid, another command line, stays unbound.
                    try:from .native_binding import managed_host
                    except ImportError:return
                    from .codex_session import runs_app_server
                    if runtime!='codex' or not runs_app_server(args) or managed_host(registration['workspace'],process['pid'],
                            registration['agent_id'],processes=processes,runtime='codex') is None:
                        self.reason='shared_app_server_without_thread_provenance';return
                    self.parent=(runtime,f"{process['pid']}:{process['started']}");break
                if any(value in args for value in (b'exec',b'queue',b'--agent-id')):return
                if any(value in args for value in (b'--print',b'-p')):
                    # A headless client is bound only as the child of a verified managed runtime host; a plain ``claude -p``
                    # (a delegated worker, a diagnostic run) is not.
                    try:from .native_binding import managed_host
                    except ImportError:return
                    if runtime!='claude-code' or managed_host(registration['workspace'],process['pid'],registration['agent_id'],
                                                              processes=processes) is None:return
                self.parent=(runtime,f"{process['pid']}:{process['started']}");break
            except (OSError,ValueError):continue

    def unbound(self):
        # A bridge without a verified native binding says why; it never sends a native session or generation.
        return {'X-Qorqut-Binding-Reason':self.reason}

    def headers(self):
        if self.parent is None:return self.unbound()
        if not owner_alive(self.parent[1]):
            self.reason='native_process_binding_lost';return {'X-Qorqut-Binding-Reason':self.reason}
        # A surviving interactive client can change conversations, and recovery
        # can replace its server-issued generation. Never retain authority from
        # an older receipt after either observation changes.
        self.bound=None
        path=Path(self.registration['workspace'])/'.qorqut/session-binding.json'
        observation=path.with_name('native-binding.json')
        try:
            if path_has_symlink(path) or path_has_symlink(observation):return self.unbound()
            value=json.loads(path.read_text());native=json.loads(observation.read_text())
            if (value['registration_id']!=self.registration['registration_id'] or value['owner']!=self.parent[1]
                    or value['runtime']!=self.parent[0] or native['session_id']!=value['session_id']
                    or native['owner']!=value['owner'] or native['runtime']!=value['runtime']
                    or native['registration_id']!=value['registration_id']
                    or type(value['generation']) is not int or not 1 <= value['generation'] < 2**63):
                return self.unbound()
            self.bound=(value['session_id'],value['generation'])
        except (OSError,ValueError,KeyError,TypeError):return self.unbound()
        return {'X-Qorqut-Native-Session':self.bound[0],'X-Qorqut-Binding-Generation':str(self.bound[1])}
