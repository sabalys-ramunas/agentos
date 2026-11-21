/**
 * The login queue (spec-16, logins.png): what makes 29 accounts workable. On the left, how far along it is and the
 * logins to do; on the right, the open one in its own browser, then on to the next. Nothing here ever asks for a third-party password. The person
 * types it into the real site in the live view, and it stays on the node.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { Check, SkipForward } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useLane, useLaneAction, useLoginQueue, useSetReadiness, useVault, useWorkspaces } from '@/api/hooks';
import type { Lane, LoginQueue, QueueLane, Workspace } from '@/api/types';
import { usePageCrumb } from '@/components/shell/crumb';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/field';
import { Meter, PageHeader } from '@/components/ui/misc';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { StatusDot } from '@/components/ui/status';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { useKeys } from '@/lib/keyboard';
import { laneName } from '@/lib/labels';
import { ControlFrame } from './control';
import { SignIn } from './sign-in';
import { LiveView } from './live-view';
import { useFullWindow } from './live/full-window';

/** Only a lane with a screen can fill the window. */
const awake = (lane: Lane) => lane.state === 'warm' || lane.state === 'active';

const nameOf = (workspaces: Workspace[], id: string) => workspaces.find((item) => item.id === id)?.name ?? id;

/** How far along: the accounts ready, what is left, and one short bar per business, green once it is complete. */
function Progress({ queue, workspaces }: { queue: LoginQueue; workspaces: Workspace[] }) {
  return (
    <Card className="grid gap-4 p-5">
      <div className="grid gap-1">
        <p className="text-card-title">
          <span className="tabular">{queue.ready}</span> of <span className="tabular">{queue.total}</span> accounts ready
        </p>
        <p className="tabular text-secondary text-muted">
          {queue.needs_login + queue.needs_attention === 0
            ? 'Nothing left to log in.'
            : [queue.needs_login ? `${plural(queue.needs_login, 'login')} to go` : null, queue.needs_attention ? `${queue.needs_attention} to look at` : null].filter(Boolean).join(', ')}
        </p>
      </div>
      <ul className="grid gap-3">
        {queue.workspaces.map((workspace) => (
          <li key={workspace.id} className="grid grid-cols-[minmax(0,1fr)_88px_40px] items-center gap-3 text-secondary">
            <span className="flex min-w-0 items-center gap-2">
              <BusinessMark name={workspace.name} color={workspaces.find((item) => item.id === workspace.id)?.color} size={20} />
              <span className="min-w-0 truncate">{workspace.name}</span>
            </span>
            <Meter value={workspace.ready} max={Math.max(1, workspace.total)} progress label={`${workspace.name}: ${workspace.ready} of ${workspace.total} accounts ready`} />
            <span className="tabular text-right text-muted">
              {workspace.ready}/{workspace.total}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** One login in the list: its site's tile, "Reddit · Aster Coffee", why it waits, and a red mark when it needs a look. */
function QueueRow({ item, current, workspace, onPick }: { item: QueueLane; current: boolean; workspace: string; onPick: () => void }) {
  const look = item.readiness === 'needs_attention';
  return (
    <button
      type="button"
      onClick={onPick}
      aria-current={current || undefined}
      className={cn(
        'focus-inset flex w-full items-center gap-3 border-l-2 py-3 pr-4 pl-4 text-left transition-colors duration-(--duration-fast) ease-standard',
        current ? 'border-accent bg-accent-soft' : 'border-transparent hover:bg-hover',
      )}
    >
      <SiteMark site={item.platform || item.name} size={32} tile label={false} className="rounded-lg bg-card shadow-lift" />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-body font-medium">
          {laneName(item.name)} · {workspace}
        </span>
        <span className={cn('block truncate text-secondary', look ? 'text-bad' : 'text-muted')}>{item.note ?? (look ? 'Needs a look' : 'Not signed in yet')}</span>
      </span>
      {look ? (
        <span className="shrink-0">
          <StatusDot tone="bad" />
          <span className="sr-only">Needs a look</span>
        </span>
      ) : null}
    </button>
  );
}

interface CurrentProps {
  org: string;
  item: QueueLane;
  workspace: string;
  left: number;
  canAct: boolean;
  onNext: () => void;
}

function Current({ org, item, workspace, left, canAct, onNext }: CurrentProps) {
  const lane = useLane(org, item.workspace, item.name);
  // The lane's own take over and stop are the owner's; a person who may operate signs in through the login form.
  const canTakeOver = useAccess(org).can('lanes.control');
  const action = useLaneAction(org, item.workspace, item.name);
  const setReadiness = useSetReadiness(org);
  const [noting, setNoting] = useState(false);
  const [note, setNote] = useState('');
  const [expanded, setExpanded] = useFullWindow();
  const vault = useVault(org);
  const savedHere = (vault.data ?? []).some((entry) => entry.kind === 'password' && entry.lanes.includes(`${item.workspace}/${item.name}`));

  useEffect(() => {
    setNoting(false);
    setNote('');
  }, [item.id]);

  const ready = () =>
    setReadiness.mutate(
      { id: item.id, state: 'ready' },
      {
        onSuccess: () => {
          toast(`${laneName(item.name)} is ready`);
          onNext();
        },
      },
    );
  const flag = () => {
    if (!note.trim()) return;
    setReadiness.mutate({ id: item.id, state: 'needs_attention', note: note.trim() }, { onSuccess: onNext });
  };

  useKeys({ r: ready, s: onNext }, canAct && !noting);

  const look = item.readiness === 'needs_attention';
  return (
    <Card className="grid min-w-0 content-start gap-5 p-5 sm:p-6">
      <div className="flex min-w-0 items-start gap-3">
        <SiteMark site={item.platform || item.name} size={40} tile label={false} className="rounded-lg bg-card shadow-lift" />
        <div className="min-w-0 flex-1">
          <h2 className="text-section-title [overflow-wrap:anywhere]">
            {laneName(item.name)} for {workspace}
          </h2>
          <p className={cn('text-secondary', look ? 'text-bad' : 'text-muted')}>{item.note ?? (look ? 'Needs a look' : 'Not signed in yet')}</p>
          <p className="tabular text-secondary text-muted sm:hidden">{plural(left, 'account')} left</p>
        </div>
        <p className="tabular shrink-0 pt-1 text-secondary text-muted max-sm:hidden">{plural(left, 'account')} left</p>
      </div>
      {lane.data ? (
        <ControlFrame org={org} lane={lane.data} canControl={canTakeOver} expanded={expanded && awake(lane.data)}>
          <LiveView
            org={org}
            lane={lane.data}
            opening={action.isPending}
            canOperate={canAct}
            canTake={canAct}
            onOpen={() => action.mutate('open')}
            expanded={expanded && awake(lane.data)}
            onExpandedChange={setExpanded}
          />
        </ControlFrame>
      ) : (
        <Skeleton className="aspect-[16/10] h-auto rounded-xl" />
      )}
      {/* The promise and the one thing to do: the password is typed into the account's own browser, never here. */}
      <SignIn org={org} ws={item.workspace} name={item.name} saved={savedHere} canAct={canAct} url={item.login_url} />
      {canAct ? (
        noting ? (
          <form
            className="flex flex-wrap gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              flag();
            }}
          >
            <Input autoFocus aria-label="What is wrong" placeholder="For example, phone code asked" className="min-w-48 flex-1" value={note} onChange={(event) => setNote(event.target.value)} />
            <Button onClick={() => setNoting(false)}>Cancel</Button>
            <Button type="submit" disabled={!note.trim()} loading={setReadiness.isPending}>
              Save and move on
            </Button>
          </form>
        ) : (
          <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line pt-5">
            <Button variant="ghost" className="mr-auto" onClick={() => setNoting(true)}>
              Something is wrong
            </Button>
            <Button variant="ghost" icon={<SkipForward className="size-4" />} onClick={onNext} shortcut="S">
              Skip for now
            </Button>
            <Button icon={<Check className="size-4" />} loading={setReadiness.isPending} onClick={ready} shortcut="R">
              Logged in, next
            </Button>
          </div>
        )
      ) : (
        <p className="border-t border-line pt-5 text-secondary text-muted">Your role can see this queue but not work through it.</p>
      )}
    </Card>
  );
}

export function Logins() {
  const { org } = useParams({ from: '/$org/logins' });
  const { lane } = useSearch({ from: '/$org/logins' });
  const navigate = useNavigate({ from: '/$org/logins' });
  const queue = useLoginQueue(org);
  const workspaces = useWorkspaces(org).data ?? [];
  const canAct = useAccess(org).can('lanes.operate');
  const items = useMemo(() => queue.data?.lanes ?? [], [queue.data]);
  const current = items.find((item) => item.id === lane) ?? items[0];

  usePageCrumb('Login queue', { mono: false });
  const pick = (id: string | undefined) => void navigate({ search: { lane: id }, replace: true });
  const next = () => {
    if (!current) return;
    const index = items.findIndex((item) => item.id === current.id);
    // Wraps, so skipping past the end comes back to what was skipped.
    pick(items[(index + 1) % items.length]?.id);
  };

  return (
    <>
      <PageHeader title="Login queue" description="Log in once per account, one at a time. The computer keeps the session." />
      <QueryView
        query={queue}
        card
        skeleton={
          <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
            <div className="grid gap-4">
              <Skeleton className="h-56 rounded-xl" />
              <Skeleton className="h-72 rounded-xl max-lg:hidden" />
            </div>
            <Skeleton className="h-[520px] rounded-xl" />
          </div>
        }
      >
        {(data) =>
          data.lanes.length === 0 ? (
            <Card>
              <EmptyState
                dawn
                heading="h2"
                title={`All ${data.total} accounts are ready`}
                description="Every account is logged in. If one is logged out later, it comes back here."
                action={
                  <Button asChild>
                    <Link to="/$org" params={{ org }}>
                      Go to Home
                    </Link>
                  </Button>
                }
              />
            </Card>
          ) : (
            <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,360px)_minmax(0,1fr)]">
              <div className="grid min-w-0 gap-4">
                <Progress queue={data} workspaces={workspaces} />
                <Card className="overflow-hidden max-lg:hidden">
                  <ul aria-label="Logins to do" className="max-h-[560px] divide-y divide-line overflow-y-auto">
                    {data.lanes.map((item) => (
                      <li key={item.id}>
                        <QueueRow item={item} current={item.id === current?.id} workspace={nameOf(workspaces, item.workspace)} onPick={() => pick(item.id)} />
                      </li>
                    ))}
                  </ul>
                </Card>
              </div>
              {current ? (
                <Current key={current.id} org={org} item={current} workspace={nameOf(workspaces, current.workspace)} left={data.lanes.length} canAct={canAct} onNext={next} />
              ) : null}
            </div>
          )
        }
      </QueryView>
    </>
  );
}
