/**
 * One shape for a thing that happened, whichever way it reached the panel: a story the cloud
 * already told (`GET /history`), or a run read on its own (`GET /runs/{id}`, from a routine's
 * page or an old link) and told here. The list rows and the detail render only this.
 */
import type { ConfirmLevel, ConfirmMethodId, HistoryAction, HistoryStory, PreviewData, PreviewKind, Principal, Run, RunStatus, StoryArtifact, StoryKind, WorkerThing } from '@/api/types';
import { asPrincipal } from '@/lib/principal';
import { siteOf } from '@/lib/sites';
import type { Result, Story } from './story';
import { capital, lower, OWN_WORDS, reasonOf, routineSubject, stepWords } from './story';

export interface StoryView {
  /** The run id, or the trace id for a call made without a run. */
  id: string;
  runId: string | null;
  lane: string;
  workspace: string;
  business: string;
  siteKey: string;
  /** The site on its own in a sentence: "Instagram", "the blog". */
  named: string;
  /** The site as a label: "Instagram", "Blog". */
  siteLabel: string;
  at: string;
  startedAt: string;
  /** "Posted a photo on Harbor Supply's Instagram". */
  sentence: string;
  result: Result;
  status: RunStatus;
  /** The pill: "Live, checked", "Not checked", "Did not work". */
  label: string;
  /** Under the sentence in the list: what did not happen and why. */
  note: string | null;
  /** The detail's bold line, and its reason. */
  verdict: string;
  because: string;
  /** A person can fix it on the account (signed out, a robot check). */
  accountProblem: boolean;
  /** Looking only, so it was never going to leave a picture. */
  lookOnly: boolean;
  proof: { url: string | null; screenshot: string | null } | null;
  steps: { id: string; at: string; label: string; status: RunStatus }[];
  who: { principal: Principal; name: string };
  /** "On its schedule", "Asked from a chat". */
  why: string | null;
  /** "Demo Owner at 11:30", or null when nobody had to say yes. */
  approvedBy: { by: string | null; at: string } | null;
  /** How the yes arrived, when the computer said: where it was given and how strong it was. */
  yes: { by: string; at: string; method: ConfirmMethodId; level: ConfirmLevel; where: string | null } | null;
  /** Done without anyone being asked: the normal case, said where it went out. */
  onItsOwn: boolean;
  approvalId: string | null;
  technical: { cost: { tokens: number; usd: number }; what: string | null; detail: string | null };
  /** What kind of work, for the filters, or null when it is none of them (a routine look). */
  kind: StoryKind | null;
  /** The thing itself, as proof: the email, the files, the payment, the form, the table. */
  artifact: StoryArtifact | null;
  /** A worker did the steps, and these are the steps. */
  worker: WorkerThing | null;
}

const yesOf = (approval: HistoryStory['why']['approval']): StoryView['yes'] =>
  approval?.state === 'approved' && approval.method && approval.level
    ? { by: approval.by ?? 'Someone', at: approval.at, method: approval.method, level: approval.level, where: approval.where ?? null }
    : null;

/** The kinds of work, in the order the filter shows them, with the words a person reads. */
export const KINDS: { kind: StoryKind; label: string }[] = [
  { kind: 'sent', label: 'Sent and published' },
  { kind: 'money', label: 'Paperwork and money' },
  { kind: 'forms', label: 'Forms and bookings' },
  { kind: 'research', label: 'Research' },
  { kind: 'shop', label: 'Orders' },
  { kind: 'files', label: 'Files and commands' },
];

const KIND_OF_ACTION: Partial<Record<HistoryAction, StoryKind>> = {
  post: 'sent',
  publish: 'sent',
  reply: 'sent',
  pay: 'money',
  change: 'files',
  task: 'files',
};

const money = (amount: number | undefined, currency: string | undefined) =>
  amount === undefined ? '' : new Intl.NumberFormat('en-GB', { style: 'currency', currency: currency || 'EUR' }).format(amount);

/**
 * An approval-style preview (`{ kind, data }`, the simulator's `preview_for`) as the artifact
 * History draws, so the cloud can send either shape.
 */
