/**
 * The shell's one answer to "is the system OK?" (structure.md section 6). It replaces three
 * machine chips (the brain, the cost, the node count) with a sentence: "1 working now · all
 * systems fine", "Mac mini (home) is offline", "Reconnecting". The depth is one click away.
 */
import { Link } from '@tanstack/react-router';
import { Cpu, Server, Snowflake, Sparkles } from 'lucide-react';
import { useAccess, useBrains, useFreezeStatus, useNodeHealth, useNodes, useStatus, useWorkspaces } from '@/api/hooks';
import { useLiveConnection } from '@/api/live';
import type { Brain, Status as OrgStatus, Workspace } from '@/api/types';
import { cn } from '@/lib/cn';
import { aiName, computerName } from '@/lib/computers';
import { compact, usd } from '@/lib/format';
import { computerFreeze } from '@/lib/freeze';
import { isOrgWide } from '@/lib/permissions';
import { useTechnical } from '@/lib/technical';
import { NODE_STATE_WORDS, PLACES } from '@/lib/vocabulary';
import { useNeedsYouTotal } from '@/features/needs-you/count';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/menu';
import { Skeleton } from '../ui/states';
import { fleetOf, fleetWords } from './counts';
import { useFreezeTarget } from './freeze-control';
import { openFreeze } from './palette-store';

export type PillTone = 'working' | 'ok' | 'warn' | 'bad' | 'quiet';

export interface SystemSummary {
  tone: PillTone;
  /** The whole sentence, for the pill on a wide screen. */
  text: string;
  /** The first words, for a phone. */
  short: string;
}

/** How many accounts an agent or routine is on right now. */
export function workingNow(workspaces: Workspace[]): number {
  return workspaces.reduce(
    (total, workspace) => total + (workspace.counts ? workspace.counts.working : workspace.lanes.filter((lane) => lane.state === 'active').length),
    0,
  );
}

/**
 * What the computers say about themselves beyond up and down: a freeze, and their own health checks
 * (the same findings Computers' Health tab lists). Unknown is left out, never read as fine.
 */
export interface ComputerHealth {
  /** A whole computer is frozen: no agent acts anywhere on it. */
  frozen: boolean;
  /** Checks that failed and need fixing. */
  bad: number;
  /** Checks that are worth a look. */
  warn: number;
}

/**
 * Worst first: the connection, a computer offline, a freeze, the AI, a slow or unwell computer, then work or
 * calm. `health` is undefined when nobody has read it yet (or the person may not): then nothing is said about it.
 */
export function summarize(status: OrgStatus, brains: Brain[], working: number, reconnecting: boolean, health?: ComputerHealth): SystemSummary {
  if (reconnecting) return { tone: 'quiet', text: 'Reconnecting', short: 'Reconnecting' };
  const nodes = status.nodes;
  const down = nodes.filter((node) => node.state === 'down');
  if (down.length === 1) {
    const name = computerName(down[0]!.id);
    return { tone: 'bad', text: `${name} is offline`, short: 'Offline' };
  }
  if (down.length > 1) return { tone: 'bad', text: `${down.length} machines are offline`, short: 'Offline' };
  if (nodes.length === 0) return { tone: 'quiet', text: 'No machine yet', short: 'No machine' };
  if (health?.frozen) {
    return {
      tone: 'bad',
      text: nodes.length === 1 ? `${computerName(nodes[0]!.id)} is frozen · no agent acts` : 'A machine is frozen · no agent acts there',
      short: 'Frozen',
    };
  }

  const preferred = brains[0];
  const ready = brains.filter((brain) => brain.state === 'ready');
  if (brains.length && ready.length === 0) return { tone: 'warn', text: 'AI is not available', short: 'AI is off' };
  if (preferred && preferred.state !== 'ready') return { tone: 'warn', text: 'Using backup AI', short: 'Backup AI' };

  const slow = nodes.filter((node) => node.state === 'degraded' || node.degraded);
  if (slow.length) {
    return {
      tone: 'warn',
      text: slow.length === 1 ? `${computerName(slow[0]!.id)} is running slowly` : `${slow.length} machines are running slowly`,
      short: 'Running slowly',
    };
  }
  // A machine whose own checks fail is never "fine": the pill says what Health says.
  const unwell = health && (health.bad || health.warn) ? (health.bad ? 'needs fixing' : 'is worth a look') : null;
  const computer = nodes.length === 1 ? computerName(nodes[0]!.id) : 'A machine';
  const inSentence = nodes.length === 1 ? computer : 'a machine';
  // "Fine" is about machines and the AI, so it never sits unqualified beside things that wait.
  const needs = status.needs_you ?? 0;
  if (needs > 0) {
    const doing = working > 0 ? ` · ${working} working now` : '';
    return {
      tone: health?.bad ? 'bad' : 'warn',
      text: `${needs} need you${doing} · ${unwell ? `${inSentence} ${unwell}` : 'machines fine'}`,
      short: `${needs} need you`,
    };
  }
  if (unwell) return { tone: health?.bad ? 'bad' : 'warn', text: `${computer} ${unwell}`, short: health?.bad ? 'Needs fixing' : 'Worth a look' };
  if (working > 0) return { tone: 'working', text: `${working} working now · all systems fine`, short: `${working} working` };
  return { tone: 'ok', text: 'All systems fine', short: 'All fine' };
}

