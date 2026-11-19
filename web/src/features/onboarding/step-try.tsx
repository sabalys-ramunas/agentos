/**
 * Step 5: give it its first job (startup.md, `#s5`). The core moment inside setup: a starter
 * message to copy, what is happening in real time, and the real approval card the moment it
 * arrives (live, over the event stream). Nothing here is staged: the timeline says only what the
 * cloud knows, and the card is the approval itself, decided with the same call as Needs you.
 */
import { Check } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useApprovals, useDecide } from '@/api/hooks';
import type { Approval } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { CopyButton } from '@/components/ui/misc';
import { cn } from '@/lib/cn';
import { siteName } from '@/lib/sites';
import { PostPreview } from '../needs-you/post-preview';
import { useShown } from '../needs-you/decision-card';
import type { StepContext } from './context';
import { StepFooter } from './context';
import { starterMessage } from './goals';
import { Ready, SetupLayout } from './layout';
import { DoneRow, Landscape } from './pictures';
import { agentName, itemOf, useRefreshSetup } from './setup';

const ASKING = "This is what asking looks like. From now on you only see what you asked to see, plus money and things that can't be undone.";

function Timeline({ rows }: { rows: { title: string; detail: string; state: 'done' | 'now' | 'later' }[] }) {
  return (
    <ol aria-label="What's happening" className="grid gap-4">
      {rows.map((row) => (
        <li key={row.title} className="flex gap-3">
          <span
            aria-hidden="true"
            className={cn(
              'mt-px inline-flex size-5 shrink-0 items-center justify-center rounded-full',
              row.state === 'done' && 'bg-(--ok-soft) text-(--ok-dot)',
              row.state === 'now' && 'bg-(--current-soft)',
              row.state === 'later' && 'border border-line',
            )}
          >
            {row.state === 'done' ? <Check className="size-3" strokeWidth={3} /> : row.state === 'now' ? <span className="size-2 animate-pulse-dot rounded-full bg-(--current)" /> : null}
          </span>
          <span className="min-w-0">
            <span className={cn('block text-label', row.state === 'later' ? 'text-muted' : 'font-medium')}>{row.title}</span>
            <span className="block text-secondary text-muted">{row.detail}</span>
          </span>
        </li>
      ))}
    </ol>
  );
}

function Arrived({ org, approval, business, onDone }: { org: string; approval: Approval; business: { name: string; color?: string | null } | null; onDone: (decision: 'approve' | 'deny') => void }) {
  const shown = useShown(org, approval, business?.name ?? null);
  const decide = useDecide(org);
  const refresh = useRefreshSetup(org);
  const act = (decision: 'approve' | 'deny') =>
    decide.mutate(
      { items: [approval], decision },
      {
        onSuccess: () => {
          onDone(decision);
          // The decision is held a few seconds so it can be undone; Getting started counts it after.
          setTimeout(refresh, 8_000);
        },
      },
    );
  return (
    <article aria-label={shown.question} className="grid gap-3 rounded-xl border border-(--current-line) bg-card p-4 shadow-[0_0_0_4px_var(--current-soft)]">
      <p className="flex items-center gap-2 text-secondary text-muted">
        {shown.where.site ? <SiteMark site={shown.where.site} size={16} label={false} /> : null}
        <span className="truncate">{shown.where.place ?? business?.name}</span>
        <span className="ml-auto font-medium text-(--current)">Needs you</span>
      </p>
      <h2 className="text-section-title">{shown.question}</h2>
      <PostPreview approval={approval} where={shown.where} color={business?.color ?? null} />
      <div className="grid grid-cols-2 gap-2">
        <Button size="lg" disabled={decide.isPending} onClick={() => act('deny')}>
          {shown.verbs.no}
        </Button>
        <Button variant="primary" size="lg" loading={decide.isPending && decide.variables?.decision === 'approve'} icon={<Check className="size-4" />} onClick={() => act('approve')}>
          {shown.verbs.yes}
        </Button>
      </div>
      <p className="text-caption text-faint">{shown.verbs.noMeans}</p>
    </article>
  );
}

function WaitingCard() {
  return (
    <div aria-hidden="true" className="grid gap-3 rounded-xl border border-dashed border-line-strong p-4 opacity-70">
      <p className="text-secondary text-faint">It will appear here</p>
      <div className="h-4 w-3/4 rounded-sm bg-pressed" />
      <div className="overflow-hidden rounded-lg border border-line">
        <Landscape className="h-28 opacity-60 grayscale" />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <span className="h-10 rounded-lg border border-line" />
        <span className="h-10 rounded-lg bg-pressed" />
      </div>
    </div>
  );
}

