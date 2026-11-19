/**
 * "Follow up once if no reply in 3 days", in the language of a routine's page: the sentence, the steps
 * as a map, what it made. What only email has is the chase below: one line for each person, starting
 * the day they got the first email and ending the day it was over. An answer cuts a line short, which
 * is the whole promise of this routine, drawn. A node keeps less than the concept: what comes back
 * null is said plainly or left out, never drawn as zero.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ArrowLeft, Lock } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useMailRoutine, useRoutineCatalog, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { ChasePerson, MailRoutine, RoutineCatalog, RoutineStepRun } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { Card } from '@/components/ui/card';
import { Meter } from '@/components/ui/misc';
import { ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { FlowMap } from '@/features/flows/flow-map';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { dataBefore, refName, sentenceOf } from '@/lib/routines';
import { FromComputer } from './shared';
import { firstName } from './words';

const DAY = 86_400_000;
const SPAN = 6;

type Ending = 'replied' | 'unsubscribed' | 'followed' | 'waiting';

interface Life extends ChasePerson {
  ending: Ending;
  /** Days from the first email to where the line stops. */
  stops: number;
  follow: number | null;
}

function lifeOf(person: ChasePerson, now: number): Life {
  const first = new Date(person.first_at).getTime();
  const days = (iso: string | null) => (iso ? (new Date(iso).getTime() - first) / DAY : null);
  const reply = days(person.reply_at);
  const out = days(person.unsubscribe_at);
  const follow = days(person.follow_at);
  const today = (now - first) / DAY;
  if (reply !== null) return { ...person, ending: 'replied', stops: reply, follow };
  if (out !== null) return { ...person, ending: 'unsubscribed', stops: out, follow };
  if (follow !== null) return { ...person, ending: 'followed', stops: follow, follow };
  return { ...person, ending: 'waiting', stops: today, follow: null };
}

const ENDING_ORDER: Ending[] = ['replied', 'unsubscribed', 'followed', 'waiting'];

function storyOf(life: Life): string {
  const name = firstName(life.name);
  const when = (days: number) => plural(Math.max(1, Math.round(days)), 'day');
  switch (life.ending) {
    case 'replied':
      return life.follow !== null ? `${name} answered the follow-up after ${when(life.stops)}. Nothing more goes out.` : `${name} replied after ${when(life.stops)}, so no follow-up went.`;
    case 'unsubscribed':
      return `${name} unsubscribed after ${when(life.stops)}, and will never be written to again.`;
    case 'followed':
      return `${name} got the one follow-up. No answer, so that is the end of it.`;
    case 'waiting':
      return `${name} is in the three days. If nothing comes back, one follow-up goes.`;
  }
}

const LEGEND: { ending: Ending; word: string; target: string | null }[] = [
  { ending: 'replied', word: 'Replied, so the chase stopped', target: 'replied' },
  { ending: 'unsubscribed', word: 'Unsubscribed', target: null },
  { ending: 'followed', word: 'Followed up once, then left alone', target: 'follow' },
  { ending: 'waiting', word: 'Still in the three days', target: 'wait' },
];

