/**
 * Replay (the signature of an environment that is gone): the browser as it was, frame by frame,
 * kept on the node after the VM is deleted. The frame large, what led to it under it in words, the track
 * of the whole session below (pages, the agent's calls, a person's take-overs, the proofs), and beside it
 * everything that happened in order, the current moment marked. Play at 1, 2, 4 or 8 times, with quiet
 * stretches cut short; step with the arrows, jump with Shift. The owner's alone, as the node serves it.
 */
import { Bot, ChevronLeft, ChevronRight, Hand, History, Pause, Play } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useEnvReplay } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { EnvDetail } from '@/api/types';
import { IconButton } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { Kbd } from '@/components/ui/misc';
import { Skeleton } from '@/components/ui/states';
import { SegmentedControl } from '@/components/ui/tabs';
import { cn } from '@/lib/cn';
import { clockSeconds, date, plural } from '@/lib/format';
import { toolPlainTitle } from '@/lib/plain-node';
import { SPEEDS, buildReplay, causeWords, dwell, frameAt, offset } from './replay-model';
import type { ReplayModel, Speed } from './replay-model';
import { ReplayTrack } from './replay-track';
import { span } from './words';
import './environments.css';

export function ReplayCard({ org, env, embedded = false }: { org: string; env: EnvDetail; embedded?: boolean }) {
  const query = useEnvReplay(org, env.id);
  const model = useMemo(() => (query.data ? buildReplay(query.data, env.proofs, env.closed_at) : null), [query.data, env.proofs, env.closed_at]);
  const body = query.isPending ? (
    <div className="grid gap-3 p-5">
      <Skeleton className="aspect-[16/10] h-auto w-full rounded-lg" />
      <Skeleton className="h-24 w-full" />
    </div>
  ) : !model || !model.frames.length ? (
    <p className="flex items-center gap-2 px-5 py-6 text-body text-muted">
      <History aria-hidden="true" className="size-4 text-faint" />
      {query.error instanceof ApiError && query.error.status === 403
        ? 'Only the owner watches a replay.'
        : isNotServed(query.error)
          ? 'This computer keeps no replay of its environments yet.'
          : 'No frames were kept. The node pictures the browser after each page call and while a person has control.'}
    </p>
  ) : (
    <Player model={model} />
  );
  if (embedded) return body;
  const replay = query.data?.replay;
  return (
    <Card className="overflow-hidden">
      <CardHeader
        title={
          <>
            <History aria-hidden="true" className="size-4 text-faint" />
            Replay
          </>
        }
        meta={model && replay ? `${plural(replay.frames, 'frame')} over ${span(model.end - model.start)}${replay.kept_until ? ` · kept until ${date(replay.kept_until)}` : ''}` : undefined}
      />
      {body}
    </Card>
  );
}

