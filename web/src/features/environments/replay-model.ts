/**
 * The replay as a picture of time (concept): the frames in order, the agent's calls between them, when a
 * person had the browser (from the run of `person` frames, which the node takes every 3 s while someone is
 * in control), the pages it was on, and the proofs. Playback, stepping and the words of each frame. Pure.
 */
import type { EnvProof, EnvReplayTimeline, ReplayCall, ReplayCause, ReplayFrame } from '@/api/types';

/** How often the node pictures a person's take-over (openajar.environments.replay EVERY_S). */
export const PERSON_EVERY_MS = 3_000;
export const SPEEDS = [1, 2, 4, 8] as const;
export type Speed = (typeof SPEEDS)[number];

export interface Span {
  from: number;
  to: number;
}

export interface ReplayModel {
  start: number;
  end: number;
  frames: (ReplayFrame & { t: number })[];
  calls: (ReplayCall & { t: number })[];
  takeovers: Span[];
  pages: (Span & { label: string })[];
  proofs: { t: number; title: string; id: string }[];
}

const ms = (iso: string | null | undefined) => (iso ? Date.parse(iso) : Number.NaN);

/** "Checkout" from "Checkout · Harbor Supply", else the address's path. */
export function pageLabel(frame: Pick<ReplayFrame, 'page_title' | 'page_url'>): string {
  const title = frame.page_title?.split(' · ')[0]?.trim();
  if (title) return title;
  try {
    const url = new URL(frame.page_url ?? '');
    return url.pathname === '/' ? url.host : url.pathname;
  } catch {
    return frame.page_url ?? 'A page';
  }
}

export function buildReplay(timeline: EnvReplayTimeline, proofs: EnvProof[] = [], closedAt?: string | null): ReplayModel {
  const frames = timeline.timeline
    .filter((item): item is ReplayFrame => item.kind === 'frame')
    .map((frame) => ({ ...frame, t: ms(frame.at) }))
    .sort((a, b) => a.t - b.t);
  const calls = timeline.timeline
    .filter((item): item is ReplayCall => item.kind === 'call')
    .map((item) => ({ ...item, t: ms(item.at) }))
    .sort((a, b) => a.t - b.t);
  const start = Math.min(ms(timeline.environment.created_at), frames[0]?.t ?? Number.POSITIVE_INFINITY);
  const last = Math.max(frames[frames.length - 1]?.t ?? start, calls[calls.length - 1]?.t ?? start);
  const end = Math.max(last, Number.isFinite(ms(closedAt)) ? ms(closedAt) : last);
  return { start, end: Math.max(end, start + 1), frames, calls, takeovers: takeovers(frames), pages: pages(frames, end), proofs: proofs.map((proof) => ({ t: ms(proof.taken_at), title: proof.title, id: proof.id })).filter((proof) => Number.isFinite(proof.t)) };
}

/** Runs of person frames: a person had the browser from the first until a beat after the last. */
function takeovers(frames: { cause: ReplayCause; t: number }[]): Span[] {
  const out: Span[] = [];
  for (const frame of frames) {
    if (frame.cause !== 'person') continue;
    const open = out[out.length - 1];
    if (open && frame.t - open.to <= PERSON_EVERY_MS * 4) open.to = frame.t + PERSON_EVERY_MS;
    else out.push({ from: frame.t - PERSON_EVERY_MS, to: frame.t + PERSON_EVERY_MS });
  }
  return out;
}

function pages(frames: (ReplayFrame & { t: number })[], end: number): (Span & { label: string })[] {
  const out: (Span & { label: string })[] = [];
  for (const frame of frames) {
    const label = pageLabel(frame);
    const open = out[out.length - 1];
    if (open && open.label === label) continue;
    if (open) open.to = frame.t;
    out.push({ from: frame.t, to: end, label });
  }
  return out;
}

/** The frame shown at a moment: the last one taken at or before it. */
export function frameAt(model: ReplayModel, t: number): number {
  let found = 0;
  for (let index = 0; index < model.frames.length; index += 1) {
    if ((model.frames[index]?.t ?? 0) <= t) found = index;
    else break;
  }
  return found;
}

/** The next frame (or the one before, `dir` -1) that marks something done: an agent's call or a take-over starting. */
export function jump(model: ReplayModel, index: number, dir: 1 | -1): number {
  for (let next = index + dir; next >= 0 && next < model.frames.length; next += dir) {
    const frame = model.frames[next];
    const before = model.frames[next - 1];
    if (frame && (frame.cause === 'agent' || (frame.cause === 'person' && before?.cause !== 'person') || frame.cause === 'close')) return next;
  }
  return dir > 0 ? model.frames.length - 1 : 0;
}

/** How long a frame stays on screen while playing: its real time, cut to a few seconds, faster with speed. */
export function dwell(model: ReplayModel, index: number, speed: Speed): number {
  const frame = model.frames[index];
  const next = model.frames[index + 1];
  if (!frame || !next) return 0;
  return Math.min(2_400, Math.max(320, next.t - frame.t)) / speed;
}

/** "15:21", "1:02:09": time since the open, as a player shows it. */
export function offset(model: ReplayModel, t: number): string {
  const total = Math.max(0, Math.round((t - model.start) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${secs}` : `${minutes}:${secs}`;
}

/** What led to a frame, as a person reads it. */
export function causeWords(frame: Pick<ReplayFrame, 'cause' | 'tool' | 'summary'>): string {
  switch (frame.cause) {
    case 'agent':
      return frame.summary ? actionWords(frame.summary) : 'The agent acted';
    case 'person':
      return 'A person had control';
    case 'change':
      return 'The page changed on its own';
    case 'save':
      return 'Saved';
    case 'close':
      return 'Its last state, as it closed';
    default:
      return 'A frame';
  }
}

/** 'click "Add to cart"' as 'Clicked "Add to cart"', an address as 'Opened localhost:3000/cart'. */
export function actionWords(summary: string): string {
  const url = summary.match(/^https?:\/\/(\S+)/);
  if (url) return `Opened ${url[1]?.replace(/\/$/, '')}`;
  const verb = summary.match(/^(click|type|fill|hover|scroll|press|select|upload)\b\s*(.*)$/i);
  if (verb) {
    const past: Record<string, string> = { click: 'Clicked', type: 'Typed', fill: 'Filled in', hover: 'Pointed at', scroll: 'Scrolled', press: 'Pressed', select: 'Chose', upload: 'Uploaded' };
    // a bare snapshot ref ("f4e8") names nothing a person knows
    const what = /^[a-z]?\d*e\d+$/i.test((verb[2] ?? '').trim()) ? 'on the page' : (verb[2] ?? '');
    return `${past[verb[1]?.toLowerCase() ?? ''] ?? verb[1]} ${what}`.trim();
  }
  return summary.charAt(0).toUpperCase() + summary.slice(1);
}

/** Where a moment falls on the track, 0 to 1. */
export const along = (model: ReplayModel, t: number) => Math.min(1, Math.max(0, (t - model.start) / (model.end - model.start)));

/** The axis ticks: every 5, 10 or 15 minutes, as fits the length. */
export function ticks(model: ReplayModel): number[] {
  const minutes = (model.end - model.start) / 60_000;
  const every = minutes > 90 ? 30 : minutes > 40 ? 10 : minutes > 15 ? 5 : minutes > 4 ? 1 : 0.5;
  const out: number[] = [];
  for (let m = 0; m <= minutes; m += every) out.push(model.start + m * 60_000);
  return out;
}
