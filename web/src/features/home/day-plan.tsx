/**
 * The day's plan, under what needs a person on Home: what is blocked, what waits in the queue, what is due,
 * and what to look at. It is a draft that queues and sends nothing, and it is shown only when it has something
 * to say, so a quiet day leaves no empty block on Home. The node's sentences are shown as they are written.
 */
import { Link } from '@tanstack/react-router';
import { useDayPlan, useWorkspaces } from '@/api/hooks';
import type { PlanItem, PlanSection, Workspace } from '@/api/types';
import { Card } from '@/components/ui/card';
import { StatusDot } from '@/components/ui/status';
import { laneName, planAction, planReadiness, planSection } from '@/lib/labels';

/** The order the sections read in: what blocks first, then the queue, what is due, the programs, and upkeep. */
const ORDER: PlanSection[] = ['blocked', 'queue', 'due', 'programs', 'upkeep'];

/** The one sentence on top, from the counts: what is blocked, else what waits, else what there is to look at. */
export function planSentence(items: PlanItem[]): string {
  const count = (section: PlanSection) => items.filter((item) => item.section === section).length;
  const blocked = count('blocked');
  if (blocked) return blocked === 1 ? '1 thing is blocked today.' : `${blocked} things are blocked today.`;
  const queued = count('queue');
  if (queued) return queued === 1 ? '1 thing waits in the queue.' : `${queued} things wait in the queue.`;
  return items.length === 1 ? '1 thing to look at today.' : `${items.length} things to look at today.`;
}

export function DayPlanStrip({ org, workspace }: { org: string; workspace: string }) {
  const plan = useDayPlan(org, workspace).data;
  const names = useWorkspaces(org).data ?? [];
  if (!plan || (plan.items.length === 0 && plan.content.length === 0)) return null;
  const sections = ORDER.filter((section) => plan.items.some((item) => item.section === section));
  return (
    <section aria-labelledby="home-plan">
      <div className="mb-4 flex flex-wrap items-end justify-between gap-x-4 gap-y-1">
        <h2 id="home-plan" className="text-card-title">
          The day
        </h2>
        <p className="text-13 text-faint">A draft. It queues and sends nothing.</p>
      </div>
      <Card className="overflow-hidden">
        <p className="flex items-center gap-2 border-b border-line px-4 py-3 text-14 font-medium">
          <StatusDot tone={plan.items.some((item) => item.section === 'blocked') ? 'warn' : 'neutral'} />
          {planSentence(plan.items)}
        </p>
        <div className="divide-y divide-line">
          {sections.map((section) => (
            <PlanGroup key={section} section={section} items={plan.items.filter((item) => item.section === section)} names={names} org={org} />
          ))}
          {plan.content.length ? (
            <div className="grid gap-2 px-4 py-3">
              <h3 className="text-13 text-faint">Drafted, not sent</h3>
              <ul className="grid gap-2">
                {plan.content.map((item, index) => (
                  <li key={`${item.surface}-${index}`} className="text-14 break-words">
                    <span className="text-faint">{item.surface}</span> {item.text}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </Card>
      {plan.unavailable.length ? <p className="mt-2 text-13 text-faint">Part of the day could not be read, so this may be missing something.</p> : null}
    </section>
  );
}

/** Home keeps the day short: the first rows of each group, and how many more. */
const SHOWN = 3;

function PlanGroup({ section, items, names, org }: { section: PlanSection; items: PlanItem[]; names: Workspace[]; org: string }) {
  return (
    <div className="grid gap-2 px-4 py-3">
      <h3 className="text-13 text-faint">{planSection[section]}</h3>
      <ul className="grid gap-2">
        {items.slice(0, SHOWN).map((item, index) => (
          <PlanRow key={`${section}-${item.lane ?? 'all'}-${index}`} item={item} names={names} org={org} />
        ))}
      </ul>
      {items.length > SHOWN ? <p className="text-caption text-faint">and {items.length - SHOWN} more</p> : null}
    </div>
  );
}

function PlanRow({ item, names, org }: { item: PlanItem; names: Workspace[]; org: string }) {
  const [ws, account] = item.lane ? item.lane.split('/') : [null, null];
  const place = ws && account ? `${names.find((row) => row.id === ws)?.name ?? ws} · ${laneName(account)}` : null;
  // A blocked account says what it is and what to do, in the screen's words. The node's sentence is for the command line.
  const readiness = item.section === 'blocked' && item.readiness ? item.readiness : null;
  return (
    <li className="grid gap-1 text-14">
      {readiness ? (
        <span className="break-words">
          <span className="font-medium">{planReadiness[readiness]?.label ?? 'Blocked'}.</span> <span className="text-muted">{planAction[readiness] ?? ''}</span>
        </span>
      ) : (
        <span className="break-words">{item.text}</span>
      )}
      {place && ws && account ? (
        <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name: account }} className="inline-flex min-h-8 w-fit items-center text-13 text-muted hover:text-fg hover:underline md:min-h-0">
          {place}
        </Link>
      ) : null}
    </li>
  );
}

