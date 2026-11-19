/** The small mark: the sparkline. (The range switch is SegmentedControl, components/ui/tabs.tsx.) */
import { cn } from '@/lib/cn';

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