export function artifactOf(story: Pick<HistoryStory, 'artifact' | 'preview'>): StoryArtifact | null {
  if (story.artifact) return story.artifact;
  const kind: PreviewKind | null | undefined = story.preview?.kind;
  const data: PreviewData = story.preview?.data ?? {};
  const files = (data.items ?? []).map((item) => ({ name: item.name, size: item.size ?? 0 }));
  switch (kind) {
    case 'email':
      return { type: 'email', from: data.from ?? '', to: data.to ?? [], cc: data.cc, subject: data.subject ?? '', body: data.body ?? '', attachments: (data.attachments ?? []).map((item) => ({ name: item.name, size: item.size ?? 0 })) };
    case 'payment':
      return { type: 'payment', amount: money(data.amount, data.currency), payee: data.payee ?? '', for: data.for, from_account: data.from_account, reference: data.reference };
    case 'form': {
      const changed = new Set(data.changed ?? []);
      return { type: 'form', title: data.form ?? 'The form', fields: (data.fields ?? []).map((field) => ({ ...field, changed: changed.has(field.label) || Boolean(field.before) })) };
    }
    case 'order':
      return {
        type: 'order',
        number: data.order ?? '',
        customer: data.customer,
        items: (data.items ?? []).map((item) => ({ name: item.name, quantity: item.quantity ?? 1, price: money(item.price ?? undefined, data.currency) })),
        total: money(data.amount, data.currency),
        note: data.reason,
      };
    case 'message':
      return {
        type: 'message',
        rating: data.thread?.[0]?.stars ?? null,
        thread: [...(data.thread ?? []).map((message) => ({ from: message.from, text: message.text })), ...(data.reply ? [{ from: 'Our reply', text: data.reply, ours: true }] : [])],
      };
    case 'files':
      return { type: 'files', folder: data.destination, files };
    case 'delete':
      return { type: 'files', folder: data.destination ? `Deleted from ${data.destination}` : 'Deleted', files };
    case 'command':
      return { type: 'command', command: data.command ?? '', computer: data.computer };
    default:
      return null;
  }
}

/** The story's kind: what the cloud says, else what its artifact or action means. */
export function kindOf(story: Pick<HistoryStory, 'kind' | 'action' | 'artifact' | 'preview'>): StoryKind | null {
  if (story.kind) return story.kind;
  switch (artifactOf(story)?.type) {
    case 'email':
    case 'message':
      return 'sent';
    case 'files':
    case 'payment':
      return 'money';
    case 'form':
      return 'forms';
    case 'table':
      return 'research';
    case 'order':
      return 'shop';
    case 'command':
      return 'files';
    default:
      return KIND_OF_ACTION[story.action] ?? null;
  }
}

/** The checked verdict, said for what was done: a post is live, an email was sent, a form went in. */
function checkedWords(story: HistoryStory, named: string): { label: string; verdict: string; because: string } {
  const artifact = artifactOf(story);
  switch (artifact?.type) {
    case 'email':
      return { label: 'Sent, checked', verdict: 'It was sent, and we checked.', because: `Afterwards, openajar found it in ${named}'s Sent folder. This is the email as it went out.` };
    case 'message':
      return { label: 'Sent, checked', verdict: 'The reply is there, and we checked.', because: `Afterwards, openajar found the reply in the thread on ${named}.` };
    case 'files':
      return {
        label: 'Saved, checked',
        verdict: artifact.files.length === 1 ? 'The file is saved, and we checked it.' : `All ${artifact.files.length} files are saved, and we checked them.`,
        because: 'Each file opened and is the size it should be.',
      };
    case 'payment':
      return { label: 'Paid, checked', verdict: 'It was paid, and we checked.', because: `Afterwards, openajar found the payment in ${named}'s list of payments.` };
    case 'form':
      return { label: 'Sent in, checked', verdict: 'It was sent in, and we checked.', because: `${capital(named)} confirmed it. This is the form as it was sent, with what changed marked.` };
    case 'table':
      return { label: 'Checked', verdict: 'Done, and this is what it found.', because: artifact.summary ?? `openajar read ${named} and kept what it found.` };
    case 'order':
      return {
        label: 'Done, checked',
        verdict: artifact.refund ? 'The refund went through, and we checked.' : 'The order is in, and we checked.',
        because: `Afterwards, openajar found it on ${named}.`,
      };
    case 'command':
      return { label: 'Done, checked', verdict: 'It ran, and finished cleanly.', because: 'This is the command, and what it printed.' };
    default:
      break;
  }
  const goesPublic = PUBLIC.has(story.action);
  return {
    label: goesPublic ? 'Live, checked' : 'Checked',
    verdict: goesPublic ? 'It is live, and we checked.' : 'Done, and we checked.',
    because: story.proof?.screenshot ? `Afterwards, openajar opened ${named} as a visitor and found it. This is what it saw.` : `openajar looked at ${named} afterwards and saw it was done.`,
  };
}

const ACCOUNT_WORDS = /signed out|real person|robot|really you|switched off|someone else|refus/i;

/** Places that are not a brand read as "the bank", "the tax portal". */
const PLAIN_PLACES: Record<string, string> = { bank: 'the bank', gov: 'the government portal', portal: 'the portal' };

export function namedSite(key: string, label: string): string {
  return PLAIN_PLACES[key] ?? (OWN_WORDS[key] ? `the ${OWN_WORDS[key]}` : label);
}

/** The subject of the title: "Claude Code", "The Instagram post routine". */
export function subjectOf(view: Pick<StoryView, 'who'>): string {
  return view.who.principal.kind === 'flow' ? routineSubject(view.who.name) : view.who.name;
}

