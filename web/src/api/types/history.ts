import type { ConfirmLevel, ConfirmMethodId, PreviewData, PreviewKind } from './approvals';
import type { Cost, Effect, Principal, RunStatus } from './lanes';
import type { WorkerThing } from './workers';

// History as stories (api-for-rebuild.md, section 9): `GET /api/orgs/{org}/history`.

export type HistoryAction = 'post' | 'publish' | 'reply' | 'draft' | 'read' | 'pay' | 'change' | 'task';
export type HistoryVerdict = 'checked' | 'not_checked' | 'partial' | 'failed' | 'not_run' | 'running';
export type HistoryShow = 'everything' | 'posts' | 'problems' | 'spending';

export interface HistoryWho {
  kind: Principal['kind'];
  id: string;
  /** "Claude Code", a routine as "Instagram post", a person by name. */
  name: string;
  brand: string | null;
}

/**
 * What kind of work a story was, for History's filters: social posting is one use case of many.
 * Optional: a story without it is placed from its `action`.
 */
export type StoryKind = 'sent' | 'money' | 'forms' | 'research' | 'shop' | 'files';

/** The thing itself, shown as the proof: what was sent, saved, paid, submitted, found or run. */
export type StoryArtifact =
  | {
      type: 'email';
      from: string;
      to: string[];
      cc?: string[];
      subject: string;
      body: string;
      attachments?: { name: string; size: number }[];
    }
  | { type: 'files'; folder?: string | null; files: { name: string; size: number; url?: string | null }[] }
  | { type: 'payment'; amount: string; payee: string; for?: string | null; from_account?: string | null; reference?: string | null }
  | {
      type: 'form';
      title: string;
      fields: { label: string; value: string; before?: string | null; changed?: boolean }[];
      confirmation?: string | null;
    }
  | { type: 'table'; title: string; columns: string[]; rows: { cells: string[]; change?: 'up' | 'down' | 'new' | null }[]; summary?: string | null }
  | {
      type: 'order';
      number: string;
      customer?: string | null;
      items: { name: string; quantity: number; price: string }[];
      total: string;
      refund?: boolean;
      note?: string | null;
    }
  | { type: 'message'; thread: { from: string; text: string; at?: string | null; ours?: boolean }[]; rating?: number | null }
  | { type: 'command'; command: string; output?: string | null; computer?: string | null };

export interface HistoryStory {
  /** The run id, or the trace id for a call a real node made without a run. */
  id: string;
  source: 'run' | 'call';
  at: string;
  started_at: string;
  lane: string;
  workspace: string | null;
  workspace_name: string | null;
  /** "Aster Coffee's Instagram". */
  place: string;
  site: { key: string; label: string };
  action: HistoryAction;
  /** "Posted a photo on Harbor Supply's Instagram". */
  sentence: string;
  verdict: HistoryVerdict;
  verdict_text: string;
  reason: string | null;
  /** The node's own words, for Technical details. */
  detail: string | null;
  status: RunStatus;
  proof: { url: string | null; screenshot: string | null } | null;
  who: HistoryWho;
  /** `on_its_own`: nobody was asked, so it ran on its own (api-for-rebuild.md 20). */
  why: { trigger: string | null; text: string | null; approval: { by: string | null; at: string; state: string; method?: ConfirmMethodId | null; level?: ConfirmLevel | null; where?: string | null } | null; on_its_own?: boolean };
  steps: { id: string; at: string; label: string; status: RunStatus }[];
  technical: { run_id: string | null; flow?: string | null; tool?: string | null; effect: Effect | null; cost: Cost };
  /** Working now only: "Claude Code is posting on Aster Coffee's Instagram". */
  headline?: string;
  /** What kind of work, for the filters. Placed from `action` when the cloud does not say. */
  kind?: StoryKind | null;
  /** Concept stage: a worker did the steps, and these are the steps. */
  worker?: WorkerThing | null;
  /** What was sent, saved, paid, submitted, found or run, as the proof. */
  artifact?: StoryArtifact | null;
  /** The same structured preview an approval carries, when the cloud sends that instead. */
  preview?: { kind?: PreviewKind | null; data?: PreviewData | null } | null;
}

export interface HistoryReads {
  times: number;
  accounts: number;
  /** "Also checked 28 accounts 398 times." */
  sentence: string;
  by_account: { lane: string; place: string; site: { key: string; label: string }; times: number; last_at: string }[];
}

export interface HistoryDay {
  /** The day in the person's time zone, `2026-11-04`. */
  date: string;
  label: string;
  /** "12 things done · 11 checked · 1 payment · 2 problems". */
  summary: string;
  done: number;
  checked: number;
  problems: number;
  payments: number;
  stories: HistoryStory[];
  reads: HistoryReads | null;
}

export interface HistoryPage {
  timezone: string;
  counts: Record<HistoryShow, number>;
  working_now: HistoryStory[];
  days: HistoryDay[];
  next_before: string | null;
  retention_days: number;
}