export function TryStep({ ctx }: { ctx: StepContext }) {
  const { org, setup, goal, business } = ctx;
  const approvals = useApprovals(org).data;
  const ai = itemOf(setup, 'ai');
  const firstDone = itemOf(setup, 'approval')?.done ?? false;
  const agent = agentName(setup) ?? 'your AI';
  const site = setup?.sites?.[0] ?? goal?.sites[0] ?? null;
  const message = useMemo(
    () => starterMessage(goal, business?.name ?? 'my business', site ? siteName(site) : null, setup?.goal_note),
    [goal, business?.name, site, setup?.goal_note],
  );

  // Only what arrives while this step is open is "the first try"; older ones wait in Needs you.
  const opened = useRef(Date.now());
  const [decided, setDecided] = useState<'approve' | 'deny' | null>(null);
  const [kept, setKept] = useState<Approval | null>(null);
  const fresh = (approvals ?? []).filter((item) => new Date(item.created_at).getTime() >= opened.current - 10 * 60_000);
  const arrived = kept ?? fresh[0] ?? null;
  useEffect(() => {
    if (!kept && fresh[0]) setKept(fresh[0]);
  }, [kept, fresh]);
  const waitingElsewhere = (approvals ?? []).length - fresh.length;

  const rows: { title: string; detail: string; state: 'done' | 'now' | 'later' }[] = [
    ai?.done
      ? { title: `${agent} is connected`, detail: 'Ready when you paste the message.', state: 'done' }
      : setup?.agent
        ? { title: `Connection made for ${agent}`, detail: 'It counts as connected once it first uses agentos.', state: 'done' }
        : { title: 'Connect your AI', detail: 'Do the step before this one first.', state: 'later' },
    {
      title: arrived ? `${agent} did the work` : `Waiting for ${agent}`,
      detail: arrived ? 'A draft only. Nothing is public yet.' : 'Paste the message into it. Its first request shows here.',
      state: arrived ? 'done' : 'now',
    },
    {
      title: decided ? (decided === 'approve' ? 'You said yes' : 'You said not now') : 'Asking you',
      detail: decided ? 'That is the whole loop. It works the same from your phone.' : arrived ? 'Here it is. It also went to your phone, if it is set up.' : 'The card appears here when it asks.',
      state: decided ? 'done' : arrived ? 'now' : 'later',
    },
  ];

  return (
    <SetupLayout
      org={org}
      business={business}
      step="try"
      setup={setup}
      wide
      eyebrow={ai?.done ? <Ready>{agent} is connected</Ready> : undefined}
      title="Give it its first job"
      description={`Copy this into ${agent}. It turns on Ask me first for this job, so you see what asking looks like. After that, your routines run on their own unless you ask to see them first.`}
      footer={
        <StepFooter
          ctx={ctx}
          step="try"
          note={decided || firstDone ? 'You have seen it ask. One last step.' : 'Your first yes or no is the last thing setup needs from you.'}
          later={decided || firstDone ? false : "I'll try this later"}
          quiet={!decided && !firstDone}
        />
      }
    >
      <div className="grid items-start gap-8 lg:grid-cols-[minmax(0,1fr)_360px]">
        <div className="grid gap-8">
          <div className="grid gap-3 rounded-xl border border-line bg-inset p-4">
            <p className="text-lede leading-relaxed">{message}</p>
            <div className="flex flex-wrap items-center gap-2 border-t border-line pt-3">
              <p className="mr-auto text-secondary text-faint">{goal ? `Written for "${goal.title}". Change anything you like.` : 'Change anything you like.'}</p>
              <CopyButton text={message} label="Copy" />
            </div>
          </div>
          <section aria-labelledby="happening" className="grid gap-4">
            <h2 id="happening" className="text-label">
              What's happening
            </h2>
            <Timeline rows={rows} />
            {waitingElsewhere > 0 && !arrived ? <p className="text-secondary text-muted">{waitingElsewhere} older {waitingElsewhere === 1 ? 'request waits' : 'requests wait'} in Needs you.</p> : null}
          </section>
        </div>
        <div className="grid gap-3">
          {decided ? (
            <div className="grid gap-3 rounded-xl border border-line p-4">
              <DoneRow>{decided === 'approve' ? 'Approved. It goes ahead now.' : 'Not now. Nothing happened.'}</DoneRow>
              <p className="text-secondary text-muted">{ASKING}</p>
            </div>
          ) : arrived ? (
            <Arrived org={org} approval={arrived} business={business} onDone={setDecided} />
          ) : (
            <WaitingCard />
          )}
          {!decided ? <p className="text-secondary text-muted">{ASKING}</p> : null}
        </div>
      </div>
    </SetupLayout>
  );
}