function Player({ model }: { model: ReplayModel }) {
  const [index, setIndex] = useState(() => Math.max(0, model.frames.length - 1));
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<Speed>(2);
  const frame = model.frames[index];
  useEffect(() => {
    if (!playing) return undefined;
    if (index >= model.frames.length - 1) {
      setPlaying(false);
      return undefined;
    }
    const timer = window.setTimeout(() => setIndex((value) => value + 1), dwell(model, index, speed));
    return () => window.clearTimeout(timer);
  }, [playing, index, speed, model]);
  const toggle = () => {
    if (!playing && index >= model.frames.length - 1) setIndex(0);
    setPlaying((value) => !value);
  };
  if (!frame) return null;
  const person = frame.cause === 'person';
  return (
    <div className="grid gap-0 lg:grid-cols-[minmax(0,1fr)_320px]">
      <div className="grid min-w-0 gap-4 p-5 max-lg:pb-4">
        <figure className="grid gap-3">
          <div className={cn('relative overflow-hidden rounded-lg border bg-inset', person ? 'border-fg' : 'border-line')}>
            {/* every frame in the first one's shape, letterboxed when the window changed, so nothing is cut */}
            <img key={frame.id} src={frame.src} alt={`${frame.page_title ?? 'The page'}: ${causeWords(frame)}`} width={frame.width ?? 1280} height={frame.height ?? 800} style={{ aspectRatio: frameShape(model) }} className="replay-frame block h-auto max-h-[70dvh] w-full object-contain" />
            {model.frames.slice(index + 1, index + 3).map((next) => (next.src ? <link key={next.id} rel="prefetch" href={next.src} /> : null))}
            {person ? (
              <span className="absolute top-3 left-3 inline-flex items-center gap-2 rounded-full bg-inverse px-3 py-1 text-caption text-on-inverse">
                <Hand aria-hidden="true" className="size-3.5" />A person had control
              </span>
            ) : null}
          </div>
          <figcaption className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
            <CauseMark cause={frame.cause} />
            <span className="min-w-0 text-label text-fg [overflow-wrap:anywhere]">{causeWords(frame)}</span>
            <span className="min-w-0 truncate font-mono text-code-caption text-muted" title={frame.page_url ?? undefined}>
              {frame.page_url?.replace(/^https?:\/\//, '')}
            </span>
            <span className="ml-auto font-mono text-code-caption text-faint tabular" title={clockSeconds(frame.at)}>
              {offset(model, frame.t)} · {clockSeconds(frame.at)}
            </span>
          </figcaption>
        </figure>
        <Transport model={model} index={index} playing={playing} speed={speed} onToggle={toggle} onStep={(dir) => { setPlaying(false); setIndex((value) => Math.min(model.frames.length - 1, Math.max(0, value + dir))); }} onSpeed={setSpeed} />
        <ReplayTrack model={model} index={index} onSeek={(next) => { setPlaying(false); setIndex(next); }} onToggle={toggle} />
      </div>
      <EventLog model={model} index={index} onSeek={(next) => { setPlaying(false); setIndex(next); }} />
    </div>
  );
}

/** The shape of the replay's pictures: the first frame's, kept between a phone's and a wide screen's. */
function frameShape(model: ReplayModel): string {
  const first = model.frames.find((frame) => frame.width && frame.height);
  const ratio = first?.width && first.height ? first.width / first.height : 16 / 10;
  return String(Math.min(21 / 9, Math.max(3 / 4, ratio)));
}

function CauseMark({ cause }: { cause: string }) {
  const Icon = cause === 'person' ? Hand : cause === 'agent' ? Bot : History;
  return (
    <span aria-hidden="true" className={cn('flex size-6 shrink-0 translate-y-1 items-center justify-center self-start rounded-md border', cause === 'person' ? 'border-fg' : 'border-line')}>
      <Icon className="size-3.5 text-fg" />
    </span>
  );
}

function Transport({ model, index, playing, speed, onToggle, onStep, onSpeed }: { model: ReplayModel; index: number; playing: boolean; speed: Speed; onToggle: () => void; onStep: (dir: 1 | -1) => void; onSpeed: (speed: Speed) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      <div className="flex items-center gap-1">
        <IconButton label={playing ? 'Pause' : 'Play'} variant="secondary" onClick={onToggle} aria-keyshortcuts="Space">
          {playing ? <Pause className="size-4" /> : <Play className="size-4" />}
        </IconButton>
        <IconButton label="The frame before" onClick={() => onStep(-1)} disabled={index === 0}>
          <ChevronLeft className="size-4" />
        </IconButton>
        <IconButton label="The next frame" onClick={() => onStep(1)} disabled={index >= model.frames.length - 1}>
          <ChevronRight className="size-4" />
        </IconButton>
      </div>
      <span className="font-mono text-code-caption text-muted tabular">
        <span className="text-fg">{index + 1}</span> / {model.frames.length}
      </span>
      <SegmentedControl<string> label="Speed" value={String(speed)} onValueChange={(value) => onSpeed(Number(value) as Speed)} options={SPEEDS.map((value) => ({ value: String(value), label: `${value}×` }))} />
      <p className="ml-auto hidden items-center gap-1 text-caption text-faint md:flex">
        <Kbd>←</Kbd>
        <Kbd>→</Kbd> a frame · <Kbd>Shift</Kbd> the next thing done · <Kbd>Space</Kbd> play. Quiet stretches are cut short.
      </p>
    </div>
  );
}

/** A person's frames in a row: the first took control, the rest say how far in they are. */
function personWords(model: ReplayModel, index: number): string | null {
  const frame = model.frames[index];
  if (frame?.cause !== 'person') return null;
  const span = model.takeovers.find((item) => frame.t >= item.from && frame.t <= item.to);
  const before = model.frames[index - 1];
  if (!span || before?.cause !== 'person') return 'A person took control';
  return `With the person, ${Math.round((frame.t - span.from) / 1000)} s in`;
}

/** One row of what happened: a frame, a call between frames, or a run of the page changing on its own. */
export interface LogItem {
  key: string;
  t: number;
  /** The frame it seeks to (the first of a run). */
  frame: number;
  /** The frames a run covers, first and last. */
  last: number;
  kind: string;
  title: string;
  ok: boolean;
  count: number;
  /** A call the page changed after (a reload when a file changed): the row seeks to that frame. */
  changed?: boolean;
}

/** A frame of the page changing this soon after a call is that call's doing, shown on the call's row. */
const AFTER_CALL_MS = 15_000;

/**
 * Everything that happened, in order. A frame of the page changing just after a call goes on that call's
 * row (an edit the dev server reloaded), and a run of frames the page took on its own (an animation, a
 * spinner, a clock) is one row with its count, so the agent's and the person's steps stay readable.
 */
export function logItems(model: ReplayModel): LogItem[] {
  const items: LogItem[] = [
    ...model.frames.map((frame, at) => ({ key: frame.id, t: frame.t, frame: at, last: at, kind: frame.cause, title: personWords(model, at) ?? causeWords(frame), ok: true, count: 1 })),
    ...model.calls
      // a call its own frame pictures (the frame waits out the page's next read) is that frame's row
      .filter((call) => !model.frames.some((frame) => frame.t === call.t || (frame.cause === 'agent' && frame.tool === call.tool && frame.t >= call.t && frame.t - call.t <= AFTER_CALL_MS)))
      .map((call, at) => ({ key: `c${at}`, t: call.t, frame: frameAt(model, call.t), last: frameAt(model, call.t), kind: 'call', title: call.summary ? `${toolPlainTitle(call.tool)}: ${call.summary}` : toolPlainTitle(call.tool), ok: call.ok, count: 1 })),
  ].sort((a, b) => a.t - b.t);
  const out: LogItem[] = [];
  for (const item of items) {
    const before = out[out.length - 1];
    if (item.kind === 'change' && before?.kind === 'call' && !before.changed && item.t - before.t <= AFTER_CALL_MS) {
      Object.assign(before, { changed: true, frame: item.frame, last: item.frame });
      continue;
    }
    if (item.kind === 'change' && before?.kind === 'change') {
      before.count += 1;
      before.last = item.frame;
      before.title = `The page changed on its own, ${before.count} times`;
      continue;
    }
    out.push({ ...item });
  }
  return out;
}

/** Everything that happened, in order, the current moment marked and kept in view. */
function EventLog({ model, index, onSeek }: { model: ReplayModel; index: number; onSeek: (index: number) => void }) {
  const items = useMemo(() => logItems(model), [model]);
  const current = model.frames[index]?.t ?? 0;
  const currentKey = [...items].reverse().find((item) => index >= item.frame && index <= item.last && (item.kind !== 'call' || item.changed))?.key;
  const list = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const element = list.current?.querySelector<HTMLElement>('[aria-current="true"]');
    if (element && list.current) list.current.scrollTop = element.offsetTop - list.current.clientHeight / 2;
  }, [currentKey]);
  return (
    <section aria-label="What happened" className="grid min-h-0 grid-rows-[auto_minmax(0,1fr)] border-line max-lg:border-t lg:border-l">
      <h3 className="px-4 pt-4 pb-2 text-caption text-faint">What happened · {plural(items.length, 'step')}</h3>
      <ol ref={list} className="relative max-h-[560px] overflow-y-auto px-2 pb-3 max-lg:max-h-72">
        {items.map((item) => {
          const active = item.key === currentKey;
          const past = item.t <= current;
          return (
            <li key={item.key}>
              <button
                type="button"
                aria-current={active || undefined}
                onClick={() => onSeek(item.frame)}
                className={cn(
                  'grid w-full grid-cols-[52px_minmax(0,1fr)] items-baseline gap-2 rounded-md px-2 py-1 text-left transition-colors duration-(--duration-fast) hover:bg-hover',
                  active && 'bg-pressed',
                )}
              >
                <span className="font-mono text-code-caption text-faint tabular">{offset(model, item.t)}</span>
                <span className={cn('min-w-0 truncate text-secondary', !item.ok ? 'text-bad' : item.kind === 'person' ? 'font-medium text-fg' : item.kind === 'call' ? 'text-muted' : past ? 'text-fg' : 'text-muted')} title={item.title}>
                  {item.kind === 'person' ? <Hand aria-hidden="true" className="mr-1 inline size-3.5 align-[-2px]" /> : null}
                  {item.title}
                  {item.changed ? <span className="text-faint"> · the page changed</span> : null}
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
