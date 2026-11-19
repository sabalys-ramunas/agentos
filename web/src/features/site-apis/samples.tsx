/**
 * The job itself, small. Each Site API shows its sample in the shape of what it gives you: orders as
 * rows, money owed as how late it is, stock as what is left against the reorder point, a parcel as
 * its journey, comments as what people said. Values come from the last good read and are shown as
 * text. That difference is what stops the list reading as a stack of equal boxes.
 */
import type { SiteApi, SiteSample } from '@/api/types';
import { cn } from '@/lib/cn';
import { hueOf } from './page-sheet';

function Rows({ sample, hued }: { sample: Extract<SiteSample, { kind: 'rows' }>; hued?: boolean }) {
  return (
    <table className="w-full table-fixed text-secondary">
      <thead>
        <tr>
          {sample.columns.map((column, index) => (
            <th key={column} scope="col" className={cn('truncate pb-1 text-left text-caption font-normal text-faint', index === 1 && sample.columns.length > 2 && 'w-2/5')}>
              {hued ? <span aria-hidden="true" className="mr-2 inline-block size-2 rounded-full" style={{ backgroundColor: hueOf(index) }} /> : null}
              {column}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {sample.rows.map((row, rowIndex) => (
          <tr key={rowIndex} className="border-t border-line">
            {row.map((cell, index) => (
              <td key={index} className={cn('tabular truncate py-1 pr-2', index === 0 ? 'font-medium' : 'text-muted')}>
                {cell}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Aging({ sample }: { sample: Extract<SiteSample, { kind: 'aging' }> }) {
  const most = Math.max(...sample.items.map((item) => item.days));
  return (
    <div className="grid gap-3">
      <p className="flex items-baseline gap-2">
        <span className="text-page-title-compact">{sample.total}</span>
        <span className="text-secondary text-muted">{sample.note}</span>
      </p>
      <ul className="grid gap-2">
        {sample.items.map((item) => (
          <li key={item.who} className="grid grid-cols-[minmax(0,1fr)_72px_minmax(0,1fr)_32px] items-center gap-2 text-secondary">
            <span className="truncate font-medium">{item.who}</span>
            <span className="tabular text-right text-muted">{item.amount}</span>
            <span aria-hidden="true" className="h-1 overflow-hidden rounded-full bg-pressed">
              <span className="block h-full rounded-full bg-fg" style={{ width: `${(item.days / most) * 100}%` }} />
            </span>
            <span className="tabular text-right text-caption text-faint">{item.days}d</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Levels({ sample }: { sample: Extract<SiteSample, { kind: 'levels' }> }) {
  return (
    <ul className="grid gap-2">
      {sample.items.map((item) => (
        <li key={item.label} className="grid grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)_56px] items-center gap-3 text-secondary">
          <span className="truncate font-medium">{item.label}</span>
          <span aria-hidden="true" className="relative h-1 rounded-full bg-pressed">
            <span className="absolute inset-y-0 left-0 rounded-full bg-fg" style={{ width: `${Math.min(100, (item.left / item.reorder) * 100)}%` }} />
            <span className="absolute -top-1 right-0 h-3 w-px bg-line-strong" />
          </span>
          <span className="tabular text-right text-muted">
            {item.left} {item.unit}
          </span>
        </li>
      ))}
      <li className="text-caption text-faint">The mark at the end of each line is its reorder point.</li>
    </ul>
  );
}

function Journey({ sample }: { sample: Extract<SiteSample, { kind: 'journey' }> }) {
  return (
    <div>
      <p className="flex items-baseline justify-between gap-2 text-secondary">
        <span className="mono font-medium">{sample.parcel}</span>
        <span className="text-caption text-faint">{sample.note}</span>
      </p>
      <ol className="mt-3 grid" style={{ gridTemplateColumns: `repeat(${sample.stops.length}, minmax(0, 1fr))` }}>
        {sample.stops.map((stop, index) => (
          <li key={stop} className="relative grid justify-items-center gap-2 text-center">
            {index > 0 ? <span aria-hidden="true" className={cn('absolute top-1 right-1/2 h-px w-full', index <= sample.at ? 'bg-fg' : 'bg-line-strong')} /> : null}
            <span aria-hidden="true" className={cn('relative size-2 rounded-full', index <= sample.at ? 'bg-fg' : 'border border-line-strong bg-inset')} />
            <span className={cn('text-caption', index === sample.at ? 'font-medium text-fg' : 'text-faint')}>{stop}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function Quotes({ sample }: { sample: Extract<SiteSample, { kind: 'quotes' }> }) {
  return (
    <ul className="grid gap-2">
      {sample.items.map((item) => (
        <li key={item.who + item.when} className="border-l border-line-strong pl-3">
          <p className="truncate text-secondary">{item.text}</p>
          <p className="text-caption text-faint">
            {item.who} · {item.when}
          </p>
        </li>
      ))}
    </ul>
  );
}

export function Sample({ sample, hued }: { sample: SiteSample; hued?: boolean }) {
  switch (sample.kind) {
    case 'rows':
      return <Rows sample={sample} hued={hued} />;
    case 'aging':
      return <Aging sample={sample} />;
    case 'levels':
      return <Levels sample={sample} />;
    case 'journey':
      return <Journey sample={sample} />;
    case 'quotes':
      return <Quotes sample={sample} />;
  }
}

/** One line of a sample, for a row that has no room for the whole thing. */
export function sampleLine(sample: SiteSample): string {
  switch (sample.kind) {
    case 'rows':
      return (sample.rows[0] ?? []).join(' · ');
    case 'aging':
      return `${sample.total} · ${sample.note}`;
    case 'levels':
      return sample.items.slice(0, 2).map((item) => `${item.label}: ${item.left}`).join(' · ');
    case 'journey':
      return `${sample.parcel} · ${sample.stops[sample.at] ?? ''}`;
    case 'quotes':
      return `"${sample.items[0]?.text ?? ''}"`;
  }
}

/**
 * A week of calls, four bars a day: the cache in grey, fresh reads in blue, errors in red. A shape to
 * recognise, not to measure: the number beside it says how many.
 */
export function Traffic({ spark, className }: { spark: SiteApi['spark']; className?: string }) {
  const top = Math.max(1, ...spark.map((bucket) => bucket.cached + bucket.fresh + bucket.errors));
  const height = 28;
  const scale = (value: number) => (value / top) * height;
  const slot = 3;
  return (
    <svg viewBox={`0 0 ${spark.length * slot} ${height}`} preserveAspectRatio="none" aria-hidden="true" className={cn('h-7 w-28', className)}>
      {spark.map((bucket, index) => {
        const x = index * slot;
        const cached = scale(bucket.cached);
        const fresh = scale(bucket.fresh);
        const errors = scale(bucket.errors);
        const rest = bucket.cached + bucket.fresh + bucket.errors === 0;
        return (
          <g key={index}>
            {rest ? <rect x={x} y={height - 1} width={2} height={1} fill="var(--border-strong)" /> : null}
            <rect x={x} y={height - cached} width={2} height={cached} fill="var(--mark-cached)" />
            <rect x={x} y={height - cached - fresh} width={2} height={fresh} fill="var(--mark-fresh)" />
            <rect x={x} y={height - cached - fresh - errors} width={2} height={errors} fill="var(--mark-failed)" />
          </g>
        );
      })}
    </svg>
  );
}