/** The chart. Every line starts at the first email, so the third day is one straight gate. */
function Chase({ people, lit, onLight }: { people: ChasePerson[]; lit: string | null; onLight: (target: string | null) => void }) {
  const lives = useMemo(() => {
    const now = Date.now();
    return people.map((person) => lifeOf(person, now)).sort((a, b) => ENDING_ORDER.indexOf(a.ending) - ENDING_ORDER.indexOf(b.ending) || a.stops - b.stops);
  }, [people]);
  const [pointed, setPointed] = useState<Life | null>(null);
  const [group, setGroup] = useState<Ending | null>(null);
  const [listed, setListed] = useState(false);
  const at = (days: number) => `${Math.min(100, (days / SPAN) * 100)}%`;
  const fromStep: Ending | null = lit === 'replied' || lit === 'stop' ? 'replied' : lit === 'follow' ? 'followed' : lit === 'wait' ? 'waiting' : null;
  const shown = group ?? fromStep;
  const counts = Object.fromEntries(ENDING_ORDER.map((ending) => [ending, lives.filter((life) => life.ending === ending).length])) as Record<Ending, number>;
  return (
    <section aria-labelledby="chase" className="grid gap-8 lg:grid-cols-[minmax(0,1fr)_17rem]">
      <div className="min-w-0">
        <h2 id="chase" className="text-heading">The chase, one line for each person</h2>
        <p aria-live="polite" className="mt-1 min-h-5 truncate text-body text-muted">{pointed ? storyOf(pointed) : 'A line that stops early is someone who answered. Point at one to read it.'}</p>
        <div className="relative mt-4 pt-6">
          <div aria-hidden="true" className="absolute inset-y-0 w-px bg-(--board-wire-strong)" style={{ left: at(3) }}>
            <span className="absolute top-0 left-2 text-caption whitespace-nowrap text-muted">Day 3: one follow-up</span>
          </div>
          <div role="img" aria-label={`${counts.replied} replied, ${counts.unsubscribed} unsubscribed, ${counts.followed} followed up once, ${counts.waiting} still in the three days`} className={cn(shown && 'chase-dim')} onPointerLeave={() => setPointed(null)}>
            {lives.map((life, index) => (
              <div key={life.id} className={cn('chase-row', index > 0 && lives[index - 1]?.ending !== life.ending && 'mt-3')} data-lit={shown === life.ending || pointed?.id === life.id ? '' : undefined} onPointerEnter={() => setPointed(life)}>
                <span className="chase-line" style={{ left: 0, width: at(life.stops) }} />
                {life.follow !== null ? <span className="chase-end" data-kind="followed" style={{ left: at(life.follow) }} /> : null}
                {life.ending === 'replied' || life.ending === 'unsubscribed' ? <span className="chase-end" data-kind={life.ending} style={{ left: at(life.stops) }} /> : null}
              </div>
            ))}
          </div>
          <div aria-hidden="true" className="mt-2 flex justify-between border-t border-line pt-1 text-caption text-faint">
            <span>First email</span>
            <span>{SPAN} days later</span>
          </div>
        </div>
        <button type="button" aria-expanded={listed} onClick={() => setListed(!listed)} className="mt-3 text-secondary text-muted hover:text-fg">{listed ? 'Hide the list' : 'Read it as a list'}</button>
        {listed ? (
          <ul className="mt-2 divide-y divide-line border-y border-line">
            {lives.map((life) => (
              <li key={life.id} className="grid gap-x-4 py-2 text-secondary text-pretty sm:grid-cols-[14rem_minmax(0,1fr)]">
                <span className="min-w-0 truncate font-medium">{life.name} <span className="font-normal text-faint">{life.company}</span></span>
                <span className="min-w-0 text-muted">{storyOf(life)}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      <ul className="grid content-start gap-1 text-body">
        {LEGEND.map((item) => (
          <li key={item.ending}>
            <button
              type="button"
              onPointerEnter={() => {
                setGroup(item.ending);
                onLight(item.target);
              }}
              onPointerLeave={() => {
                setGroup(null);
                onLight(null);
              }}
              onFocus={() => setGroup(item.ending)}
              onBlur={() => setGroup(null)}
              className="-mx-2 flex w-[calc(100%+16px)] items-center gap-3 rounded-md px-2 py-2 text-left transition-colors duration-(--duration-fast) ease-standard hover:bg-hover"
            >
              <span className="relative size-2 shrink-0">
                {item.ending === 'waiting' ? <span className="absolute top-[3px] h-0.5 w-2 rounded-sm bg-(--board-wire-strong)" /> : <span className="chase-end" data-kind={item.ending} style={{ left: 4 }} />}
              </span>
              <span className="tabular w-6 shrink-0 text-section-title">{counts[item.ending]}</span>
              <span className="min-w-0 text-secondary text-pretty text-muted">{item.word}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Rules({ org, routine }: { org: string; routine: MailRoutine }) {
  const { limit, follow_up: followUp, list } = routine;
  const rules = [
    'A reply stops it at once, for that person',
    'Once means once: no second follow-up, ever',
    limit ? `Stops at ${limit.daily_limit} a day` : "Stops at the mailbox's daily limit",
    limit ? `Skips ${plural(limit.suppressed, 'person', 'people')} who asked not to hear from you` : 'Skips everyone who asked not to hear from you',
    'Every email carries an unsubscribe link',
  ];
  return (
    <section aria-label="What it will never do" className="grid gap-6 lg:grid-cols-2 lg:gap-16">
      <div className="min-w-0">
        <h2 className="text-heading">What goes out</h2>
        {followUp ? (
          <div className="mt-3 rounded-lg border border-line p-4">
            <p className="text-secondary font-medium">{followUp.subject.replace('{company}', 'your cafe')}</p>
            <p className="mt-2 text-body text-pretty text-muted">{followUp.body.replace('{first name}', 'there')}</p>
          </div>
        ) : null}
        {limit ? (
          <div className="mt-4 flex items-center gap-3">
            <Meter value={limit.sent_today} max={limit.daily_limit} label="Sent today" className="w-40 shrink-0" />
            <span className="tabular text-secondary text-muted">{limit.sent_today} of {limit.daily_limit} today</span>
          </div>
        ) : (
          <p className="mt-1 text-body text-pretty text-muted">
            It sends from {routine.address ? <span className="mono text-secondary">{routine.address}</span> : 'its mailbox'}. Its send limit is set on the mailbox.
          </p>
        )}
      </div>
      <div>
        <h2 className="text-heading">Who it can reach</h2>
        {list ? (
          <p className="mt-1 text-body text-pretty text-muted">
            Only <Link to="/$org/email/lists/$list" params={{ org, list: list.id }} className="text-fg underline underline-offset-4">{list.name}</Link>{limit ? ` (${plural(limit.list_size, 'person', 'people')})` : null}. These always apply. They are not settings.
          </p>
        ) : (
          <p className="mt-1 text-body text-pretty text-muted">This routine has no list it sends to yet. These always apply. They are not settings.</p>
        )}
        <ul className="mt-3 divide-y divide-line text-body">
          {rules.map((rule) => (
            <li key={rule} className="flex items-center gap-3 py-2">
              <Lock aria-hidden="true" className="size-4 shrink-0 text-faint" />
              {rule}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function Sentence({ routine, catalog, lit, onLight }: { routine: MailRoutine; catalog: RoutineCatalog | undefined; lit: string | null; onLight: (target: string | null) => void }) {
  const definition = { trigger: routine.trigger, steps: routine.steps };
  const parts = sentenceOf(definition, (ref) => refName(dataBefore(definition, null, catalog), ref)?.toLowerCase() ?? null, catalog);
  return (
    <h1 className="max-w-4xl text-page-title-compact text-balance sm:text-[36px] sm:leading-[42px] sm:tracking-[-0.04em]">
      {parts.map((part, index) =>
        part.target ? (
          <span key={index} onPointerEnter={() => onLight(part.target ?? null)} onPointerLeave={() => onLight(null)} className={cn('rounded-sm transition-colors duration-(--duration-fast) ease-standard', lit === part.target ? 'bg-pressed' : '')}>
            {part.text}
          </span>
        ) : (
          <span key={index} className="text-faint">{part.text}</span>
        ),
      )}
    </h1>
  );
}

function Body({ org, routine }: { org: string; routine: MailRoutine }) {
  const catalog = useRoutineCatalog(org).data;
  const workspace = useWorkspaces(org).data?.find((item) => item.id === routine.workspace);
  const [lit, setLit] = useState<string | null>(null);
  const lives = useMemo(() => (routine.people ?? []).map((person) => lifeOf(person, Date.now())), [routine.people]);
  const replied = lives.filter((life) => life.ending === 'replied').length;
  const followed = lives.filter((life) => life.follow !== null).length;
  const waiting = lives.filter((life) => life.ending === 'waiting').length;
  const counted = useMemo<RoutineStepRun[]>(() => {
    const step = (id: string, label: string, kind: RoutineStepRun['kind'], short: string): RoutineStepRun => ({ step: id, label, kind, short, status: 'succeeded_verified', started_at: null, finished_at: null, input: null, output: null, attempts: 1 });
    return [step('wait', 'wait 3 days', 'wait', `${waiting} waiting now`), step('replied', 'if they replied', 'branch', `${replied} replied`), step('stop', 'stop, and tell you', 'notify', `${replied} chases stopped`), step('follow', 'send one follow-up', 'action', `${followed} sent`)];
  }, [replied, followed, waiting]);
  // The counts come from who it chases. Without a run, or without the people, the steps stand alone.
  const run = routine.run === null ? null : routine.people ? counted : routine.run;

  return (
    <div className="grid gap-10 pb-16 md:gap-12">
      <div>
        <Link to="/$org/email" params={{ org }} className="mb-6 inline-flex items-center gap-1 text-secondary text-muted hover:text-fg">
          <ArrowLeft className="size-3" /> Email
        </Link>
        <header>
          <p className="mb-3 flex min-w-0 items-center gap-2 text-body text-muted">
            {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={20} /> : null}
            <span className="truncate">{workspace?.name} · {routine.name}</span>
          </p>
          <Sentence routine={routine} catalog={catalog} lit={lit} onLight={setLit} />
          {routine.people ? (
            <p className="mt-4 text-body text-pretty text-muted">
              <span className="text-fg">{plural(replied, 'person', 'people')} replied, so the chase stopped for them.</span> {followed} got their one follow-up, and {waiting} are still in the three days.
            </p>
          ) : null}
          <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-y border-line py-3">
            <p className="text-secondary text-muted"><span className="text-label text-fg">Runs on its own.</span> It only ever writes to people on one list, and a reply always wins.</p>
            <FromComputer computers={['home-mini']} className="ml-auto" />
          </div>
        </header>
      </div>
      <FlowMap trigger={routine.trigger} steps={routine.steps} catalog={catalog} run={run} next={null} locked={new Set()} lit={lit === 'trigger' ? 'trigger' : lit} onLight={setLit} />
      {routine.people ? (
        <Chase people={routine.people} lit={lit} onLight={setLit} />
      ) : (
        <section aria-labelledby="chase">
          <h2 id="chase" className="text-heading">The chase</h2>
          <p className="mt-1 text-body text-pretty text-muted">Who this routine has written to, and how each one ended, is not kept on this computer yet.</p>
        </section>
      )}
      <Rules org={org} routine={routine} />
    </div>
  );
}

export function MailRoutinePage() {
  const { org, routine: id } = useParams({ from: '/$org/email/routines/$routine' });
  const routine = useMailRoutine(org, id);
  if (isNotServed(routine.error)) return <Card><NotLive /></Card>;
  if (routine.error) return <ErrorState error={routine.error} onRetry={() => void routine.refetch()} />;
  if (!routine.data) return <div className="grid gap-6"><Skeleton className="h-24 w-3/4" /><Skeleton className="h-40 rounded-(--radius-card)" /><Skeleton className="h-96 rounded-(--radius-card)" /></div>;
  return <Body org={org} routine={routine.data} />;
}
