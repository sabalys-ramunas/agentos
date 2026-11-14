import { useState } from 'react';
import { useClearHolds, useEngageItems, useEngageProgram, useWithdrawItem } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EngageDay, EngageHold, EngageItem, EngageWeekDay } from '@/api/types';
import { StackedBars, type BarBucket } from '@/components/charts/stacked-bars';
import { Button } from '@/components/ui/button';
import { Card, Section } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { Meter } from '@/components/ui/misc';
import { Status, StatusDot } from '@/components/ui/status';
import { EmptyState, Skeleton } from '@/components/ui/states';
import { Table, Td, Th, Tr, mainCell } from '@/components/ui/table';
import { clock, timeUntil } from '@/lib/format';
import { visitStatus } from '@/lib/labels';
import { plainDetail } from '@/lib/plain-node';
import { useTechnical } from '@/lib/technical';
import { ENGAGEMENT } from '@/lib/vocabulary';
import { cn } from '@/lib/cn';
import { kindName, limitLine, nearLimit, pauseTitle, pauseUntil, skipWords, visitWhen } from './words';

/** What the account did on the day: its week, pauses, visits, what went out, limits, and what waits for you. */
export function TodayTab({ org, ws, name, data, week }: { org: string; ws: string; name: string; data: EngageDay; week: EngageWeekDay[] }) {
  const program = useEngageProgram(org, ws, name);
  const mayChange = program.data?.may_change ?? false;
  return (
    <div className="grid gap-10">
      <WeekBars week={week} />
      <Pauses org={org} ws={ws} name={name} holds={data.holds} mayChange={mayChange} />
      <Section title="What went out">
        <Outcomes data={data} />
      </Section>
      <Section title={ENGAGEMENT.visits.word}>
        <Visits data={data} />
      </Section>
      <Section title={ENGAGEMENT.limits.word}>
        <Limits data={data} />
      </Section>
      <Waiting data={data} />
      <Queued org={org} ws={ws} name={name} mayChange={mayChange} />
      {data.notices.length ? (
        <Section title="Notes from the day">
          <Card>
            <ul className="divide-y divide-line">
              {data.notices.map((notice) => (
                <li key={`${notice.at}-${notice.text}`} className="px-4 py-3 text-14">
                  <span className="tabular mr-3 text-13 text-faint">{clock(notice.at)}</span>
                  {plainDetail(notice.text)}
                </li>
              ))}
            </ul>
          </Card>
        </Section>
      ) : null}
    </div>
  );
}

const SERIES = [
  { key: 'verified', label: 'Went out', color: 'var(--ok-dot)' },
  { key: 'awaiting', label: 'Waiting for you', color: 'var(--wait-dot)' },
];

/** The last seven days: what went out and what still waits, one bar a day. Read only. */
function WeekBars({ week }: { week: EngageWeekDay[] }) {
  const buckets: BarBucket[] = week.map((d) => ({
    at: `${d.day}T00:00:00Z`,
    values: { verified: d.verified, awaiting: d.awaiting_approval },
  }));
  const total = week.reduce((sum, d) => sum + d.verified, 0);
  return (
    <Section title="The last seven days" description={`${total} ${total === 1 ? 'action' : 'actions'} went out, one bar a day.`}>
      <Card className="p-4">
        <StackedBars label="Actions a day, and what waits for you" series={SERIES} buckets={buckets} bucketSeconds={86_400} height={148} />
      </Card>
    </Section>
  );
}

/** One pause, in a sentence, with the node's own words under it. Only the owner starts one again. */
function Pauses({ org, ws, name, holds, mayChange }: { org: string; ws: string; name: string; holds: EngageHold[]; mayChange: boolean }) {
  const active = holds.filter((hold) => hold.active_now);
  if (!active.length) return null;
  return (
    <Section title={ENGAGEMENT.pause.word}>
      <Card>
        <ul className="divide-y divide-line">
          {active.map((hold) => (
            <PauseRow key={hold.id} org={org} ws={ws} name={name} hold={hold} mayChange={mayChange} />
          ))}
        </ul>
      </Card>
    </Section>
  );
}