/**
 * The freeze and the health round of the one computer a node edition runs on, as Health shows them. Health is
 * read for the one computer only (a cloud with several computers says nothing about their health here).
 */
function useComputerHealth(org: string, node: string | null, owner: boolean): ComputerHealth | undefined {
  const freeze = useFreezeStatus(org).data;
  const checks = useNodeHealth(org, node ?? '', node !== null && owner).data;
  const frozen = Boolean(computerFreeze(freeze)) || Boolean(freeze?.node_frozen);
  if (frozen) return { frozen, bad: 0, warn: 0 };
  if (!checks) return undefined;
  return {
    frozen: false,
    bad: checks.filter((check) => check.status === 'fail').length,
    warn: checks.filter((check) => check.status === 'warn').length,
  };
}

const DOT: Record<PillTone, string> = {
  working: 'bg-live',
  ok: 'bg-(--ok-dot)',
  warn: 'bg-(--wait-dot)',
  bad: 'bg-(--stuck-dot)',
  quiet: 'border border-faint',
};

export function PillDot({ tone, className }: { tone: PillTone; className?: string }) {
  return (
    <span aria-hidden="true" className={cn('relative inline-flex size-2 shrink-0', className)}>
      {tone === 'working' ? <span className="absolute inset-0 animate-ping rounded-full bg-live opacity-60 motion-reduce:hidden" /> : null}
      <span className={cn('relative inline-block size-2 rounded-full', DOT[tone])} />
    </span>
  );
}

function Row({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-3 px-4 py-3">
      <span className="mt-1 text-faint [&_svg]:size-4">{icon}</span>
      <div className="min-w-0 flex-1">
        <p className="text-secondary text-faint">{title}</p>
        <div className="mt-1 text-body text-fg">{children}</div>
      </div>
    </div>
  );
}

const NODE_DOT = { up: 'ok', degraded: 'warn', down: 'bad' } as const;

/**
 * The system's state, one click from anywhere. On a wide screen it is the status line at the foot of
 * the rail (`variant="line"`): the machines' load as bars and "3 online · 1 not answering". On a
 * phone it is a dot in the top bar (`variant="dot"`). Both open the same popover: the worst thing
 * first as a sentence, then the machines, the AI and what it costs today.
 */
