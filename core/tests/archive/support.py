"""Narrow reuse of accepted initialized-agent fixtures."""

from dataclasses import replace
import hashlib
from uuid import uuid4

from agents.support import AgentCase
from qorqut_core.transcripts.archive import ArchiveMetadata
from qorqut_core.transcripts.spool import Chunk, RunBinding, Source


class ArchiveCase(AgentCase):
    def setUp(self):
        super().setUp()
        self.agent = self.rows("agents")[0]
        self.binding = RunBinding(str(self.settings.installation_id), self.agent["id"],
            str(uuid4()), 1, str(uuid4()), str(uuid4()), 1)
        self.source = Source(str(uuid4()), self.binding.installation_id, self.binding.agent_id,
                             "native", "synthetic-native-session")
        self.epoch = str(uuid4())
        self.metadata = ArchiveMetadata(capability_evidence="Synthetic capture; no provider ownership asserted",
            locator="/evidence/only/never-open", file_identity="synthetic inode 7",
            native_created_at="2026-10-07T12:00:00Z")

    def register(self, *, source=None, epoch=None, binding=None, metadata=None, context=None):
        return self.app.archive.register(context or self.context, binding or self.binding,
            source or self.source, epoch or self.epoch, metadata or self.metadata)

    def chunk(self, payload=b"exact\0bytes\xff", offset=0, *, source=None, epoch=None):
        return Chunk((source or self.source).source_id, epoch or self.epoch, offset,
                     len(payload), hashlib.sha256(payload).hexdigest(), payload)

    def receive(self, chunk=None, binding=None):
        return self.app.archive.receive(self.context, binding or self.binding, chunk or self.chunk())

    def state(self, *, source=None, epoch=None):
        return self.app.archive.state(self.context, (source or self.source).source_id, epoch or self.epoch)

    def read(self, offset=0, *, source=None, epoch=None):
        return self.app.archive.read_chunk(self.context, (source or self.source).source_id, epoch or self.epoch, offset)

    def object_path(self, chunk=None, *, conflict=False):
        chunk = chunk or self.chunk()
        folder = self.settings.data_root / "agents" / self.agent["id"] / "history" / "chunks" / chunk.source_id / chunk.source_epoch
        if conflict:
            folder /= "conflicts"
        return folder / f"{chunk.offset}-{chunk.length}-{chunk.sha256}.bin"

    def inventory(self):
        return {table: self.rows(table) for table in ("archive_runs", "archive_sources", "archive_epochs",
            "archive_run_sources", "archive_chunks", "archive_conflicts", "events", "operations", "jobs")}

    def fresh_source(self):
        self.source = replace(self.source, source_id=str(uuid4()))
        self.epoch = str(uuid4())
        self.register()
