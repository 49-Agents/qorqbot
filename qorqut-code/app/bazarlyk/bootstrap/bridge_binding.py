"""Process-scoped MCP provenance; shared app-server pipes deliberately stay unbound."""
import json
import os
from pathlib import Path

from .client import path_has_symlink
from .codex_session import owner_alive
from .terminal_chat import native_process


class BridgeBinding:
    def __init__(self,registration,*,ancestor=native_process,proc=Path('/proc')):
        self.registration=registration;self.ancestor=ancestor;self.proc=proc
        self.bound=None;self.reason='native_session_association_unavailable'
        self.parent=None
        for runtime in ('codex','claude-code'):
            try:
                process=ancestor(runtime)
                if not process:continue
                args=(proc/str(process['pid'])/'cmdline').read_bytes().split(b'\0')
                if b'app-server' in args:
                    self.reason='shared_app_server_without_thread_provenance';return
                if any(value in args for value in (b'exec',b'queue',b'--print',b'-p',b'--agent-id')):return
                self.parent=(runtime,f"{process['pid']}:{process['started']}");break
            except (OSError,ValueError):continue

    def unbound(self):
        # A bridge without a verified native binding says why; it never sends a native session or generation.
        return {'X-Qorqut-Binding-Reason':self.reason}

    def headers(self):
        if self.parent is None:return self.unbound()
        if not owner_alive(self.parent[1]):
            self.reason='native_process_binding_lost';return {'X-Qorqut-Binding-Reason':self.reason}
        if self.bound is None:
            path=Path(self.registration['workspace'])/'.bazarlyk/session-binding.json'
            observation=path.with_name('native-binding.json')
            try:
                if path_has_symlink(path) or path_has_symlink(observation):return self.unbound()
                value=json.loads(path.read_text());native=json.loads(observation.read_text())
                if (value['registration_id']!=self.registration['registration_id'] or value['owner']!=self.parent[1]
                        or value['runtime']!=self.parent[0] or native['session_id']!=value['session_id']
                        or native['owner']!=value['owner']):return self.unbound()
                self.bound=(value['session_id'],value['generation'])
            except (OSError,ValueError,KeyError):return self.unbound()
        return {'X-Qorqut-Native-Session':self.bound[0],'X-Qorqut-Binding-Generation':str(self.bound[1])}