export function StatusPill({ org, variant = 'line' }: { org: string; variant?: 'line' | 'dot' }) {
  const status = useStatus(org).data;
  const brains = useBrains(org).data ?? [];
  const workspaces = useWorkspaces(org).data ?? [];
  const nodes = useNodes(org).data;
  const connection = useLiveConnection();
  const [technical] = useTechnical();
  const { role } = useAccess(org);
  const health = useComputerHealth(org, status?.nodes.length === 1 ? status.nodes[0]!.id : null, role === 'owner');
  // The same count as Needs you and the rail, so the line never says a different number.
  const needs = useNeedsYouTotal(org, status);
  const freeze = useFreezeTarget(org);

  if (!status) {
    return variant === 'line' ? <Skeleton className="mx-2 h-4 w-32" /> : <Skeleton className="size-8 rounded-full" />;
  }
  const working = workingNow(workspaces);
  const summary = summarize({ ...status, needs_you: needs }, brains, working, connection === 'reconnecting', health);
  const fleet = fleetOf(status)!;
  const brain = status.brain;
  const current = brain ? brains.find((item) => item.name === brain.name) : undefined;
  const local = brains.find((item) => item.kind === 'local');
  const list = nodes ?? status.nodes.map((node) => ({ ...node, last_seen: '' }));
  const label = `System: ${summary.text}. Machines: ${fleetWords(fleet)}`;

  return (
    <Popover>
      {variant === 'line' ? (
        <PopoverTrigger
          aria-label={label}
          className={cn(
            'flex h-9 w-full min-w-0 items-center justify-center gap-2 rounded-md px-2 text-caption text-muted lg:justify-start',
            'transition-colors duration-(--duration-fast) ease-standard hover:bg-hover hover:text-fg data-[state=open]:bg-pressed data-[state=open]:text-fg',
          )}
        >
          <FleetBars status={status} />
          <span data-rail-label className="hidden min-w-0 flex-1 truncate text-left lg:block">
            {fleetWords(fleet)}
          </span>
        </PopoverTrigger>
      ) : (
        <PopoverTrigger
          aria-label={label}
          className="inline-flex size-10 shrink-0 items-center justify-center rounded-md hover:bg-hover data-[state=open]:bg-pressed"
        >
          <PillDot tone={summary.tone} />
        </PopoverTrigger>
      )}
      <PopoverContent side={variant === 'line' ? 'right' : 'bottom'} align={variant === 'line' ? 'end' : 'end'} className="w-[22rem] max-w-[calc(100vw-16px)] p-0">
        <div className="flex items-center gap-2 border-b border-line px-4 py-3">
          <PillDot tone={summary.tone} />
          <p className="text-body font-medium">{summary.text}</p>
        </div>
        <Row icon={<Server />} title={PLACES.machines.word}>
          {list.length ? (
            <ul className="grid gap-1">
              {list.map((node) => (
                <li key={node.id} className="flex items-center gap-2">
                  <PillDot tone={NODE_DOT[node.state]} />
                  <span className="min-w-0 flex-1 truncate">{computerName(node.id)}</span>
                  {technical ? <span className="mono text-caption text-faint">{node.id}</span> : null}
                  <span className="text-secondary text-muted">{node.state === 'down' ? 'Not answering' : NODE_STATE_WORDS[node.state].word}</span>
                </li>
              ))}
            </ul>
          ) : (
            <span className="text-muted">No machine yet. Add one to start working.</span>
          )}
        </Row>
        {brain?.name ? (
          <Row icon={<Sparkles />} title="AI">
            <p>
              {aiName(brain.name, current?.kind)} {brain.state === 'ready' ? 'is ready.' : 'is not ready.'}
              {brain.offline_ready && local && local.name !== brain.name ? (
                <span className="text-muted"> If the internet drops, agents keep working with the local AI.</span>
              ) : brain.offline_ready ? (
                <span className="text-muted"> If the internet drops, agents keep working.</span>
              ) : null}
            </p>
            {technical ? (
              <p className="mono mt-1 text-caption text-faint">
                {brains.map((item) => `${item.name} ${item.state}`).join(' · ')}
              </p>
            ) : null}
          </Row>
        ) : null}
        {isOrgWide(role) ? (
          <Row icon={<Cpu />} title="AI usage today">
            <p className="tabular">
              {usd(status.cost_today.usd)}
              {technical ? <span className="text-muted"> · {compact(status.cost_today.tokens)} tokens</span> : null}
            </p>
            <p className="text-caption text-faint">Only what the AI models cost. agentos never charges per task.</p>
          </Row>
        ) : null}
        <div className="flex flex-wrap items-center gap-1 border-t border-line p-2">
          <Link to="/$org/nodes" params={{ org }} className="flex h-8 items-center rounded-md px-2 text-secondary text-muted hover:bg-hover hover:text-fg">
            Open {PLACES.machines.word}
          </Link>
          <Link to="/$org/settings/brains" params={{ org }} className="flex h-8 items-center rounded-md px-2 text-secondary text-muted hover:bg-hover hover:text-fg">
            AI models
          </Link>
          {freeze.canFreeze ? (
            <button type="button" onClick={openFreeze} className="ml-auto flex h-8 items-center gap-1 rounded-md px-2 text-secondary text-muted hover:bg-hover hover:text-fg">
              <Snowflake className="size-3" aria-hidden="true" />
              Freeze {freeze.name}
            </button>
          ) : null}
        </div>
      </PopoverContent>
    </Popover>
  );
}

const BAR_TONE: Record<'ok' | 'warn' | 'bad', string> = {
  ok: 'bg-faint',
  warn: 'bg-warn-dot',
  bad: 'bg-line-strong',
};

/** One bar a machine, up to eight: tall when answering, amber when slow, low and pale when not answering. */
function FleetBars({ status }: { status: OrgStatus }) {
  const nodes = status.nodes.slice(0, 8);
  if (!nodes.length) return <span aria-hidden="true" className="inline-flex h-4 w-1 shrink-0" />;
  return (
    <span aria-hidden="true" className="inline-flex h-4 shrink-0 items-end gap-px">
      {nodes.map((node) => {
        const tone = node.state === 'down' ? 'bad' : node.state === 'degraded' || node.degraded ? 'warn' : 'ok';
        return <i key={node.id} className={cn('block w-1', tone === 'bad' ? 'h-1' : tone === 'warn' ? 'h-4' : 'h-3', BAR_TONE[tone])} />;
      })}
    </span>
  );
}
