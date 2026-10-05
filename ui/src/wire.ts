// Frontend wire views of core API v1, frozen at 055cbd48071d28c03cde3daf1a08521875c130a3.
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
  kind: string; role: string; direction: string; text: string; sender_actor_id: string;
  occurred_at: string; recorded_at: string;
  source: { type: string; message_id: string | null; run_id: string | null; provider: string | null;
    source_id: string | null; source_epoch: string | null; offset: number | null; length: number | null };
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
