/**
 * Network and secrets (concept): the environment's network level on a three-step scale, what it lets
 * through in short, the hosts it refused (how often and when last), each with the exact command the owner
 * runs on the node to allow it, and the names of the app secrets bound to it. The web app changes none of
 * it: the node's settings are the owner's, at the node. No value of a secret is ever here; the machine
 * holds a placeholder that the node swaps for the real value on the way out, and only to its own host.
 * Once its machine stopped (state gone) the level scale goes: a level on a machine that is not there reads as a
 * control that does nothing. One quiet line says what it was; what was refused and the secrets' names stay.
 */
import { KeyRound, PowerOff, ShieldCheck, TriangleAlert } from 'lucide-react';
import { useState } from 'react';
import type { EnvBlockedHost, EnvNetwork, EnvNetworkLevel } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { CodeBlock, CopyButton } from '@/components/ui/misc';
import { cn } from '@/lib/cn';
import { dateTime, plural, timeAgo } from '@/lib/format';

const LEVELS: { level: EnvNetworkLevel; label: string }[] = [
  { level: 'full', label: 'Full' },
  { level: 'limited', label: 'Limited' },
  { level: 'none', label: 'None' },
];

/** What each level lets out, in one line. */
export const LEVEL_SENTENCE: Record<EnvNetworkLevel, string> = {
  full: 'It reaches any host on the internet.',
  limited: 'Only package registries, code hosts and the hosts you allowed.',
  none: 'Nothing leaves it but the git gateway and the hosts of its secrets.',
};

const BLOCKED_SHOWN = 4;

/** The exact command that allows one host. The web app shows it; the owner runs it on the node. */
export const allowCommand = (host: string) => `openajar environments allow --host ${host}`;

/** The line that stands in for the scale once the machine stopped. */
export const stoppedNetworkLine = (level: EnvNetworkLevel) =>
  `Its machine stopped, so no network setting does anything now. It was ${LEVELS.find((item) => item.level === level)?.label ?? level}.`;

export function NetworkCard({ network, now, stopped = false }: { network: EnvNetwork; now: number; stopped?: boolean }) {
  const [all, setAll] = useState(false);
  const blocked = [...network.blocked].sort((a, b) => b.last.localeCompare(a.last));
  const shown = all ? blocked : blocked.slice(0, BLOCKED_SHOWN);
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Network" meta={network.blocked.length ? plural(network.blocked.length, 'host') + ' refused' : undefined} />
      {stopped ? (
        <p className="flex items-center gap-2 px-5 py-4 text-secondary text-muted">
          <PowerOff aria-hidden="true" className="size-4 shrink-0 text-faint" />
          {stoppedNetworkLine(network.policy)}
        </p>
      ) : (
        <div className="grid gap-4 px-5 py-4">
          <LevelScale level={network.policy} />
          <Allowed network={network} />
        </div>
      )}
      {blocked.length ? (
        <section aria-label="Refused hosts" className="border-t border-line">
          <h3 className="px-5 pt-4 text-caption text-faint">Refused</h3>
          <ul className="divide-y divide-line">
            {shown.map((row) => (
              <BlockedRow key={row.host} row={row} now={now} />
            ))}
          </ul>
          {blocked.length > BLOCKED_SHOWN ? (
            <div className="px-3 pb-2">
              <Button variant="ghost" size="sm" aria-expanded={all} onClick={() => setAll((value) => !value)}>
                {all ? 'Show fewer' : `Show all ${blocked.length}`}
              </Button>
            </div>
          ) : null}
          <div className="grid gap-2 border-t border-line px-5 py-4">
            <p className="text-secondary text-muted">To let one through, run this on the node. It adds the host to the node's own list, which only you change.</p>
            <CodeBlock code={allowCommand(blocked[0]?.host ?? 'example.com')} label="Copy the command" />
          </div>
        </section>
      ) : network.policy !== 'full' && !stopped ? (
        <p className="flex items-center gap-2 border-t border-line px-5 py-3 text-secondary text-muted">
          <ShieldCheck aria-hidden="true" className="size-4 text-faint" />
          Nothing was refused.
        </p>
      ) : null}
      <Secrets names={network.secrets} hosts={network.allowed.secret_hosts ?? []} />
    </Card>
  );
}

