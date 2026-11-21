/**
 * The login queue: what makes 29 accounts workable. One account at a time, in the lane's own
 * browser, then on to the next. Nothing here ever asks for a third-party password. The person
 * types it into the real site in the live view, and it stays on the node.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { Check, ChevronRight, KeyRound, SkipForward } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useLane, useLaneAction, useLoginQueue, useSetReadiness, useVault, useWorkspaces } from '@/api/hooks';
import type { Lane, LoginQueue, QueueLane, Workspace } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/field';
import { Kbd, Meter, PageHeader } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
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

function Progress({ queue }: { queue: LoginQueue }) {
  return (
    <Card className="mb-6 p-4">
      <div className="flex items-baseline justify-between gap-4">
        <p className="text-label">
          <span className="tabular">{queue.ready}</span> of <span className="tabular">{queue.total}</span> accounts ready
        </p>
        <p className="tabular text-secondary text-faint">
          {plural(queue.needs_login, 'login')} to go
          {queue.needs_attention ? `, ${queue.needs_attention} to look at` : ''}
        </p>
      </div>
      <Meter value={queue.ready} max={queue.total} label="Accounts ready" className="mt-2" />
      <ul className="mt-4 grid gap-x-6 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
        {queue.workspaces.map((workspace) => (
          <li key={workspace.id} className="flex items-center gap-2 text-secondary">
            <span className="min-w-0 flex-1 truncate text-muted">{workspace.name}</span>
            <Meter value={workspace.ready} max={Math.max(1, workspace.total)} label={`${workspace.name} accounts ready`} className="w-16 shrink-0" />
            <span className="mono tabular w-10 shrink-0 text-right text-caption text-faint">
              {workspace.ready}/{workspace.total}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function QueueRow({ item, current, workspace, onPick }: { item: QueueLane; current: boolean; workspace: string; onPick: () => void }) {
  return (
    <button
      type="button"
      onClick={onPick}
      aria-current={current || undefined}
      className={cn(
        'focus-inset flex w-full items-center gap-2 px-4 py-2 text-left transition-colors duration-(--duration-fast) ease-standard',
        current ? 'bg-pressed' : 'hover:bg-hover',
      )}
    >
      <span className="min-w-0 flex-1">
        <span className="block truncate text-body">{laneName(item.name)}</span>
        <span className="block truncate text-caption text-faint">{item.note ? `${workspace}: ${item.note}` : workspace}</span>
      </span>
      {item.readiness === 'needs_attention' ? <Status status={{ label: 'Look at this', tone: 'warn' }} className="text-caption" /> : null}
      <ChevronRight className="size-4 text-faint" />
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

  return (
    <div className="grid min-w-0 content-start gap-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-section-title">
          {laneName(item.name)} <span className="font-normal text-muted">for {workspace}</span>
        </h2>
        <p className="tabular text-secondary text-faint">{plural(left, 'account')} left</p>
      </div>
      {item.note ? <p className="text-body text-muted">{item.note}</p> : null}
      {/* The answer first: the sign-in is what this page is for, so it sits above the live view, not under it. */}
      <p className="text-secondary text-faint">
        The password is typed on this computer, into the account's own browser. openajar never sees it, and the computer keeps the session.
        {item.login_url ? (
          <>
            {' '}
            It opens at <span className="mono break-all">{item.login_url}</span>.
          </>
        ) : null}
      </p>
      <SignIn org={org} ws={item.workspace} name={item.name} saved={savedHere} canAct={canAct} />
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
            <Button type="submit" variant="primary" disabled={!note.trim()} loading={setReadiness.isPending}>
              Save and move on
            </Button>
          </form>
        ) : (
          <div className="flex flex-wrap items-center justify-end gap-2">
            <Button variant="ghost" className="mr-auto" onClick={() => setNoting(true)}>
              Something is wrong
            </Button>
            <Button icon={<SkipForward className="size-4" />} onClick={onNext}>
              Skip for now
              <Kbd aria-hidden="true" className="hidden border-transparent bg-transparent lg:inline-flex">
                S
              </Kbd>
            </Button>
            <Button variant="primary" icon={<Check className="size-4" />} loading={setReadiness.isPending} onClick={ready}>
              Logged in, next
              <Kbd aria-hidden="true" className="hidden border-transparent bg-transparent text-on-primary opacity-60 lg:inline-flex">
                R
              </Kbd>
            </Button>
          </div>
        )
      ) : (
        <p className="text-secondary text-faint">Your role can see this queue but not work through it.</p>
      )}
    </div>
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
          <div className="grid gap-6">
            <Skeleton className="h-28 rounded-xl" />
            <Skeleton className="h-96 rounded-xl" />
          </div>
        }
      >
        {(data) => (
          <>
            <Progress queue={data} />
            {data.lanes.length === 0 ? (
              <Card>
                <EmptyState
                  icon={<KeyRound />}
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
              <div className="grid items-start gap-6 lg:grid-cols-[280px_minmax(0,1fr)]">
                <Card className="max-h-[640px] overflow-y-auto max-lg:hidden">
                  <ul className="divide-y divide-line">
                    {data.lanes.map((item) => (
                      <li key={item.id}>
                        <QueueRow item={item} current={item.id === current?.id} workspace={nameOf(workspaces, item.workspace)} onPick={() => pick(item.id)} />
                      </li>
                    ))}
                  </ul>
                </Card>
                {current ? (
                  <Current
                    key={current.id}
                    org={org}
                    item={current}
                    workspace={nameOf(workspaces, current.workspace)}
                    left={data.lanes.length}
                    canAct={canAct}
                    onNext={next}
                  />
                ) : null}
              </div>
            )}
          </>
        )}
      </QueryView>
    </>
  );
}
