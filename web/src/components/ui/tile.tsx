/**
 * The wrapper tile (web/DESIGN.md, Components): the desktop dock's squircle with a soft top light, a white
 * glyph on a gradient. It marks the app's own things (a kind of ask on Home, a channel, a settings
 * section), never an action: a button is a pill. The kinds of work keep their own `KindTile`.
 */
import type { LucideIcon } from 'lucide-react';
import type { CSSProperties } from 'react';
import { cn } from '@/lib/cn';

export type TileTone = 'violet' | 'blue' | 'pink' | 'amber' | 'green' | 'ink' | 'teal' | 'red';

const sizes = {
  sm: 'size-7 rounded-[8px] [&_svg]:size-4',
  md: 'size-9 rounded-[10px] [&_svg]:size-5',
  lg: 'size-11 rounded-[12px] [&_svg]:size-6',
} as const;

export interface AppTileProps {
  tone: TileTone;
  icon: LucideIcon;
  size?: keyof typeof sizes;
  /** Its name for a screen reader, when no word sits beside it. */
  label?: string;
  className?: string;
}

export function AppTile({ tone, icon: Icon, size = 'md', label, className }: AppTileProps) {
  const style = { '--tile-top': `var(--tile-${tone}-top)`, '--tile-bottom': `var(--tile-${tone}-bottom)` } as CSSProperties;
  return (
    <span
      role={label ? 'img' : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      data-tone={tone}
      style={style}
      className={cn('ui-app-tile', sizes[size], className)}
    >
      <Icon strokeWidth={2} />
    </span>
  );
}