/** Full, limited and none on one scale, the level it has marked: wider to the left. */
function LevelScale({ level }: { level: EnvNetworkLevel }) {
  return (
    <div className="grid gap-2">
      <div role="img" aria-label={`Network: ${level}`} className="grid grid-cols-3 gap-1 rounded-md bg-inset p-1">
        {LEVELS.map((item) => (
          <span
            key={item.level}
            className={cn(
              'flex h-7 items-center justify-center rounded-sm text-label',
              item.level === level ? 'bg-card text-fg shadow-lift' : 'text-faint',
            )}
          >
            {item.label}
          </span>
        ))}
      </div>
      <p className="text-secondary text-fg">{LEVEL_SENTENCE[level]}</p>
    </div>
  );
}

function Allowed({ network }: { network: EnvNetwork }) {
  if (network.policy === 'full') return null;
  const hosts = network.allowed.hosts ?? [];
  if (!hosts.length && !network.allowed.preset) return null;
  return (
    <dl className="grid gap-2 text-secondary">
      {network.allowed.preset ? (
        <div className="flex min-w-0 flex-wrap justify-between gap-x-3">
          <dt className="text-muted">Always</dt>
          <dd className="text-fg">{network.allowed.preset.charAt(0).toUpperCase() + network.allowed.preset.slice(1)}</dd>
        </div>
      ) : null}
      {hosts.length ? (
        <div className="grid gap-1">
          <dt className="text-muted">You allowed</dt>
          <dd className="flex flex-wrap gap-1">
            {hosts.map((host) => (
              <span key={host} className="max-w-full truncate rounded-full border border-line px-2 font-mono text-code-caption text-fg" title={host}>
                {host}
              </span>
            ))}
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

function BlockedRow({ row, now }: { row: EnvBlockedHost; now: number }) {
  const placeholder = row.why?.includes('placeholder');
  return (
    <li className="group grid gap-1 px-5 py-2">
      <div className="flex min-h-8 min-w-0 items-center gap-3">
        <span className="min-w-0 flex-1 truncate font-mono text-code text-fg" title={row.host}>
          {row.host}
        </span>
        <span className="shrink-0 font-mono text-code-caption text-muted tabular" title={`Refused ${plural(row.count, 'time')}`}>
          {row.count}×
        </span>
        <time dateTime={row.last} title={`Last ${dateTime(row.last)}`} className="w-16 shrink-0 text-right text-caption text-faint tabular">
          {timeAgo(row.last, now)}
        </time>
        <span className="shrink-0 opacity-100 transition-opacity duration-(--duration-fast) md:opacity-0 md:group-focus-within:opacity-100 md:group-hover:opacity-100">
          <CopyButton text={allowCommand(row.host)} label={`Copy the command that allows ${row.host}`} />
        </span>
      </div>
      {row.why ? (
        <p className={cn('flex items-start gap-2 text-caption', placeholder ? 'text-warn' : 'text-faint')}>
          {placeholder ? <TriangleAlert aria-hidden="true" className="mt-px size-3.5 shrink-0" /> : null}
          <span>{placeholder ? `A secret's placeholder went here. It was refused, so the real value never left the node.` : row.why}</span>
        </p>
      ) : null}
    </li>
  );
}

function Secrets({ names, hosts }: { names: string[]; hosts: string[] }) {
  if (!names.length) return null;
  return (
    <section aria-label="App secrets" className="grid gap-3 border-t border-line px-5 py-4">
      <h3 className="text-caption text-faint">App secrets</h3>
      <ul className="flex flex-wrap gap-2">
        {names.map((name) => (
          <li key={name} className="inline-flex max-w-full items-center gap-2 rounded-md border border-line bg-card px-2 py-1 shadow-lift">
            <KeyRound aria-hidden="true" className="size-3.5 shrink-0 text-faint" />
            <span className="truncate font-mono text-code-caption text-fg">{name}</span>
          </li>
        ))}
      </ul>
      <p className="text-caption text-faint">
        Names only. The machine holds a placeholder; the node puts the real value in on the way out
        {hosts.length ? <>, only to <span className="font-mono">{hosts.join(', ')}</span></> : ', only to the host each is bound to'}.
      </p>
    </section>
  );
}
