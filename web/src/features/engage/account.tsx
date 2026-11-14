import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ChevronLeft } from 'lucide-react';
import { useEngageAccountDay, useLane, useWorkspaces } from '@/api/hooks';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { PageHeader } from '@/components/ui/misc';
import { ErrorState, Skeleton } from '@/components/ui/states';
import { Tab, TabList, TabPanel, Tabs } from '@/components/ui/tabs';
import { accountName } from '@/features/home/words';
import { laneName } from '@/lib/labels';
import { ENGAGEMENT, PLACES } from '@/lib/vocabulary';
import { ConnectedTab } from './connected';
import { PlanTab } from './plan';
import { DayPicker } from './day-picker';
import { TodayTab } from './today';
import { dayLabel, daySentence, localDay } from './words';

type EngageTab = 'today' | 'plan' | 'connected';
const TABS: EngageTab[] = ['today', 'plan', 'connected'];

/** One account's engagement: what its plan did, the plan itself, and what it is connected to. */
export function AccountEngagement() {
  const { org, ws, name } = useParams({ from: '/$org/lanes/$ws/$name/engagement' });
  const search = useSearch({ from: '/$org/lanes/$ws/$name/engagement' });
  const navigate = useNavigate({ from: '/$org/lanes/$ws/$name/engagement' });
  const lane = useLane(org, ws, name);
  const workspaces = useWorkspaces(org).data ?? [];
  const tab: EngageTab = search.tab ?? 'today';
  const today = localDay();
  const shown = search.day ?? today;
  const record = useEngageAccountDay(org, ws, name, shown);
  // The business and the account by their names, never their ids ("Biz A · Social", not "biz-a · social").
  const place = workspaces.find((item) => item.id === ws)?.name ?? ws;
  const title = `${place} · ${lane.data ? accountName(lane.data) : laneName(name)}`;
  const day = record.data?.available ? record.data.record : null;

  return (
    <>
      <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }} className="mb-4 inline-flex items-center gap-1 text-13 text-faint hover:text-fg">
        <ChevronLeft className="size-4" />
        {lane.data ? `Back to ${accountName(lane.data)}` : `Back to ${PLACES.accounts.singular.toLowerCase()}`}
      </Link>
      <PageHeader
        title={`${ENGAGEMENT.place.word}: ${title}`}
        description={day ? daySentence([day], day.sessions_on) : `${dayLabel(shown, today)}.`}
        actions={
          <Button asChild size="sm" variant="secondary">
            <Link to="/$org/accounts/engagement" params={{ org }} search={{ day: undefined }}>
              All accounts
            </Link>
          </Button>
        }
      />
      <Tabs
        value={tab}
        onValueChange={(next) =>
          void navigate({ search: (previous) => ({ ...previous, tab: next === 'today' ? undefined : (next as EngageTab) }), replace: true, resetScroll: false })
        }
        className="mt-8"
      >
        <TabList aria-label={ENGAGEMENT.place.word} className="sticky top-(--topbar-height) z-10 bg-bg">
          {TABS.map((key) => (
            <Tab key={key} value={key}>
              {key === 'today' ? 'Today' : key === 'plan' ? ENGAGEMENT.plan.word : 'Connections'}
            </Tab>
          ))}
        </TabList>
        <TabPanel value="today" className="grid gap-6 pt-6">
          <DayPicker
            value={shown}
            today={today}
            onChange={(next) =>
              void navigate({ search: (previous) => ({ ...previous, day: next === today ? undefined : next }), replace: true, resetScroll: false })
            }
          />
          {record.isError ? (
            <Card>
              <ErrorState error={record.error} onRetry={() => void record.refetch()} />
            </Card>
          ) : record.isPending ? (
            <Skeleton className="h-64 rounded-xl" />
          ) : day && record.data ? (
            <TodayTab org={org} ws={ws} name={name} data={day} week={record.data.week} />
          ) : (
            <Card className="px-6 py-12 text-center">
              <p className="text-14 text-muted">This computer does not run engagement, so this account has no day to show.</p>
            </Card>
          )}
        </TabPanel>
        <TabPanel value="plan" className="pt-6">
          <PlanTab org={org} ws={ws} name={name} />
        </TabPanel>
        <TabPanel value="connected" className="pt-6">
          <ConnectedTab org={org} ws={ws} name={name} />
        </TabPanel>
      </Tabs>
    </>
  );
}