function PauseRow({ org, ws, name, hold, mayChange }: { org: string; ws: string; name: string; hold: EngageHold; mayChange: boolean }) {
  const [technical] = useTechnical();
  const clear = useClearHolds(org);
  const [starting, setStarting] = useState(false);
  const error = clear.error instanceof ApiError ? clear.error.message : null;
  return (
    <li className="grid gap-2 px-4 py-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-2">
          <StatusDot tone="warn" />
          <p className="min-w-0 text-14 font-medium">{pauseTitle(hold)}</p>
        </div>
        <p className="mt-1 text-13 text-muted">{pauseUntil(hold)}</p>
        {hold.detail ? <p className="mt-1 text-13 text-muted">{hold.detail}</p> : null}
        {technical ? <p className="mono mt-1 text-12 text-faint">hold {hold.id} · {hold.reason}</p> : null}
        {error ? <p role="alert" className="mt-2 text-13 text-red">{error}</p> : null}
      </div>
      {mayChange ? (
        <Button onClick={() => setStarting(true)}>{ENGAGEMENT.startAgain.word}</Button>
      ) : (
        <p className="text-13 text-muted">Only the owner can start it again.</p>
      )}
      <ConfirmDialog
        open={starting}
        onOpenChange={setStarting}
        title="Start this account again?"
        description={`${pauseTitle(hold)}. Check the account on the site first if the site asked for a look. Its plan can run again after you start it.`}
        confirmLabel={ENGAGEMENT.startAgain.word}
        loading={clear.isPending}
        onConfirm={() => clear.mutate({ ws, name, id: hold.id }, { onSuccess: () => setStarting(false) })}
      />
    </li>
  );
}

