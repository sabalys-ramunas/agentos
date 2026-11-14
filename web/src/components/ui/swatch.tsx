import { cn } from '@/lib/cn';
import { CopyButton } from './misc';

/**
 * A colour shown as data: another brand's value, a token's value. It is the colour itself, so it is
 * marked for studio as data, not as this app's style.
 */
export function Swatch({ color, name, value, tag, className }: { color: string; name: string; value?: string; tag?: string; className?: string }) {
  return (
    <figure className={cn('min-w-0 overflow-hidden rounded-lg border border-line bg-card', className)}>
      <div data-studio="ignore" role="img" aria-label={`${name}, ${value ?? color}`} className="relative h-16" style={{ background: color }}>
        {tag ? <span className="absolute top-1 left-1 rounded-sm bg-inverse px-1 text-micro text-on-inverse">{tag}</span> : null}
      </div>
      <figcaption className="grid border-t border-line p-2">
        <b title={name} className="truncate font-mono text-code-caption font-medium">
          {name}
        </b>
        <span title={value ?? color} className="truncate font-mono text-micro text-muted">
          {value ?? color}
        </span>
      </figcaption>
    </figure>
  );
}

/** A token's name as a mono chip, with an optional colour square, and a copy button when asked. */
export function TokenChip({ name, color, copy = false, className }: { name: string; color?: string; copy?: boolean; className?: string }) {
  return (
    <span className={cn('inline-flex max-w-full min-w-0 items-center gap-1 rounded-sm border border-line bg-hover px-1 font-mono text-code-caption text-fg', className)}>
      {color ? <span data-studio="ignore" aria-hidden="true" className="size-2.5 shrink-0 rounded-[2px] border border-line" style={{ background: color }} /> : null}
      <span title={name} className="truncate">
        {name}
      </span>
      {copy ? <CopyButton text={name} label={`Copy ${name}`} /> : null}
    </span>
  );
}
