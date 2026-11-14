import { cn } from '@/lib/cn';
import { dayLabel, pickableDays } from './words';

/** The days a person can look at: today, then the six before it. The one on show is pressed. */
export function DayPicker({ value, today, onChange }: { value: string; today: string; onChange: (day: string) => void }) {
  return (
    <div role="group" aria-label="Day" className="flex flex-wrap items-center gap-1">
      {pickableDays(today).map((day) => {
        const on = day === value;
        return (
          <button
            key={day}
            type="button"
            aria-pressed={on}
            onClick={() => onChange(day)}
            className={cn(
              'h-8 rounded-full px-3 text-13 text-muted hover:bg-fill-1 hover:text-fg',
              on && 'bg-fill-2 text-fg',
            )}
          >
            {dayLabel(day, today)}
          </button>
        );
      })}
    </div>
  );
}