/** "Claude posted a photo on Harbor Supply's Instagram", "Claude Code is posting on ...". */
export function titleOf(view: Pick<StoryView, 'who' | 'sentence' | 'result'>): string {
  const subject = subjectOf(view);
  if (view.result === 'working') return `${subject} is ${lower(view.sentence)}`;
  return `${subject} ${lower(view.sentence)}`;
}

const PUBLIC = new Set(['post', 'publish', 'reply']);

export function fromHistory(story: HistoryStory): StoryView {
  const named = namedSite(story.site.key, story.site.label);
  const routine = story.who.kind === 'flow';
  const base = {
    id: story.id,
    runId: story.technical.run_id ?? (story.source === 'run' ? story.id : null),
    lane: story.lane,
    workspace: story.workspace ?? story.lane.split('/')[0] ?? '',
    business: story.workspace_name ?? '',
    siteKey: story.site.key,
    named,
    siteLabel: story.site.label,
    at: story.at,
    startedAt: story.started_at,
    sentence: story.sentence,
    status: story.status,
    lookOnly: story.action === 'read',
    proof: story.proof,
    steps: story.steps,
    who: {
      principal: { kind: story.who.kind, id: story.who.id, thread: null },
      name: routine ? `${story.who.name} routine` : story.who.name,
    },
    why: story.why.text,
    approvedBy: story.why.approval?.state === 'approved' ? { by: story.why.approval.by, at: story.why.approval.at } : null,
    yes: yesOf(story.why.approval),
    onItsOwn: Boolean(story.why.on_its_own),
    approvalId: null,
    technical: { cost: story.technical.cost, what: story.technical.flow ?? story.technical.tool ?? null, detail: story.detail },
    kind: kindOf(story),
    artifact: artifactOf(story),
    worker: story.worker ?? null,
  };
  const problem = story.reason ?? story.verdict_text;
  switch (story.verdict) {
    case 'checked':
      return { ...base, result: 'checked', ...checkedWords(story, named), note: null, accountProblem: false };
    case 'done':
      return { ...base, result: 'checked', label: 'Done', note: null, verdict: 'Done.', because: story.verdict_text, accountProblem: false };
    case 'not_checked':
      return {
        ...base,
        result: 'unchecked',
        label: 'Not checked',
        note: story.verdict_text,
        verdict: 'Done, but not checked.',
        because: `${story.verdict_text} It may still be fine: open the account to look.`,
        accountProblem: false,
      };
    case 'partial': {
      const why = reasonOf(story.detail ?? '', named)?.text ?? 'Only part of it went out.';
      return { ...base, result: 'failed', label: 'Partly done', note: why, verdict: 'Only part of it went out.', because: why, accountProblem: false };
    }
    case 'failed':
      return { ...base, result: 'failed', label: 'Did not work', note: story.verdict_text, verdict: 'It did not work.', because: story.verdict_text, accountProblem: ACCOUNT_WORDS.test(problem) };
    case 'not_run':
      return { ...base, result: 'failed', label: 'Stopped', note: story.verdict_text, verdict: 'It did not happen.', because: story.verdict_text, accountProblem: false };
    default:
      return { ...base, result: 'working', label: 'Working now', note: null, verdict: 'Working on it now.', because: 'An agent is on it right now. You can watch it live.', accountProblem: false };
  }
}

/** A run read on its own, told here from its summary. */
export function fromRun(run: Run, story: Story, whoName: string): StoryView {
  const principal = asPrincipal(run.principal);
  const flow = principal.kind === 'flow';
  const lookOnly = run.effect === 'read';
  return {
    id: run.id,
    runId: run.id,
    lane: run.lane,
    workspace: story.workspace,
    business: story.business,
    siteKey: story.siteKey,
    named: story.named,
    siteLabel: siteOf(run.lane)?.name ?? capital(story.site),
    at: run.finished_at ?? run.started_at,
    startedAt: run.started_at,
    sentence: story.sentence,
    result: story.result,
    status: run.status,
    label: story.label,
    note: story.note,
    verdict: run.status === 'cancelled' ? 'It did not happen.' : story.verdict,
    because: story.because,
    accountProblem: story.accountProblem,
    lookOnly,
    proof: run.proof ? { url: run.proof.url ?? null, screenshot: run.proof.screenshot ?? null } : null,
    steps: (run.steps ?? []).map((step, index, all) => ({ id: step.id, at: step.at, label: stepWords(step, story, index === all.length - 1), status: step.status })),
    who: { principal, name: whoName },
    why: flow ? null : principal.kind === 'human' ? 'Started by hand' : principal.thread ? 'Asked from a chat' : null,
    approvedBy: null,
    yes: null,
    onItsOwn: false,
    approvalId: run.approval_id ?? null,
    technical: { cost: run.cost, what: run.flow, detail: story.detail },
    kind: null,
    artifact: null,
    worker: null,
  };
}
