/**
 * Every enum the API sends, mapped once to honest, plain words and a status tone. The words come
 * from `vocabulary.ts`. Run statuses and effects keep their older words until History and Needs
 * you are rebuilt around the plain ones (their charts and groups change with them).
 */
import type {
  BrainState,
  Effect,
  FileBox,
  FileStatus,
  Health,
  LaneState,
  MemoryState,
  MemoryTrust,
  MemoryType,
  NodeState,
  Role,
  RunStatus,
  Signal,
  StandingState,
  VaultKind,
  WebhookDelivery,
} from '@/api/types';
import { SITES } from './sites';
import { LANE_STATE_WORDS, NODE_STATE_WORDS, SIGNAL_WORDS } from './vocabulary';

/**
 * A status tone. Each is a shape as well as a colour (components/ui/status.tsx): ok a dot, warn a
 * diamond, bad a square, live a ringed dot (pulsing when `pulse`), idle a hollow ring, neutral a
 * quiet solid dot. In Mono running is ink, so `live` has no hue. `info` is the old name for live and
 * draws the same; new code says `live`.
 */
export type Tone = 'ok' | 'warn' | 'bad' | 'live' | 'info' | 'neutral' | 'idle';

export interface StatusLabel {
  label: string;
  tone: Tone;
  pulse?: boolean;
}

export const runStatus: Record<RunStatus, StatusLabel> = {
  queued: { label: 'Queued', tone: 'neutral' },
  running: { label: 'Running', tone: 'live', pulse: true },
  waiting_approval: { label: 'Waiting for you', tone: 'warn' },
  succeeded_verified: { label: 'Verified', tone: 'ok' },
  succeeded_unverified: { label: 'Unverified', tone: 'neutral' },
  partial: { label: 'Partial', tone: 'warn' },
  failed: { label: 'Failed', tone: 'bad' },
  cancelled: { label: 'Cancelled', tone: 'idle' },
};

export const laneState: Record<LaneState, StatusLabel> = {
  cold: { label: LANE_STATE_WORDS.cold.word, tone: 'idle' },
  warming: { label: LANE_STATE_WORDS.warming.word, tone: 'live', pulse: true },
  warm: { label: LANE_STATE_WORDS.warm.word, tone: 'ok' },
  active: { label: LANE_STATE_WORDS.active.word, tone: 'live', pulse: true },
  quarantined: { label: LANE_STATE_WORDS.quarantined.word, tone: 'bad' },
};

export const signal: Record<Signal, StatusLabel> = {
  ok: { label: SIGNAL_WORDS.ok.word, tone: 'ok' },
  captcha: { label: SIGNAL_WORDS.captcha.word, tone: 'warn' },
  checkpoint: { label: SIGNAL_WORDS.checkpoint.word, tone: 'warn' },
  rate_limited: { label: SIGNAL_WORDS.rate_limited.word, tone: 'warn' },
  blocked: { label: SIGNAL_WORDS.blocked.word, tone: 'bad' },
  logged_out: { label: SIGNAL_WORDS.logged_out.word, tone: 'warn' },
  unknown: { label: SIGNAL_WORDS.unknown.word, tone: 'idle' },
};

export const health: Record<Health, StatusLabel> = {
  ok: { label: 'OK', tone: 'ok' },
  attention: { label: 'Attention', tone: 'warn' },
  blocked: { label: 'Blocked', tone: 'bad' },
  idle: { label: 'Idle', tone: 'idle' },
};

export const nodeState: Record<NodeState, StatusLabel> = {
  up: { label: NODE_STATE_WORDS.up.word, tone: 'ok' },
  degraded: { label: NODE_STATE_WORDS.degraded.word, tone: 'warn' },
  down: { label: NODE_STATE_WORDS.down.word, tone: 'bad' },
};

export const brainState: Record<BrainState, StatusLabel> = {
  ready: { label: 'Ready', tone: 'ok' },
  cooling_down: { label: 'Cooling down', tone: 'warn' },
  needs_auth: { label: 'Needs sign-in', tone: 'warn' },
  down: { label: 'Down', tone: 'bad' },
};

export const effectLabel: Record<Effect, string> = {
  read: 'Read',
  draft: 'Draft',
  commit: 'Send or publish',
  pay: 'Payment',
  irreversible: 'Irreversible',
  secret: 'Login',
  exec: 'Run command',
  install_overlay: 'Install on an account',
  install_base: 'Install on base image',
  service: 'Run service',
  brain: 'Model spend',
  agent: 'Talk to an agent',
  vm: 'Rent a cloud machine',
};

/** Heaviest first: Needs you shows the groups in this order. */
export const effectOrder: Effect[] = [
  'irreversible',
  'pay',
  'vm',
  'install_base',
  'commit',
  'secret',
  'exec',
  'install_overlay',
  'service',
  'brain',
  'agent',
  'draft',
  'read',
];

/** The core's PRESET_LABELS: the names people see for isolation presets. */
export const presetLabel: Record<string, { name: string; hint: string }> = {
  account: { name: 'Sealed account', hint: "One account's own browser, network route and device, with no shell" },
  workbench: { name: 'Shared desk', hint: 'A trusted terminal that shares the node and installs freely' },
  sandbox: { name: 'Scratch', hint: 'A throwaway space with no network and nothing kept' },
  service: { name: 'Service', hint: 'A long-running program with its own disk and an allowlist' },
  locked: { name: 'Locked down', hint: 'Nothing gets out unless allowed, and only signed programs run' },
};

