// Frontend wire views of core API v1, based on 055cbd48071d28c03cde3daf1a08521875c130a3.
// Native conversation contract pinned at 35b6d82cd702fe6e82562d7a3c6533ac04050baa; Terminal has its own pin below.
export type CLI = { provider: 'claude' | 'codex'; model: string };
export type Resources = { cpu: number; memory_mib: number; disk_gib: number };
export type CoreError = { code: string; message: string; retryable: boolean };
export type Result<T> = {
  ok: boolean; operation_id: string | null; outcome: 'accepted' | 'completed' | null;
  data: T | null; event_seq: number | null; error: CoreError | null;
};
export type Session = { authenticated: boolean; actor_id: string; api_version: number };
export type Schema = { api_version: number; operations: { name: string; mutation: boolean }[] };
export type Config = {
  installation_id: string; recovery_epoch: string; revision: number;
  defaults: { cli: CLI; resources: Resources }; icon_keys: string[]; api_versions: number[];
};
export type Agent = {
  id: string; actor_id: string; conversation_id: string; display_name: string; icon_key: string;
  kind: 'host' | 'vm'; workspace_path: string; cli: CLI; resources: Resources;
  revision: number; lifecycle_revision: number; lifecycle: string; start_requested: boolean;
  created_by: string; created_at: string; updated_at: string;
  workspace_initialized_at: string | null; projection_revision: number;
  status: { text: string; revision: number; updated_at: string; reviewed_at: string; actor_id: string };
  activity: string; activity_observed_at: string | null; last_contact_at: string | null;
  current_run: unknown | null; capture_health: string; needs_user_input: boolean;
};
export type AgentPage = {
  items: Agent[]; next_after_id: string | null; has_more: boolean;
  recovery_epoch: string; onbash_agent_id: string;
};
export type EventPage = {
  items: { event_seq: number; kind: string; agent_id: string | null }[];
  next_after_seq: number; has_more: boolean; recovery_epoch: string;
};
export type Delivery = {
  message_id: string; recipient_agent_id: string; state: string; reason: string | null; updated_at: string;
};
export type Handling = { message_id: string; handled_by: string; handled_at: string; note: string | null };
export type Entry = {
  entry_id: string; agent_id: string; conversation_id: string; conversation_seq: number;
  kind: string; role: string; direction: string; text: string; sender_actor_id: string | null;
  occurred_at: string; recorded_at: string;
  source:
    | { type: 'message'; message_id: string; run_id: string | null; provider: string | null;
        source_id: string | null; source_epoch: string | null; offset: number | null; length: number | null }
    | { type: 'native'; provider: 'claude' | 'codex'; run_id: string; source_id: string; source_epoch: string;
        offset: number; length: number; sha256: string; parser_version: string; native_id: string;
        part_index: number; timestamp_origin: 'native' | 'ingestion' };
  delivery: Delivery | null; handling: Handling | null;
};
export type Publication = { pending_entries: number; state: 'current' | 'pending' | 'blocked'; error: { code: string; message: string } | null };
export type ConversationPage = {
  agent_id: string; conversation_id: string; recovery_epoch: string; items: Entry[];
  first_seq: number | null; last_seq: number | null; has_older: boolean; has_newer: boolean;
  publication: Publication;
};
export type SendResult = {
  message: { message_id: string; body: string; recipient_agent_id: string };
  delivery: Delivery; entries: { entry_id: string; agent_id: string; conversation_id: string; conversation_seq: number }[];
};

// D38 Terminal protocol 2; geometry contract pinned at 6149b1bd1e82f637e62d9e7e1f5d51ea29b8a8a2.
export type TerminalControl = { mode: 'unclaimed' | 'human'; epoch: number; viewer_id: string | null; last_sequence: number };
export type TerminalDimensions = Readonly<{ columns: number; rows: number }>;
export type TerminalView = {
  agent_id: string; recovery_epoch: string; terminal_id: string | null; observed_at: string | null;
  status: 'disabled' | 'waiting_workspace' | 'available' | 'stopped' | 'unavailable' | 'unsupported_target';
  reason: string; control: TerminalControl | null; dimensions: TerminalDimensions | null;
  recording: { state: 'starting' | 'healthy' | 'blocked' | 'incomplete' | 'unknown'; local_captured_offset: number; host_archived_offset: number } | null;
};
export type TerminalFrame = {
  type: 'frame'; kind: 'input' | 'resize'; viewer_id: string; epoch: number; sequence: number;
  outcome: 'submitted' | 'uncertain'; bytes_written: number;
};
export type TerminalMessage =
  | { type: 'hello'; protocol: 2; viewer_id: string; terminal: TerminalView }
  | ({ type: 'size' } & TerminalDimensions)
  | { type: 'state'; terminal: TerminalView }
  | { type: 'control'; control: TerminalControl }
  | TerminalFrame
  | { type: 'error'; code: string; message: string; related_type: string | null; sequence: number | null };
