/**
 * The previews of the developer kinds on Needs you (Mono): a git push, a disposable VM and a command in a
 * lane. Each is drawn from the request's own preview data and nothing else. What the node does not know
 * is not drawn: a push has no line counts, and a VM's deletion time is known only once it is rented.
 */
import type { ReactNode } from 'react';
import type { Approval, PreviewData } from '@/api/types';
import { BranchPill } from '@/components/ui/branch';
import { Terminal } from '@/components/ui/terminal';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { durationWords, money, repoName } from './kinds';

const frame = 'w-full max-w-[640px] overflow-hidden rounded-lg border border-line bg-card text-fg shadow-lift';

/** One fact as a label over its value. */
function Spec({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid min-w-0 gap-1 px-4 py-3">
      <dt className="text-caption text-faint">{label}</dt>
      <dd className="min-w-0 truncate text-body">{children}</dd>
    </div>
  );
}

export function PushPreview({ approval }: { approval: Approval }) {
  const d: PreviewData = approval.preview.data ?? {};
  const refs = d.push_refs ?? [];
  return (
    <article aria-label="Push preview" className={frame}>
      <div className="flex min-w-0 flex-wrap items-center gap-2 border-b border-line px-4 py-3">
        <span className="text-secondary text-muted">Into</span>
        <span className="truncate font-mono text-code" title={d.remote}>
          {d.remote ? repoName(d.remote) : 'the remote'}
        </span>
      </div>
      {refs.length === 0 ? (
        <p className="px-4 py-4 text-body break-words text-muted">{approval.summary}</p>
      ) : (
        <ul className="divide-y divide-line">
          {refs.map((ref) => (
            <li key={ref.ref} className="grid gap-3 px-4 py-4">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <BranchPill name={ref.name} main={ref.protected} size="lg" />
                <span className="text-secondary text-muted">
                  {ref.kind === 'delete' ? 'is deleted' : ref.kind === 'create' ? 'is created' : 'is updated'}
                  {ref.commits ? `, ${plural(ref.commits, 'commit')}` : ''}
                </span>
                {ref.protected ? <span className="text-secondary text-muted">· protected</span> : null}
              </div>
              {ref.force ? (
                <p role="note" className="rounded-md border border-bad-line bg-bad-fill px-3 py-2 text-secondary text-bad">
                  A force-push: commits on the remote that are not in this push are lost.
                </p>
              ) : null}
              {ref.log.length ? (
                <ol aria-label={`Commits that would land on ${ref.name}`} className="grid gap-1">
                  {ref.log.map((line, index) => {
                    const [hash = '', ...words] = line.split(' ');
                    const isHash = /^[0-9a-f]{6,40}$/i.test(hash);
                    return (
                      <li key={`${line}-${index}`} className="flex min-w-0 items-baseline gap-3">
                        <span aria-hidden="true" className="size-2 shrink-0 rounded-full bg-line-strong" />
                        {isHash ? <span className="shrink-0 font-mono text-code-caption text-faint">{hash}</span> : null}
                        <span className="min-w-0 truncate text-body" title={isHash ? words.join(' ') : line}>
                          {isHash ? words.join(' ') : line}
                        </span>
                      </li>
                    );
                  })}
                </ol>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      <p className="border-t border-line px-4 py-2 text-caption text-faint">The lines each commit changes are not known before the push.</p>
    </article>
  );
}

/** A big number and what it means. */
function Figure({ value, label }: { value: string; label: string }) {
  return (
    <div className="grid min-w-0 content-start gap-1 px-4 py-4">
      <span className="font-mono text-stat tabular">{value}</span>
      <span className="text-secondary text-muted">{label}</span>
    </div>
  );
}

export function VmPreview({ approval }: { approval: Approval }) {
  const d: PreviewData = approval.preview.data ?? {};
  const hourly = d.price_hourly_eur != null ? money(d.price_hourly_eur, 'EUR') : null;
  const max = d.max_cost_eur != null ? money(d.max_cost_eur, 'EUR') : null;
  const limit = d.ttl_minutes ? durationWords(d.ttl_minutes) : null;
  return (
    <article aria-label="Disposable VM preview" className={frame}>
      <div className="grid divide-line max-sm:divide-y sm:grid-cols-3 sm:divide-x">
        <Figure value={hourly ?? '–'} label={hourly ? 'an hour, billed by the minute' : 'the price is not known'} />
        <Figure value={max ?? '–'} label={max ? `at most${limit ? ` for ${limit}` : ''}` : 'no most is known'} />
        <Figure value={limit ?? '–'} label={limit ? 'then it deletes itself, even if the agent forgets' : 'no time limit is set'} />
      </div>
      <dl className="grid border-t border-line sm:grid-cols-3">
        <Spec label="Size">
          <span className="font-mono text-code">{d.server_type ?? '–'}</span>
        </Spec>
        <Spec label="Profile">{d.vm_profile ?? '–'}</Spec>
        <Spec label="Lane">
          <span className="font-mono text-code">{d.computer ?? approval.lane}</span>
        </Spec>
      </dl>
    </article>
  );
}

export function LanePreview({ approval }: { approval: Approval }) {
  const d: PreviewData = approval.preview.data ?? {};
  const lane = d.computer ?? approval.lane;
  const specs: [string, ReactNode][] = [
    ['Lane', <span key="lane" className="font-mono text-code">{lane}</span>],
    ...(d.lane_preset ? ([['Kind', d.lane_preset]] as [string, ReactNode][]) : []),
    ...(d.fields ?? []).map((field) => [field.label, field.value] as [string, ReactNode]),
    ...(d.timeout_s ? ([['Time limit', durationWords(Math.round(d.timeout_s / 60))]] as [string, ReactNode][]) : []),
  ];
  const head = [d.sync_dest ? `syncs into ${d.sync_dest} first${d.synced_files != null ? `, ${plural(d.synced_files, 'file')}` : ''}` : null].filter(Boolean) as string[];
  return (
    <article aria-label="Lane preview" className="grid w-full max-w-[640px] gap-3">
      <dl className={cn(frame, 'grid grid-cols-2 sm:grid-cols-3')}>
        {specs.map(([label, value]) => (
          <Spec key={label} label={label}>
            {value}
          </Spec>
        ))}
      </dl>
      {d.command ? (
        <Terminal
          label={`The command it runs in ${lane}`}
          command={d.command}
          lines={[...head.map((line) => `\u001b[2m${line}\u001b[0m`), ...(d.cwd ? [`\u001b[2min ${d.cwd}\u001b[0m`] : [])]}
          numbered={false}
          controls={false}
          heightClass="max-h-40"
        />
      ) : null}
    </article>
  );
}
