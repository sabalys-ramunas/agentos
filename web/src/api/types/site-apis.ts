import type { Effect } from './lanes';

/*
 * Site APIs. Concept stage: only the mock answers these, and they
 * will move when the node's report is pinned. `pay` and `irreversible` are refused at save, so they
 * are not in this type at all.
 */
export type SiteApiEffect = Extract<Effect, 'read' | 'draft' | 'commit'>;
export type SiteApiStage = 'proposed' | 'active' | 'paused';
/** What a person needs to know right now. Each has its own words and its one next step. */
export type SiteApiHealth = 'good' | 'changed' | 'sign_in' | 'site_check' | 'resting' | 'paused' | 'empty' | 'unread';
export type SiteFieldKind = 'text' | 'money' | 'date' | 'count' | 'status' | 'link';

export interface SiteField {
  key: string;
  label: string;
  kind: SiteFieldKind;
}

/** A column on the page. `field` is null when it was left out; `values` then holds what it said. */
export interface SitePageColumn {
  label: string;
  field: string | null;
  values?: string[];
}

/** Another thing the computer noticed on the page, for Teach it. */
export interface SitePageRegion {
  id: string;
  /** "a table of 42 orders" */
  words: string;
  sure: 'sure' | 'likely' | 'unsure';
  kind: 'main' | 'side' | 'nav';
}

/**
 * The page as the computer saw it: its structure and its text, never its HTML. Text is shown as
 * text, and nothing here carries a cookie or a session.
 */
export interface SitePage {
  url: string;
  wordmark: string;
  nav: string[];
  heading: string;
  count_words: string;
  layout: 'table' | 'cards' | 'thread';
  columns: SitePageColumn[];
  /** The short notes down the side of the page, when it has them. */
  side: string[];
  seen_at: string;
}

export type SiteRow = Record<string, string>;

export interface SiteInputField {
  key: string;
  label: string;
  type: 'choice' | 'text' | 'number' | 'date';
  options?: { value: string; label: string }[];
  default?: string;
  required?: boolean;
  hint?: string;
}

export interface SiteEndpoint {
  key: string;
  title: string;
  description: string;
  effect: SiteApiEffect;
  input: SiteInputField[];
  fields: SiteField[];
  /** The last good result. */
  rows: SiteRow[];
  /** How many there were on the page, which can be more than `rows` holds. Null when the node does not know. */
  total: number | null;
  /** Row indexes that are new since the read before. */
  new_rows?: number[];
  cache_s: number;
  pace_per_hour: number;
  expect_min: number | null;
  calls_7d: number;
  /** Null when no call was made in the week. */
  cached_pct: number | null;
  /** The median call time over the hours with calls, or null when there were none. */
  p50_ms: number | null;
  /** The page as the computer last saw it. Null until a read has kept one. */
  page: SitePage | null;
}

export type SiteSample =
  | { kind: 'rows'; columns: string[]; rows: string[][] }
  | { kind: 'aging'; total: string; note: string; items: { who: string; amount: string; days: number }[] }
  | { kind: 'levels'; items: { label: string; left: number; reorder: number; unit: string }[] }
  | { kind: 'journey'; parcel: string; stops: string[]; at: number; note: string }
  | { kind: 'quotes'; items: { who: string; text: string; when: string }[] };

/** Why the API is not answering, in facts. The words are the panel's (`features/site-apis/words.ts`). */
export interface SiteProblem {
  since: string;
  /** The field that moved or went missing. */
  field?: string;
  /** What the page says now, where the field was. */
  now_label?: string;
  now_values?: string[];
  /** When the site's rest ends. */
  resumes_at?: string;
  expected?: number;
  found?: number;
  /** Who paused it, as a name. */
  by?: string;
}

export interface SiteProposal {
  /** Who proposed it, as a name. */
  by: string;
  at: string;
  /** How many rows the agent read to try it, when the node kept that. */
  tried_rows?: number;
  /** What the node recorded it would do, in the node's words. */
  steps: string[];
}

export interface SiteApi {
  key: string;
  /** The job, as a person would say it. */
  title: string;
  site: { name: string; domain: string };
  /** One plain sentence of what it gives you. */
  words: string;
  workspace: string;
  /** The one account it runs on, and only that one. */
  lane: string;
  account: string;
  stage: SiteApiStage;
  health: SiteApiHealth;
  problem: SiteProblem | null;
  proposal: SiteProposal | null;
  sample: SiteSample;
  /** Calls by six hours over the week: cached, fresh, errors. */
  spark: { cached: number; fresh: number; errors: number }[];
  calls_7d: number;
  /** Null when no call was made in the week. */
  cached_pct: number | null;
  read_at: string | null;
  version: string;
}

export interface SitePace {
  per_hour: number;
  /** Reads of this Site API in the last hour. */
  used: number;
  /** What else used this account's pace in the last hour. */
  others: { label: string; used: number }[];
  /** Reads in each of the last twelve hours, oldest first. */
  hours: number[];
}

export interface SiteApiDetail extends SiteApi {
  endpoints: SiteEndpoint[];
  pace: SitePace;
  computer: string;
  /** How often it is read for a person who asked to keep it updated. */
  keep_updated: 'off' | 'hourly' | 'morning';
}

export interface SiteUsageHour {
  at: string;
  /** Per endpoint key. */
  calls: Record<string, { cached: number; fresh: number; errors: number }>;
}

export interface SiteKey {
  id: string;
  label: string;
  prefix: string;
  apis: string[];
  /** Null reaches every endpoint of those APIs. */
  endpoints: string[] | null;
  ceiling: SiteApiEffect;
  rate_per_min: number;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

export interface SiteKeyInput {
  label: string;
  apis: string[];
  endpoints: string[] | null;
  ceiling: SiteApiEffect;
  rate_per_min: number;
  expires_at: string | null;
}

/** Shown once. Only the hash is kept after this. */
export interface SiteKeyCreated extends SiteKey {
  secret: string;
}

/** What a person can do to a Site API. Only a person: an agent can propose, never these. */
export type SiteAction =
  | { kind: 'read' }
  | { kind: 'pause' }
  | { kind: 'resume' }
  | { kind: 'activate' }
  | { kind: 'decline' }
  | { kind: 'repair' }
  | { kind: 'accept-empty' }
  | { kind: 'keep-updated'; every: SiteApiDetail['keep_updated'] };

export interface SiteTryResult {
  status: number;
  ms: number;
  headers: Record<string, string>;
  body: unknown;
}

export interface SiteDiscovery {
  lane: string;
  /** The Site API this page already belongs to on this account, or empty when none does. */
  api: string;
  site: { name: string; domain: string };
  page: SitePage;
  regions: SitePageRegion[];
  rows: SiteRow[];
  total: number;
  /** Pages this account visits most, to start from. */
  suggestions: { url: string; words: string }[];
}
