/**
 * What a run actually made, as it looks in the world: the email, the invoice, the post. Drawn with
 * our own components from what the computer reports, and always as text (DESIGN.md, Copy). The
 * foot says how we know: checked on the real site, or nobody checked.
 */
import { BadgeCheck, CircleDashed, FileText, Trash2 } from 'lucide-react';
import type { ReactNode } from 'react';
import type { RoutineOutput, RoutineRun } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { StateGlyph } from '@/components/ui/state-badge';
import { cn } from '@/lib/cn';
import { dateTime, monthDay } from '@/lib/format';
import { siteName } from '@/lib/sites';

const money = (amount: number, currency = 'EUR') => new Intl.NumberFormat('en', { style: 'currency', currency, maximumFractionDigits: amount % 1 ? 2 : 0 }).format(amount);

function Likeness({ output, site, lead }: { output: RoutineOutput; site: string; lead: boolean }) {
  switch (output.kind) {
    case 'post':
      return (
        <>
          {output.image ? <img src={output.image} alt="" className={cn('w-full object-cover', lead ? 'aspect-[2/1]' : 'aspect-[16/10]')} /> : null}
          <p className={cn('line-clamp-2 px-4 pt-3 text-pretty', lead ? 'text-16 font-medium' : 'text-13')}>{output.title}</p>
        </>
      );
    case 'payment':
    case 'order':
      return (
        <div className="px-4 pt-4">
          {output.amount !== undefined && output.amount !== null ? <p className="tabular text-32 font-semibold">{money(output.amount, output.currency ?? 'EUR')}</p> : null}
          <p className="mt-1 text-14 font-medium">{output.title}</p>
          <p className="line-clamp-2 text-13 text-muted">{[output.who, output.body].filter(Boolean).join(' · ')}</p>
        </div>
      );
    case 'form':
    case 'digest':
    case 'files':
      return (
        <div className="px-4 pt-4">
          {output.kind === 'files' ? <Trash2 aria-hidden="true" className="mb-2 size-4 text-faint" /> : <FileText aria-hidden="true" className="mb-2 size-4 text-faint" />}
          <p className="text-14 font-medium">{output.title}</p>
          <p className="mt-1 line-clamp-3 text-13 text-muted">{[output.who, output.body].filter(Boolean).join(' · ')}</p>
        </div>
      );
    case 'email':
    case 'invoice':
      return (
        <>
          {output.image ? <img src={output.image} alt="" className="aspect-[16/7] w-full object-cover" /> : null}
          <div className="px-4 pt-3">
            <p className="flex items-center gap-2 text-12 text-faint">
              <SiteMark site={site} size={14} label={false} />
              <span className="truncate">To {output.who}</span>
              {output.more ? <span className="shrink-0">{output.more}</span> : null}
            </p>
            <p className={cn('mt-2 font-medium text-pretty', lead ? 'text-20' : 'text-14')}>{output.title}</p>
            {output.body ? <p className={cn('mt-1 line-clamp-3 text-pretty text-muted', lead ? 'text-14' : 'text-13')}>{output.body}</p> : null}
          </div>
        </>
      );
  }
}

function Foot({ children }: { children: ReactNode }) {
  return <span className="mt-auto flex items-center gap-2 px-4 pt-3 pb-3 text-12 text-faint">{children}</span>;
}

export function OutputCard({ run, site, lead = false, onOpen }: { run: RoutineRun; site: string; lead?: boolean; onOpen: () => void }) {
  const failed = run.status === 'failed';
  const proof = run.output?.proof;
  return (
    <button
      type="button"
      onClick={onOpen}
      className={cn(
        'flex h-full min-h-48 w-full flex-col overflow-hidden rounded-(--radius-card) border bg-bg text-left shadow-(--shadow-card)',
        'transition-[box-shadow,border-color,transform] duration-(--duration-base) ease-standard hover:-translate-y-px hover:shadow-(--shadow-hover) motion-reduce:hover:translate-y-0',
        failed ? 'border-(--stuck-line)' : 'border-line hover:border-line-strong',
      )}
    >
      {failed ? (
        <div className="px-4 pt-4">
          <StateGlyph state="stuck" size={16} />
          <p className="mt-2 text-14 font-medium">It did not finish</p>
          <p className="mt-1 line-clamp-4 text-13 text-pretty text-muted">{run.result}</p>
        </div>
      ) : run.output ? (
        <Likeness output={run.output} site={site} lead={lead} />
      ) : (
        <p className="px-4 pt-4 text-14">{run.result}</p>
      )}
      <Foot>
        {failed ? null : proof ? <BadgeCheck aria-hidden="true" className="size-3 shrink-0 text-(--ok-dot)" /> : <CircleDashed aria-hidden="true" className="size-3 shrink-0" />}
        <span className="truncate">
          {lead ? dateTime(run.started_at) : monthDay(run.started_at)}
          {failed ? '' : proof ? ` · Checked on ${siteName(proof.site)}` : ' · Not checked'}
        </span>
      </Foot>
    </button>
  );
}