export const roleLabel: Record<Role, string> = {
  owner: 'Owner',
  admin: 'Admin',
  member: 'Member',
  approver: 'Approver',
  viewer: 'Viewer',
  auditor: 'Auditor',
};

export const roleHint: Record<Role, string> = {
  owner: 'Billing, and can delete the org',
  admin: 'Manages members, nodes and grants',
  member: 'Works in assigned workspaces',
  approver: 'Approves in assigned workspaces',
  viewer: 'Read only. Live view is for members',
  auditor: 'Reads the evidence, and nothing else',
};

export const vaultKind: Record<VaultKind, string> = {
  password: 'Password',
  totp: 'One-time code',
  cookie: 'Session cookie',
  api_key: 'API key',
};

/** A lane's name as a person reads it: the site's own name when it is one ("LinkedIn"). */
export function laneName(name: string): string {
  return SITES.get(name)?.name ?? name.charAt(0).toUpperCase() + name.slice(1).replaceAll('-', ' ');
}

export const deliveryState: Record<WebhookDelivery['state'], StatusLabel> = {
  pending: { label: 'Waiting', tone: 'neutral' },
  sending: { label: 'Sending', tone: 'live', pulse: true },
  delivered: { label: 'Delivered', tone: 'ok' },
  failed: { label: 'Failed', tone: 'bad' },
  dropped: { label: 'Dropped', tone: 'idle' },
};

export const webhookEvent: Record<WebhookDelivery['kind'], { label: string; hint: string }> = {
  'approval.requested': { label: 'Approval requested', hint: 'Something needs a person. Carries a Review link that lasts 30 minutes.' },
  'approval.decided': { label: 'Approval decided', hint: 'Someone approved or denied it.' },
  'approval.expired': { label: 'Approval expired', hint: 'Nobody decided in time, so nothing happened.' },
  'run.failed': { label: 'Run failed', hint: 'A run failed or only partly worked.' },
  ping: { label: 'Test', hint: 'Sent from this page.' },
};

export const standingState: Record<StandingState, StatusLabel> = {
  active: { label: 'Active', tone: 'ok' },
  used_up: { label: 'Used up', tone: 'idle' },
  expired: { label: 'Expired', tone: 'idle' },
  revoked: { label: 'Revoked', tone: 'idle' },
};

/**
 * Memory. Who wrote an entry is a fact about it, not a status, so none of these carries a tone. The untrusted
 * one says where the text came from, because that is what a person needs to judge it.
 */
export const memoryTrust: Record<MemoryTrust, string> = {
  owner: 'Confirmed by you',
  node: 'Written by agentos',
  agent: 'Written by an agent',
  untrusted: 'Written after reading outside text',
};

export const memoryType: Record<MemoryType, string> = {
  fact: 'Fact',
  document: 'Document',
  decision: 'Decision',
  preference: 'Preference',
  contact: 'Contact',
  run_summary: 'Run summary',
};

export const memoryState: Record<MemoryState, StatusLabel> = {
  live: { label: 'Kept', tone: 'neutral' },
  expired: { label: 'Expired', tone: 'idle' },
  forgotten: { label: 'Forgotten', tone: 'idle' },
};

/** The two places an account's files are kept. The inbox is what can be attached; the outbox is what came in. */
export const fileBox: Record<FileBox, string> = {
  inbox: 'Ready to use',
  outbox: 'Downloaded',
};

export const fileStatus: Record<FileStatus, StatusLabel> = {
  complete: { label: 'Complete', tone: 'neutral' },
  downloading: { label: 'Downloading', tone: 'live', pulse: true },
  partial: { label: 'Stopped part way', tone: 'warn' },
  failed: { label: 'Failed', tone: 'bad' },
};

/** A computer's service (a supervised command in an environment). The node's own state words. */
export const serviceState: Record<string, StatusLabel> = {
  running: { label: 'Running', tone: 'ok' },
  starting: { label: 'Starting', tone: 'live', pulse: true },
  stopping: { label: 'Stopping', tone: 'neutral' },
  stopped: { label: 'Stopped', tone: 'idle' },
  backoff: { label: 'Restarting', tone: 'warn', pulse: true },
  failed: { label: 'Failed', tone: 'bad' },
};

/** Whether a backup checked out, as the node recorded it. */
export const backupCheck: Record<'verified' | 'failed' | 'not_checked', StatusLabel> = {
  verified: { label: 'Checked', tone: 'ok' },
  failed: { label: 'Did not check out', tone: 'bad' },
  not_checked: { label: 'Not checked yet', tone: 'idle' },
};

/** An account's Web Bot identity: whether it signs its requests, and to how many sites. */
export const identityState: Record<'none' | 'off' | 'signing' | 'stale' | 'unreadable', StatusLabel> = {
  signing: { label: 'Signing', tone: 'ok' },
  off: { label: 'Off', tone: 'idle' },
  none: { label: 'Not set up', tone: 'neutral' },
  stale: { label: 'Needs a new key', tone: 'warn' },
  unreadable: { label: 'Cannot be read', tone: 'bad' },
};
