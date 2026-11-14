/** The small marks: the sparkline and the range switch. */
import { RadioGroup } from 'radix-ui';
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
