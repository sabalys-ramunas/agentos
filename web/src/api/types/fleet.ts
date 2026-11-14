import type { Effect, RunStatus } from './lanes';
import type { Schedule } from './runs';

// The fleet map and node depth: every computer, what runs on it, and how it is doing.

export type EntityType = 'agent' | 'cloud' | 'node' | 'workspace' | 'lane' | 'site' | 'brain' | 'proxy' | 'schedule';
export type EntityState = 'ok' | 'attention' | 'blocked' | 'idle' | 'down';
export type LinkKind = 'attached' | 'runs_on' | 'belongs_to' | 'acts_on' | 'exits_via' | 'uses_brain' | 'triggers';

export interface TopologyEntity {
  id: string;
  type: EntityType;
  label: string;
  state: EntityState;
  ref: { href: string } | null;
  /** Free-form and node-supplied, so read with care and render as text. */
  stats: Record<string, unknown>;
}

export interface TopologyLink {
  from: string;
  to: string;
  kind: LinkKind;
  calls: number;
  failed: number;
  last_at: string | null;
}

export interface TopologyActive {
  lane: string;
  principal: string;
  tool: string;
  since: string;
  run_id: string | null;
}

/** A call in the last ten minutes, newest first (api-for-rebuild.md section 6). */
export interface TopologyRecent {
  at: string;
  lane: string;
  principal: string;
  kind: 'agent' | 'flow' | 'human';
  tool: string;
  status: RunStatus;
  /** The cloud's own line: "A routine checked Meridian Travel's Reddit" (section 11). */
  sentence?: string;
  /** "Aster Coffee's Instagram". */
  place?: string;
  site?: string;
  action?: string;
}

export interface Topology {
  generated_at: string;
  window_seconds: number;
  entities: TopologyEntity[];
  links: TopologyLink[];
  active: TopologyActive[];
  /** Older clouds do not send it. */
  recent?: TopologyRecent[];
}

export interface TopologyEvent {
  at: string;
  lane: string;
  principal: string;
  tool: string;
  effect: Effect;
  status: RunStatus;
}

export interface NodeSystem {
  os: string;
  arch: string;
  kernel: string | null;
  cpu_model: string | null;
  cores: number | null;
  mem_total_mb: number | null;
  disk_total_mb: number | null;
  gpu: string | null;
  timezone: string | null;
  uptime_s: number | null;
  version: string;
  enrolled_at: string | null;
  enrolled_by: string | null;
  key_fingerprint: string | null;
  protocol: number;
  reported_at: string | null;
}

export type ProgramScope = 'node' | 'lane' | 'base_image';

export interface NodeProgram {
  name: string;
  version: string;
  source: string;
  scope: ProgramScope;
  lanes: string[];
  pinned_hash: string | null;
  /** Not every node can say whether a program is signed. Absent means not reported, not unsigned. */
  signed?: boolean;
  installed_at: string | null;
}

export interface NodeService {
  name: string;
  lane: string | null;
  state: string;
  restarts: number;
  started_at: string | null;
  /** Not every node keeps a service's ports. Absent means not reported. */
  ports?: number[];
  cpu_pct: number | null;
  mem_mb: number | null;
}

export interface ToolDef {
  name: string;
  description?: string | null;
  effect?: Effect | null;
}

export type CheckStatus = 'ok' | 'warn' | 'fail';

export interface HealthCheck {
  name: string;
  status: CheckStatus;
  detail: string;
  at: string;
  /** A stable id for the check. Absent on a node that predates it. */
  id?: string;
  /** What to do about it, the exact command or edit. Null when nothing needs doing. */
  fix?: string | null;
}

/** Every check, run on request, with the proxies probed too. Read-only. */
export interface HealthRun {
  ran_at: string;
  checks: HealthCheck[];
}

/** The node's own health round, as its daemon last ran it. Null when the daemon does not answer. */
export interface HealthRound {
  at: string | null;
  level: 'ok' | 'warn' | 'critical' | null;
  findings: { key: string; kind: string; level: string; title: string; detail: string }[];
}

export type BackupState = 'verified' | 'failed' | 'not_checked';

export interface BackupCheck {
  state: BackupState;
  at: string | null;
  error: string | null;
}

export interface BackupItem {
  name: string;
  created_at: string;
  size: number;
  check: BackupCheck;
}

/** A backup or a check, running (or ended) in this node process. */
export interface BackupJob {
  id: string;
  kind: 'backup' | 'check';
  name: string | null;
  state: 'running' | 'done' | 'failed';
  message: string;
  started_at: string;
  finished_at: string | null;
}

export interface NodeBackups {
  /** Where the node keeps them, on its own disk. */
  folder: string;
  /** How many of this node's backups are kept. */
  keep: number;
  /** Newest first. */
  items: BackupItem[];
  job: BackupJob | null;
}

/** One trip to the node's own passphrase form. Holds no passphrase: the form is where it is typed. */
export interface PassphraseTrip {
  id: string;
  kind: 'backup' | 'check';
  name: string | null;
  stage: 'waiting' | 'running';
  form_url: string | null;
}

export interface NodeUpgrade {
  running: string;
  /** The newer version this node knows of. It cannot see one, so this is null. */
  newer: string | null;
  command: string;
  python: string;
}

export interface EnvLaneRow {
  id: string;
  kind: string;
  preset: string;
  state: string;
  isolation: string | null;
  network: string;
  base_image: string | null;
  last_used: string | null;
  quarantine_reason: string | null;
}

export interface EnvServiceRow {
  lane: string;
  name: string;
  state: string;
  desired: string;
  health: string;
  restarts: number;
  last_exit: number | null;
  started_at: string | null;
}

export interface ServiceView {
  lane: string;
  desired: string;
  state: string;
  health: string;
  restarts: number;
  last_exit: number | null;
  started_at: string | null;
}

export interface ServiceLog {
  lane: string;
  log: string;
}

export interface NodeEnvironments {
  lanes: EnvLaneRow[];
  services: EnvServiceRow[];
  images: {
    templates: { name: string; description: string; placeholder: boolean }[];
    built: { ref: string; name: string; version: number; built_at: string | null; size_bytes: number }[];
  };
}

export type IdentityState = 'none' | 'off' | 'signing' | 'stale' | 'unreadable';

export interface SignSite {
  origin: string;
  reason: string;
  added_by: string | null;
  added_at: string | null;
}

export interface IdentityAccount {
  lane: string;
  state: IdentityState;
  /** The public key's id. The private key never leaves the node and is never sent. */
  keyid: string | null;
  signature_agent: string | null;
  hosts: SignSite[];
}

export interface NodeIdentity {
  accounts: IdentityAccount[];
}

export interface SettingRow {
  name: string;
  value: string | number | boolean | string[] | null;
  source: string;
  description: string;
}

export interface ScheduleFire {
  at: string;
  status: RunStatus | 'missed';
  run_id?: string | null;
}

export interface NodeSchedule extends Schedule {
  recent_fires: ScheduleFire[];
  next_fires: string[];
}

export type ConnectionKind = 'connected' | 'disconnected' | 'degraded' | 'recovered' | 'backlog_flushed';

export interface NodeConnections {
  uptime_pct: number;
  days: { day: string; up_pct: number; incidents: number }[];
  events: { at: string; kind: ConnectionKind; detail: string | null }[];
}

export type LogSource = 'agentosd' | 'scheduler' | 'channel' | 'lane' | 'service';
export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

export interface LogLine {
  at: string;
  source: LogSource;
  level: LogLevel;
  message: string;
  lane: string | null;
}

export type TopologyWindow = '1h' | '24h';
