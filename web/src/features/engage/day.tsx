import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { useEngageDay, useWorkspaces } from '@/api/hooks';
import type { EngageDay } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Meter, PageHeader } from '@/components/ui/misc';
import { Status, StatusDot } from '@/components/ui/status';
import { EmptyState, ErrorState, NoAccess, Skeleton } from '@/components/ui/states';
import { Table, Td, Th, Tr, mainCell } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { planStatus } from '@/lib/labels';
import { ENGAGEMENT, PLACES } from '@/lib/vocabulary';
import { DayPicker } from './day-picker';
import { accountTitleOf, dayLabel, daySentence, localDay, tightLimitText, tightestLimit } from './words';

/** The accounts' day across every account the person may see: the plan, what went out, what is held, and the limits. */
export function EngagementDay() {
  const { org } = useParams({ from: '/$org/accounts/engagement' });
  const search = useSearch({ from: '/$org/accounts/engagement' });
  const navigate = useNavigate({ from: '/$org/accounts/engagement' });
  const today = localDay();
  const shown = search.day ?? today;
  const query = useEngageDay(org, shown);

  if (query.isError && (query.error as { status?: number }).status === 403) {
    return <NoAccess />;
  }

  const data = query.data;
  const surfaces = data?.surfaces ?? [];
  const waiting = surfaces.reduce((sum, row) => sum + row.awaiting_now.count, 0);

  return (
    <>
      <PageHeader
        title={data ? daySentence(surfaces, data.sessions_on) : `${ENGAGEMENT.place.word}`}
        description={`${dayLabel(shown, today)}. What each account’s plan did, what is held, and the daily limit it came closest to.`}
      />
      <div className="mt-6">
        <DayPicker
          value={shown}
          today={today}
          onChange={(next) => void navigate({ search: { day: next === today ? undefined : next }, replace: true, resetScroll: false })}
        />
      </div>

      {data && !data.available ? (
        <EmptyState
          title="Engagement is not on this computer"
          description="This computer does not run engagement, so no account here has a plan."
          className="py-16"
        />
      ) : query.isPending ? (
        <Skeleton className="mt-8 h-48 rounded-xl" />
      ) : query.isError ? (
        <Card>
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        </Card>
      ) : surfaces.length === 0 ? (
        <EmptyState
          title="No account has a plan yet"
          description="An account starts engaging once it has a plan. An agent drafts one, and nothing runs until you approve it."
          action={
            <Button asChild variant="primary">
              <Link to="/$org/accounts" params={{ org }}>
                Go to {PLACES.accounts.word}
              </Link>
            </Button>
          }
          className="py-16"
        />
      ) : (
        <div className="mt-8 grid gap-6">
          {waiting > 0 ? (
            <div role="status" className="flex items-center gap-3 rounded-xl border border-line px-4 py-3">
              <StatusDot tone="warn" />
              <p className="min-w-0 flex-1 text-14">
                {waiting === 1 ? '1 thing waits for you' : `${waiting} things wait for you`}
              </p>
              <Link to="/$org/needs-you" params={{ org }} className="shrink-0 text-13 text-blue hover:underline">
                Open {PLACES.needsYou.word}
              </Link>
            </div>
          ) : null}
          <AccountTable org={org} rows={surfaces} />
        </div>
      )}
    </>
  );
}

/** What went out of what the plan asked for: the count that went out, and the count it tried, when they differ. */
function wentOut(row: EngageDay): string {
  const { verified, attempted } = row.totals;
  return verified < attempted ? `${verified} of ${attempted}` : `${verified}`;
}

/** Held is the number of pauses in force now. A zero is a number, so the column never looks empty. */
function heldNow(row: EngageDay): number {
  return row.holds.filter((hold) => hold.active_now).length;
}

/** One row an account. The primary cell is the account; the counts fit their content and sit beside it. */
function AccountTable({ org, rows }: { org: string; rows: EngageDay[] }) {
  return (
    <Card className="overflow-x-auto">
      <Table>
        <thead>
          <Tr>
            <Th className={mainCell}>Account</Th>
            <Th>Plan</Th>
            <Th className="hidden md:table-cell">Drafted</Th>
            <Th className="hidden md:table-cell">Went out</Th>
            <Th className="hidden md:table-cell">Waiting for you</Th>
            <Th className="hidden md:table-cell">Held</Th>
            <Th className="hidden md:table-cell">Daily limits</Th>
          </Tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <AccountRow key={row.surface} org={org} row={row} />
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

function AccountRow({ org, row }: { org: string; row: EngageDay }) {
  const workspaces = useWorkspaces(org).data;
  const [ws = '', name = ''] = row.surface.split('/');
  const state = row.program ? (row.program.approved ? 'approved' : 'draft') : 'none';
  const tight = tightestLimit(row.caps);
  const held = heldNow(row);
  return (
    <Tr>
      <Td className={cn(mainCell, 'min-w-0')}>
        <Link to="/$org/lanes/$ws/$name/engagement" params={{ org, ws, name }} className="flex min-w-0 items-center gap-3 hover:underline">
          <SiteMark site={row.site} size={20} label={false} className="shrink-0" />
          <span className="min-w-0">
            <span className="block truncate text-14">{accountTitleOf(row.surface, workspaces)}</span>
            <span className="block whitespace-normal text-13 text-muted md:hidden">
              Drafted {row.drafts.created} · Went out {wentOut(row)} · Waiting {row.awaiting_now.count} · Held {held}
              {tight ? ` · ${tightLimitText(tight)}` : ''}
            </span>
          </span>
        </Link>
      </Td>
      <Td>
        <Status status={planStatus[state]} />
      </Td>
      <Td className="hidden md:table-cell">
        <span className="tabular text-14">{row.drafts.created}</span>
      </Td>
      <Td className="hidden md:table-cell">
        <span className="tabular text-14">{wentOut(row)}</span>
      </Td>
      <Td className="hidden md:table-cell">
        <span className="tabular text-14">{row.awaiting_now.count}</span>
      </Td>
      <Td className="hidden md:table-cell">
        <span className="flex items-center gap-2">
          {held ? <StatusDot tone="warn" /> : null}
          <span className="tabular text-14">{held}</span>
        </span>
      </Td>
      <Td className="hidden md:table-cell">
        {tight ? (
          <div className="flex min-w-0 items-center gap-3">
            <Meter value={tight.used} max={tight.cap ?? tight.ceiling} label={`${tightLimitText(tight)} today`} className="w-24 shrink-0" />
            <span className="tabular truncate text-13 text-muted">{tightLimitText(tight)}</span>
          </div>
        ) : (
          <span className="text-13 text-muted">No limits</span>
        )}
      </Td>
    </Tr>
  );
}
