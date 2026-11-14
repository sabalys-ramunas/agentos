/**
 * Two pictures of the same page, before and after a change: a slider that wipes one over the other,
 * or the two side by side. The slider is a range input, so it works from the keyboard.
 */
import { useId, useState } from 'react';
import { cn } from '@/lib/cn';
import { SegmentedControl } from './tabs';

export interface Shot {
  src: string;
  alt: string;
  /** Under the picture: "Today", "Version 5". */
  label: string;
}

interface BeforeAfterProps {
  before: Shot;
  after: Shot;
  width: number;
  height: number;
  mode?: 'slider' | 'side';
  className?: string;
}

export function BeforeAfter({ before, after, width, height, mode: startMode = 'slider', className }: BeforeAfterProps) {
  const [mode, setMode] = useState<'slider' | 'side'>(startMode);
  const [at, setAt] = useState(50);
  const id = useId();
  return (
    <div className={cn('ui-before-after grid gap-3', className)}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-secondary text-muted">
          <span className="text-fg">{before.label}</span> and <span className="text-fg">{after.label}</span>
        </p>
        <SegmentedControl
          label="Compare"
          value={mode}
          onValueChange={setMode}
          options={[
            { value: 'slider', label: 'Slider' },
            { value: 'side', label: 'Side by side' },
          ]}
        />
      </div>
      {mode === 'side' ? (
        <div className="grid gap-3 sm:grid-cols-2">
          {[before, after].map((shot) => (
            <figure key={shot.label} className="grid gap-2">
              <div className="overflow-hidden rounded-lg border border-line bg-inset" style={{ aspectRatio: `${width} / ${height}` }}>
                <img src={shot.src} alt={shot.alt} width={width} height={height} className="size-full object-contain" />
              </div>
              <figcaption className="text-caption text-faint">{shot.label}</figcaption>
            </figure>
          ))}
        </div>
      ) : (
        <div className="grid gap-2">
          <div className="relative overflow-hidden rounded-lg border border-line bg-inset" style={{ aspectRatio: `${width} / ${height}` }}>
            <img src={before.src} alt={before.alt} width={width} height={height} className="absolute inset-0 size-full object-contain" />
            <img
              src={after.src}
              alt={after.alt}
              width={width}
              height={height}
              className="absolute inset-0 size-full object-contain"
              style={{ clipPath: `inset(0 0 0 ${at}%)` }}
            />
            <div aria-hidden="true" className="absolute inset-y-0 w-0.5 -translate-x-1/2 bg-fg" style={{ left: `${at}%` }} />
            <span className="absolute top-2 left-2 rounded-sm bg-inverse px-1 text-micro text-on-inverse">{before.label}</span>
            <span className="absolute top-2 right-2 rounded-sm bg-inverse px-1 text-micro text-on-inverse">{after.label}</span>
          </div>
          <label htmlFor={id} className="sr-only">
            How much of {after.label} to show
          </label>
          <input
            id={id}
            type="range"
            min={0}
            max={100}
            value={100 - at}
            aria-valuetext={`${100 - at}% ${after.label}`}
            onChange={(event) => setAt(100 - Number(event.target.value))}
            className="w-full"
          />
        </div>
      )}
    </div>
  );
}
