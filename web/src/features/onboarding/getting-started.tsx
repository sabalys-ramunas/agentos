/**
 * Getting started, on Home (startup.md `#s7`): what is still not true on the server, each line
 * one click back into its setup step, with a time estimate and "Hide this list". It stays until
 * every line is really done, so skipping a step is never forever.
 */
import { Link } from '@tanstack/react-router';
import { Check, ChevronDown } from 'lucide-react';
import { useState } from 'react';
import { useAccess } from '@/api/hooks';
import type { GettingStartedItem, SetupItemKey } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { cn } from '@/lib/cn';
import type { StepKey } from './layout';
import { minutesLeft, remember, useSetup } from './setup';

const ACTION: Record<SetupItemKey, { label: string; step: StepKey }> = {
  goal: { label: 'Choose', step: 'goal' },
  computer: { label: 'Add', step: 'computer' },
  account: { label: 'Sign in', step: 'accounts' },
  ai: { label: 'Connect', step: 'ai' },
  approval: { label: 'Try it', step: 'try' },
  phone: { label: 'Show code', step: 'people' },
  invite: { label: 'Invite', step: 'people' },
};

function Ring({ done, total }: { done: number; total: number }) {
  const r = 14;
  const length = 2 * Math.PI * r;
  return (
    <svg viewBox="0 0 36 36" aria-hidden="true" className="size-9 shrink-0 -rotate-90">
      <circle cx="18" cy="18" r={r} fill="none" stroke="var(--fill-2)" strokeWidth="4" />
      <circle cx="18" cy="18" r={r} fill="none" stroke="var(--foreground)" strokeWidth="4" strokeLinecap="round" strokeDasharray={`${(done / total) * length} ${length}`} />
    </svg>
  );
}

function Row({ org, item }: { org: string; item: GettingStartedItem }) {
  const action = ACTION[item.key];
  return (
    <li className={cn('min-h-14 items-center gap-3 px-4 py-2', item.done ? 'hidden md:flex' : 'flex')}>
      <span
        aria-hidden="true"
        className={cn('inline-flex size-5 shrink-0 items-center justify-center rounded-full border', item.done ? 'border-fg bg-fg text-bg' : 'border-line-strong')}
      >
        {item.done ? <Check className="size-3" strokeWidth={3} /> : null}
      </span>
      <span className="min-w-0 flex-1">
        <span className={cn('block text-14', item.done ? 'text-muted' : 'font-medium')}>
          {item.title}
          {item.done ? <span className="sr-only">, done</span> : null}
        </span>
        {item.done ? null : <span className="block truncate text-13 text-faint">{item.hint}</span>}
      </span>
      {item.done ? null : (
        <>
          <span className="tabular hidden text-13 text-faint sm:inline">{item.minutes} min</span>
          <Button size="sm" asChild>
            <Link to="/onboarding/$org/$step" params={{ org, step: action.step }} aria-label={`${action.label}: ${item.title}`}>
              {action.label}
            </Link>
          </Button>
        </>
      )}
    </li>
  );
}

/** At the top of Home for people who set things up, until setup is really done. */
export function GettingStarted({ org }: { org: string }) {
  const access = useAccess(org);
  const owner = access.can('workspaces.create');
  const { setup, local } = useSetup(org, owner);
  if (!owner || !setup || setup.hidden) return null;
  // Worked out here, the list is only for an org whose setup started in this browser, so an
  // organization that has run for months is never told to choose a first job.
  if (local && !setup.goal && !setup.step) return null;
  const done = setup.items.filter((item) => item.done).length;
  const total = setup.items.length;
  if (done === total) return null;
  const left = minutesLeft(setup);
  // Open lines first, in the order setup does them; done ones after.
  const items = [...setup.items.filter((item) => !item.done), ...setup.items.filter((item) => item.done)];
  // Most of it done: one quiet line with the next step, so Home's answer stays on top.
  if (done / total >= 0.7) return <AlmostDone org={org} open={items.filter((item) => !item.done)} items={items} done={done} total={total} />;

  return (
    <Card role="region" className="mb-10 overflow-hidden" aria-labelledby="getting-started-title">
      <div className="flex items-center gap-3 border-b border-line px-4 py-4">
        <Ring done={done} total={total} />
        <div className="min-w-0 flex-1">
          <h2 id="getting-started-title" className="text-16 font-semibold">
            Getting started
          </h2>
          <p className="tabular text-13 text-muted">
            {done} of {total} done. The rest takes about {left} {left === 1 ? 'minute' : 'minutes'}.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={() => remember(org, { hidden: true })}>
          Hide this list
        </Button>
      </div>
      <ul className="grid divide-y divide-line md:grid-cols-2 md:divide-y-0 md:[&>li]:border-b md:[&>li]:border-line md:[&>li:nth-child(odd)]:border-r">
        {items.map((item) => (
          <Row key={item.key} org={org} item={item} />
        ))}
      </ul>
      {/* On a phone the done lines fold into one, so what is left stays in reach. */}
      <p className={cn('items-start gap-2 border-t border-line px-4 py-3 text-13 text-muted md:hidden', done ? 'flex' : 'hidden')}>
        <Check className="mt-1 size-3.5 shrink-0" aria-hidden="true" />
        Done: {setup.items.filter((item) => item.done).map((item) => item.title.toLowerCase()).join(', ')}.
      </p>
    </Card>
  );
}

/** One line when only a step or two is left: "Almost set up: add your phone · Show code", with the full list a click away. */
function AlmostDone({ org, open, items, done, total }: { org: string; open: GettingStartedItem[]; items: GettingStartedItem[]; done: number; total: number }) {
  const [expanded, setExpanded] = useState(false);
  const next = open[0];
  if (!next) return null;
  const action = ACTION[next.key];
  const rest = open.length - 1;
  return (
    <Card role="region" aria-labelledby="getting-started-title" className="mb-10 overflow-hidden">
      <div className="flex min-h-14 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2">
        <Ring done={done} total={total} />
        <p className="min-w-0 flex-1 basis-[calc(100%-48px)] text-14 sm:basis-auto">
          <span id="getting-started-title" className="font-medium">
            Almost set up:
          </span>{' '}
          <span className="text-muted">
            {next.title.charAt(0).toLowerCase()}
            {next.title.slice(1)}
            {rest ? `, and ${rest} more` : ''}
          </span>
        </p>
        <div className="ml-12 flex items-center gap-1 sm:ml-0">
          <Button size="sm" asChild>
            <Link to="/onboarding/$org/$step" params={{ org, step: action.step }} aria-label={`${action.label}: ${next.title}`}>
              {action.label}
            </Link>
          </Button>
          <Button variant="ghost" size="sm" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
            {expanded ? 'Less' : 'All steps'}
            <ChevronDown className={cn('size-3.5 transition-transform duration-(--duration-base)', expanded && 'rotate-180')} />
          </Button>
          <Button variant="ghost" size="sm" onClick={() => remember(org, { hidden: true })}>
            Hide
          </Button>
        </div>
      </div>
      {expanded ? (
        <ul className="grid divide-y divide-line border-t border-line md:grid-cols-2 md:divide-y-0 md:[&>li]:border-b md:[&>li]:border-line md:[&>li:nth-child(odd)]:border-r">
          {items.map((item) => (
            <Row key={item.key} org={org} item={item} />
          ))}
        </ul>
      ) : null}
    </Card>
  );
}

