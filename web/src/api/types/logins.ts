import type { Readiness } from './lanes';

/**
 * A form the computer serves for something only a person may type (a vault passphrase, a password, a
 * workspace's passphrase, a file). The app starts it and opens `form_url` on the computer; the app never
 * holds the value. `stage` is how far it got: waiting, done, or ended (it ran out or was closed).
 */
export type FormKind = 'vault_unlock' | 'vault_entry' | 'proxy' | 'workspace_export' | 'workspace_import' | 'login';
export type FormStage = 'waiting' | 'done' | 'ended';
export interface FormTrip {
  id: string;
  kind: FormKind;
  stage: FormStage;
  subject: string | null;
  form_url: string | null;
  expires_at: string;
}

/** One sign-in, as the lane's login service reports it (`GET /api/orgs/{org}/lanes/{ws}/{name}/login`). */
export type LoginState = 'none' | 'working' | 'ask' | 'waiting' | 'stopped' | 'paused' | 'signed_in' | 'cancelled' | 'done';
export type LoginStepKind = 'sign_in' | 'code' | 'approve' | 'choose' | 'confirm' | 'captcha' | 'stuck' | 'signed_in';
export interface LoginField {
  name: string;
  label: string;
  secret: boolean;
  length: number | null;
  autocomplete: string | null;
}
export interface LoginChoice {
  id: string;
  label: string;
  kind: string;
}
export interface LoginStep {
  id: string;
  kind: LoginStepKind;
  heading: string;
  message: string;
  number: string | null;
  fields: LoginField[];
  choices: LoginChoice[];
  submit: string | null;
  reason: string | null;
}
export interface LaneLogin {
  id?: string;
  lane: string;
  state: LoginState;
  site?: string | null;
  site_name?: string | null;
  host?: string | null;
  step?: LoginStep | null;
  note?: string;
  error?: string | null;
  version?: number;
  can_save?: boolean;
  saved?: string | null;
  by?: string | null;
}

/** A browser device: one fingerprint and one proxy, shared by a company's lanes. Listed by the org's browser devices route. */
export interface BrowserDevicePin {
  os: string;
  screen: string;
  window: string;
  gpu: string | null;
  timezone: string;
  locale: string;
  firefox: number;
  pinned_at: string | null;
  pinned_exit_ip: string | null;
  pinned_country: string | null;
}
export interface BrowserDevice {
  name: string;
  proxy: string | null;
  created_at: string;
  from_lane: string | null;
  note: string | null;
  pinned: boolean;
  pin: BrowserDevicePin | null;
  lanes: { id: string; state: string; proxy: string | null; pin_copy: boolean }[];
  workspaces: string[];
}

/** What moving a lane to another device changes, in plain words (`device-move`). */
export interface DeviceMovePlan {
  lane: string;
  from: string | null;
  to: string | null;
  changes: string[];
  warnings: string[];
  confirm: boolean;
  proxy: { from: string | null; to: string | null };
  done?: boolean;
}

export interface QueueLane {
  id: string;
  workspace: string;
  name: string;
  platform: string;
  readiness: Readiness;
  note: string | null;
  login_url: string | null;
}

/** What is left to log in. Counts cover every lane in scope, including ready ones. */
export interface LoginQueue {
  summary: string;
  ready: number;
  total: number;
  needs_login: number;
  needs_attention: number;
  workspaces: { id: string; name: string; summary: string; ready: number; total: number }[];
  lanes: QueueLane[];
}
