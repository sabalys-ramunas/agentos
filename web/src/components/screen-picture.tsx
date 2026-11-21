/**
 * The picture of an environment's screen (web/DESIGN.md, "The screen's picture"): what a person would see if
 * they opened its live view, small enough for a card.
 *
 * - A still of the whole screen (`screen.kind === 'screen'`) fills the frame as it is.
 * - A still of a page (`'page'`, today the newest wide proof the agent kept) is drawn as a window on the
 *   machine's own Dawn desktop, which is how that page sits on its screen.
 * - With no still, the picture is drawn from the state: Starting shows the desktop with the title and
 *   "Starting its computer"; Asleep (saved, closing, closed) the desktop dimmed; anything else a terminal
 *   window on the desktop, since that is where an agent starts.
 *
 * Nothing in it moves. The still fades in once it has loaded, and a still that fails falls back to the
 * drawing. The Dawn picture is one small file shared by every card.
 */
import { useState } from 'react';
import type { ReactNode } from 'react';
import type { EnvScreen } from '@/api/types';
import { cn } from '@/lib/cn';
import dawnSmall from '@/assets/dawn/dawn-720.webp';

export type PictureState = 'starting' | 'asleep' | 'awake';

interface ScreenPictureProps {
  /** What the machine is doing, for the drawing when there is no still. */
  state: PictureState;
  /** The environment's title, written on a starting machine's desktop. */
  title: string;
  screen?: EnvScreen | null;
  /** Overlays: the status chip and the time, in the picture's corners. */
  children?: ReactNode;
  className?: string;
}

function Desktop({ dim }: { dim?: boolean }) {
  return (
    <>
      <img src={dawnSmall} alt="" aria-hidden="true" draggable={false} className={cn('absolute inset-0 size-full object-cover', dim && 'opacity-70 saturate-50')} />
      {/* The desktop's top bar, as the machine draws it. */}
      <span aria-hidden="true" className="screen-topbar absolute inset-x-0 top-0 h-[5%]" />
    </>
  );
}

function Dock() {
  return <span aria-hidden="true" className="screen-dock absolute bottom-[3%] left-1/2 h-[7%] w-[16%] -translate-x-1/2 rounded-full" />;
}

/** A terminal window on the desktop: a few lines, two of them green. A drawing, not a picture of one. */
function TerminalWindow() {
  const lines: [number, string][] = [
    [34, 'w'],
    [62, ''],
    [40, 'g'],
    [55, ''],
    [28, 'w'],
    [46, 'g'],
  ];
  return (
    <span aria-hidden="true" className="screen-window absolute top-[14%] left-[9%] h-[58%] w-[58%] overflow-hidden rounded-[3px] bg-terminal">
      <span className="screen-window-bar block h-[9%]" />
      {lines.map(([width, tone], index) => (
        <span
          key={index}
          style={{ width: `${width}%` }}
          className={cn('mt-[3%] ml-[5%] block h-[4%] rounded-[2px]', tone === 'g' ? 'bg-ansi-green' : tone === 'w' ? 'bg-terminal-text opacity-80' : 'bg-terminal-dim')}
        />
      ))}
    </span>
  );
}

export function ScreenPicture({ state, title, screen, children, className }: ScreenPictureProps) {
  const [loaded, setLoaded] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const still = screen && failed !== screen.src && state !== 'starting' ? screen : null;
  const shown = still !== null && loaded === still.src;

  return (
    <div className={cn('screen-picture relative isolate aspect-video overflow-hidden rounded-lg bg-stage', className)}>
      {still?.kind === 'screen' ? null : <Desktop dim={state === 'asleep'} />}
      {still ? (
        still.kind === 'screen' ? (
          <img
            src={still.src}
            alt=""
            loading="lazy"
            decoding="async"
            onLoad={() => setLoaded(still.src)}
            onError={() => setFailed(still.src)}
            className={cn('absolute inset-0 size-full object-cover object-top transition-opacity duration-(--duration-slow) ease-standard', shown ? 'opacity-100' : 'opacity-0')}
          />
        ) : (
          <span className="screen-window absolute top-[10%] left-[10%] h-[74%] w-[80%] overflow-hidden rounded-[3px] bg-card">
            <span aria-hidden="true" className="screen-window-bar block h-[7%]" />
            <img
              src={still.src}
              alt=""
              loading="lazy"
              decoding="async"
              onLoad={() => setLoaded(still.src)}
              onError={() => setFailed(still.src)}
              className={cn('block h-[93%] w-full object-cover object-top transition-opacity duration-(--duration-slow) ease-standard', shown ? 'opacity-100' : 'opacity-0')}
            />
          </span>
        )
      ) : state === 'starting' ? (
        <span className="screen-greeting absolute inset-x-[6%] top-[30%] grid justify-items-center gap-1 text-center">
          <span className="line-clamp-2 text-[clamp(11px,4.2cqw,16px)] leading-tight font-semibold">{title}</span>
          <span className="text-[clamp(9px,2.6cqw,12px)] opacity-90">Starting its computer</span>
        </span>
      ) : state === 'awake' ? (
        <TerminalWindow />
      ) : null}
      {still?.kind === 'screen' ? null : <Dock />}
      {children}
    </div>
  );
}

/** A chip on the picture: dark glass, white words, so it reads on any screen. */
export function PictureChip({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={cn('picture-chip inline-flex h-7 items-center gap-2 rounded-full px-3 text-caption font-semibold whitespace-nowrap', className)}>
      {children}
    </span>
  );
}
