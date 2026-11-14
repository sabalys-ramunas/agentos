/** The small marks: sparklines, dot strips, run strips, segmented bars and the range switch. */
import { RadioGroup } from 'radix-ui';
import { useState } from 'react';
import type { Lane, RunStatus } from '@/api/types';
import { cn } from '@/lib/cn';
import { timeAgo } from '@/lib/format';
import { runStatus } from '@/lib/labels';
import type { Tone } from '@/lib/labels';
import { laneStatus } from '@/lib/lanes';
import { StatusDot } from '../ui/status';
import { Tooltip } from '../ui/menu';

/** A shape, not a measurement. The number it stands for is always printed beside it. */
export function Sparkline({ values, className }: { values: number[]; className?: string }) {
  const max = Math.max(1, ...values);
  const width = 96;
  const height = 24;
  const step = width / Math.max(1, values.length - 1);
  const line = values.map((value, index) => `${index ? 'L' : 'M'}${(index * step).toFixed(1)} ${(height - 2 - (value / max) * (height - 4)).toFixed(1)}`).join(' ');
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true" className={cn('h-6 w-24', className)}>
      <path d={`${line} L${width} ${height} L0 ${height} Z`} fill="var(--foreground)" fillOpacity={0.06} />
      <path d={line} fill="none" stroke="var(--foreground-subtle)" strokeWidth={1.5} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/** Every lane in a workspace as one dot: 29 lanes at a glance, without a flat list. */
export function HealthDots({ lanes, className }: { lanes: Lane[]; className?: string }) {
  const counts = new Map<string, number>();
  for (const lane of lanes) {
    const { label } = laneStatus(lane);
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  const summary = [...counts].map(([label, count]) => `${count} ${label.toLowerCase()}`).join(', ');
  // What needs a person sorts first, so trouble is at the same place in every row.
  const order: Record<Tone, number> = { bad: 0, warn: 1, live: 2, info: 2, ok: 3, neutral: 4, idle: 5 };
  const sorted = [...lanes].sort((a, b) => order[laneStatus(a).tone] - order[laneStatus(b).tone]);
  return (
    <Tooltip label={summary}>
      <span role="img" aria-label={summary} className={cn('inline-flex items-center gap-1', className)}>
        {sorted.map((lane) => (
          <StatusDot key={lane.id} tone={laneStatus(lane).tone} className="size-1.5" />
        ))}
      </span>
    </Tooltip>
  );
}

const RUN_COLORS: Partial<Record<RunStatus, string>> = {
  succeeded_verified: 'var(--mark-verified)',
  succeeded_unverified: 'var(--mark-unverified)',
  partial: 'var(--mark-partial)',
  failed: 'var(--mark-failed)',
};

interface RunMark {
  id: string;
  status: RunStatus;
  at: string;
}

/** Share of finished runs that were confirmed from outside. Unverified does not count. */
export function verifiedShare(runs: RunMark[]): number | null {
  const finished = runs.filter((run) => run.status in RUN_COLORS);
  if (finished.length === 0) return null;
  return Math.round((finished.filter((run) => run.status === 'succeeded_verified').length / finished.length) * 100);
}

/** The last runs of a flow, oldest to newest, each as one honest tick. */
export function RunStrip({ runs, className }: { runs: RunMark[]; className?: string }) {
  const [hover, setHover] = useState<RunMark | null>(null);
  const ordered = [...runs].reverse();
  const share = verifiedShare(runs);
  const name = `${runs.length} recent runs${share === null ? '' : `, ${share}% verified`}`;
  return (
    <span className={cn('relative inline-flex items-center gap-2', className)}>
      <span role="img" aria-label={name} className="inline-flex h-4 items-end gap-px" onPointerLeave={() => setHover(null)}>
        {ordered.map((run) => (
          <span
            key={run.id}
            className="block h-4 w-1 rounded-[1px] transition-opacity duration-(--duration-fast) ease-standard"
            style={{ backgroundColor: RUN_COLORS[run.status] ?? 'var(--fill-3)', opacity: hover && hover.id !== run.id ? 0.4 : 1 }}
            onPointerEnter={() => setHover(run)}
          />
        ))}
      </span>
      <span className="tabular w-16 text-12 text-faint">
        {hover ? `${runStatus[hover.status].label}` : share === null ? 'No runs' : `${share}% verified`}
      </span>
      {hover ? <span className="sr-only">{timeAgo(hover.at)}</span> : null}
    </span>
  );
}

/** One whole, split into parts. The figures live in the text beside it. */
export function SegmentBar({ parts, label, className }: { parts: { value: number; color: string; label: string }[]; label: string; className?: string }) {
  const total = parts.reduce((sum, part) => sum + part.value, 0);
  return (
    <div
      role="img"
      aria-label={`${label}: ${parts.map((part) => `${part.value} ${part.label.toLowerCase()}`).join(', ')}`}
      // The 2 px gap between marks is a chart rule, so it is set here and not from the spacing grid.
      style={{ gap: 2 }}
      className={cn('flex h-1 w-full overflow-hidden rounded-full', total === 0 && 'bg-fill-2', className)}
    >
      {parts
        .filter((part) => part.value > 0)
        .map((part) => (
          <span key={part.label} className="block h-full" style={{ width: `${(part.value / total) * 100}%`, backgroundColor: part.color }} />
        ))}
    </div>
  );
}

interface SegmentedProps<T extends string> {
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: string }[];
  label: string;
  className?: string;
}

/** A range switch. On a phone it is 32px tall with no outline, so each choice is a 32px target; from md it is the 28px outlined one.
 * Date range comes first in a filter row, and it scopes everything below. */
export function Segmented<T extends string>({ value, onChange, options, label, className }: SegmentedProps<T>) {
  return (
    <RadioGroup.Root
      aria-label={label}
      value={value}
      onValueChange={(next) => onChange(next as T)}
      className={cn('scrollbar-none inline-flex h-8 max-w-full shrink-0 items-center overflow-x-auto rounded-md bg-fill-1 p-0 md:h-7 md:border md:border-line md:bg-transparent md:p-px', className)}
    >
      {options.map((option) => (
        <RadioGroup.Item
          key={option.value}
          value={option.value}
          className={cn(
            'tabular h-full shrink-0 rounded-sm px-2 text-13 whitespace-nowrap text-faint transition-colors duration-(--duration-fast) ease-standard hover:text-fg',
            'data-[state=checked]:bg-fill-2 data-[state=checked]:text-fg',
          )}
        >
          {option.label}
        </RadioGroup.Item>
      ))}
    </RadioGroup.Root>
  );
}