/** Each kind of action the plan asked for, what became of it, and why any were skipped. */
function Outcomes({ data }: { data: EngageDay }) {
  if (!data.operations.length) {
    return (
      <EmptyState
        title={data.sessions_on ? 'Nothing was planned for this day' : 'Sessions are off'}
        description={data.sessions_on ? 'The plan had no actions for this day, so none went out.' : 'These are the plans only. Nothing has been done on the account.'}
        className="py-10"
      />
    );
  }
  return (
    <Card className="overflow-x-auto">
      <Table>
        <thead>
          <Tr>
            <Th className={mainCell}>Action</Th>
            <Th>Planned</Th>
            <Th>Went out</Th>
            <Th className="hidden md:table-cell">Waiting for you</Th>
            <Th className="hidden md:table-cell">Held</Th>
            <Th className="hidden md:table-cell">Skipped</Th>
          </Tr>
        </thead>
        <tbody>
          {data.operations.map((op) => (
            <Tr key={op.kind}>
              <Td className={mainCell}>
                <span className="block text-14">{kindName(op.kind)}</span>
                <span className="mt-1 block text-13 text-muted md:hidden">
                  {op.awaiting_approval ? `${op.awaiting_approval} waiting for you` : ''}
                  {op.skipped ? `${op.awaiting_approval ? ' · ' : ''}${op.skipped} skipped` : ''}
                </span>
              </Td>
              <Td><span className="tabular text-14">{op.planned}</span></Td>
              <Td>
                <span className="tabular text-14">{op.verified}</span>
                {op.verified < op.attempted ? <span className="tabular text-13 text-faint"> of {op.attempted}</span> : null}
              </Td>
              <Td className="hidden md:table-cell"><span className="tabular text-14">{op.awaiting_approval}</span></Td>
              <Td className="hidden md:table-cell"><span className="tabular text-14">{op.held}</span></Td>
              <Td className="hidden md:table-cell">
                <span className="tabular text-14">{op.skipped}</span>
                {op.skipped ? <span className="block text-12 text-faint">{skipWords(op.skip_reasons)}</span> : null}
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

/** Each visit the plan made for the day, when it was planned and how it ended. */
function Visits({ data }: { data: EngageDay }) {
  const windows = data.sessions.windows;
  if (!windows.length) {
    return (
      <EmptyState
        title="No visits on this day"
        description={data.quiet ? 'It was a quiet day: nothing but replies to the account’s own posts.' : 'The plan had no visits for this day.'}
        className="py-10"
      />
    );
  }
  return (
    <Card>
      <ul className="divide-y divide-line">
        {windows.map((w) => (
          <li key={w.idx} className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3">
            <span className="min-w-0 flex-1 text-14">{visitWhen(w)}</span>
            <Status status={visitStatus[w.status]} />
            {w.end_reason ? <span className="text-13 text-muted">{w.end_reason}</span> : null}
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** Today's use of each daily limit, against the owner's own limit or the ramp. */
function Limits({ data }: { data: EngageDay }) {
  if (!data.caps.length) return <p className="text-14 text-muted">No limits apply to this account yet.</p>;
  return (
    <Card>
      <ul className="divide-y divide-line">
        {data.caps.map((use) => {
          const limit = use.cap ?? use.ceiling;
          return (
            <li key={use.kind} className="grid gap-2 px-4 py-3 md:grid-cols-[minmax(0,1fr)_200px] md:items-center">
              <div className="flex min-w-0 items-center gap-3">
                <span className={cn('tabular min-w-0 truncate text-14', nearLimit(use) && 'font-medium')}>{kindName(use.kind)}</span>
              </div>
              <div className="flex min-w-0 items-center gap-3">
                <Meter value={use.used} max={limit} label={`${kindName(use.kind)} used today`} className="w-full min-w-0" />
                <span className="tabular shrink-0 text-13 text-muted">{limitLine(use)}</span>
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

/** Drafts the provider wrote that wait for the owner. The yes is given in Needs you, where it is always checked. */
function Waiting({ data }: { data: EngageDay }) {
  // What waits for the owner now, whichever day it was written: a draft from yesterday still waits today.
  const waiting = data.awaiting_now.drafts;
  if (!waiting.length) return null;
  return (
    <Section title={`Waiting for you (${waiting.length})`} description="Each one goes out only after you say yes in Needs you.">
      <Card>
        <ul className="divide-y divide-line">
          {waiting.map((draft) => (
            <li key={draft.id} className="grid gap-1 px-4 py-3">
              <div className="flex min-w-0 items-center gap-2">
                <StatusDot tone="warn" />
                <span className="text-14">{kindName(draft.kind)}</span>
                {draft.target ? <span className="min-w-0 truncate text-13 text-muted">on {draft.target}</span> : null}
                <span className="tabular ml-auto shrink-0 text-13 text-faint">{clock(draft.created_at)}</span>
              </div>
              {draft.preview ? <p className="text-13 text-muted">“{draft.preview}”</p> : null}
            </li>
          ))}
        </ul>
      </Card>
    </Section>
  );
}

/** Items the owner queued for a session to send, and a way to take one back. */
function Queued({ org, ws, name, mayChange }: { org: string; ws: string; name: string; mayChange: boolean }) {
  const items = useEngageItems(org, ws, name);
  const withdraw = useWithdrawItem(org, ws, name);
  const [taking, setTaking] = useState<EngageItem | null>(null);
  const queued = (items.data?.items ?? []).filter((item) => item.status === 'queued');
  if (items.isPending) return <Skeleton className="h-24 rounded-xl" />;
  if (!queued.length) return null;
  return (
    <Section title={ENGAGEMENT.queued.word} description="Sent on a visit before its last day. Taking one back means it is not sent.">
      <Card>
        <ul className="divide-y divide-line">
          {queued.map((item) => (
            <li key={item.id} className="grid gap-1 px-4 py-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
              <div className="min-w-0">
                <p className="truncate text-14">{item.title ?? (item.kind === 'post' ? 'A post' : 'A comment')}</p>
                <p className="text-13 text-muted">
                  {item.targets.length ? `${item.targets.join(', ')} · ` : ''}expires {timeUntil(item.expires_at)}
                </p>
                {item.text ? <p className="mt-1 line-clamp-2 text-13 text-muted">{item.text}</p> : null}
              </div>
              {mayChange ? (
                <Button variant="secondary" size="sm" onClick={() => setTaking(item)}>
                  {ENGAGEMENT.takeBack.word}
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
      </Card>
      <ConfirmDialog
        open={taking !== null}
        onOpenChange={(open) => {
          if (!open) setTaking(null);
        }}
        title="Take this back?"
        description="It will not be sent. Adding it again puts it back in the queue."
        confirmLabel={ENGAGEMENT.takeBack.word}
        loading={withdraw.isPending}
        danger
        onConfirm={() => {
          if (!taking) return;
          withdraw.mutate(taking.id, { onSuccess: () => setTaking(null) });
        }}
      />
    </Section>
  );
}

