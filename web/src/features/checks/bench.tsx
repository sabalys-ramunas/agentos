/**
 * The bench of an open check: the strip of its screens (width and theme, with a count of findings on
 * each) and the chosen screen as an ImageFrame with the findings outlined and numbered from their real
 * rectangles. The picked finding is the bright one; the box scrolls to it.
 */
import { ImageOff, TriangleAlert } from 'lucide-react';
import { useEffect, useRef } from 'react';
import { studioShotUrl } from '@/api/client';
import type { StudioRun, StudioScreen } from '@/api/types';
import { ImageFrame } from '@/components/ui/image-frame';
import type { Finding } from '@/components/ui/image-frame';
import { Kbd } from '@/components/ui/misc';
import { Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { findingTitle, screenWords } from './model';
import type { NumberedFinding } from './model';
import { Thumb, useImageSize } from './parts';

const SHOT_WIDTH = 1280;
const TONE = { blocker: 'bad', major: 'warn', minor: 'idle', info: 'idle' } as const;

export function ScreenStrip({
  org,
  run,
  current,
  counts,
  onPick,
}: {
  org: string;
  run: StudioRun;
  current: string;
  counts: Map<string, number>;
  onPick: (key: string) => void;
}) {
  return (
    <div role="group" aria-label="Screens" className="scrollbar-none flex gap-2 overflow-x-auto border-b border-line p-3">
      {run.screen_list.map((screen, index) => {
        const count = counts.get(screen.key) ?? 0;
        const words = screenWords(screen);
        const on = screen.key === current;
        return (
          <button
            key={screen.key}
            type="button"
            aria-pressed={on}
            aria-label={`${words}, ${screen.ok ? plural(count, 'finding') : 'did not render'}${index < 9 ? `, key ${index + 1}` : ''}`}
            onClick={() => onPick(screen.key)}
            className={cn(
              'grid w-32 shrink-0 gap-1 rounded-lg border p-1 text-left transition-colors duration-(--duration-fast) ease-standard',
              on ? 'border-accent bg-accent-soft' : 'border-transparent hover:bg-hover',
            )}
          >
            <Thumb
              src={screen.image && !run.images_gone ? studioShotUrl(org, run.capture, screen.key, 240) : null}
              alt=""
              className="aspect-[16/10] w-full"
            />
            <span className="flex items-center justify-between gap-1 px-1">
              <span title={words} className={cn('truncate font-mono text-code-caption', on ? 'text-accent-text' : 'text-muted')}>
                {words}
              </span>
              {screen.ok ? (
                <span
                  className={cn(
                    'inline-flex h-4.5 min-w-4.5 items-center justify-center rounded-full px-1 font-mono text-micro tabular',
                    count ? 'bg-fg text-on-inverse' : 'border border-line text-faint',
                  )}
                >
                  {count}
                </span>
              ) : (
                <span className="text-micro text-bad">failed</span>
              )}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function boxesOn(findings: NumberedFinding[], screen: StudioScreen, scale: number): Finding[] {
  return findings.flatMap((finding) => {
    const rect = finding.rects?.[screen.key];
    if (!rect) return [];
    return [
      {
        id: finding.id,
        n: finding.n,
        tone: TONE[finding.severity],
        label: findingTitle(finding),
        box: { x: rect.x * scale, y: rect.y * scale, w: Math.max(4, rect.w * scale), h: Math.max(4, rect.h * scale) },
      },
    ];
  });
}

export function Bench({
  org,
  run,
  screen,
  findings,
  active,
  onSelect,
}: {
  org: string;
  run: StudioRun;
  screen: StudioScreen | undefined;
  findings: NumberedFinding[];
  active: NumberedFinding | undefined;
  onSelect: (finding: NumberedFinding) => void;
}) {
  const src = screen && screen.ok && screen.image && !run.images_gone ? studioShotUrl(org, run.capture, screen.key, SHOT_WIDTH) : null;
  const { size, failed } = useImageSize(src);
  const box = useRef<HTMLDivElement>(null);

  // Bring the picked finding to the middle of the box, without moving the page.
  useEffect(() => {
    const container = box.current;
    const target = container?.querySelector<HTMLElement>('.ui-finding[data-active]');
    if (!container || !target) return;
    const outer = container.getBoundingClientRect();
    const inner = target.getBoundingClientRect();
    container.scrollTo({ top: container.scrollTop + inner.top - outer.top - container.clientHeight / 2 + inner.height / 2, behavior: 'smooth' });
  }, [active?.id, size, screen?.key]);

  if (!screen) {
    return <Missing title="No screen to show" text="This check kept no screens." />;
  }
  if (!screen.ok) {
    return <Missing title={`${screenWords(screen)} did not render`} text={screen.error ?? 'Studio could not open the page at this size.'} warn />;
  }
  if (run.images_gone || !screen.image || failed) {
    return (
      <Missing
        title="The picture of this screen is no longer kept"
        text="Studio lets old pictures go to save space. Every finding is still here, with what was measured and the fix."
      />
    );
  }
  const scale = size ? size.width / screen.width : 1;
  const boxes = size ? boxesOn(findings, screen, scale) : [];
  const unplaced = findings.filter((finding) => !finding.rects?.[screen.key]).length;
  return (
    <div ref={box} className="max-h-[min(72vh,720px)] min-h-60 overflow-auto bg-inset p-3 md:p-4">
      {size && src ? (
        <div className="mx-auto" style={{ maxWidth: Math.max(screen.width * 1.25, 320) }}>
        <ImageFrame
          src={src}
          alt={`The page at ${screenWords(screen)}, with ${plural(boxes.length, 'finding')} outlined and numbered`}
          width={size.width}
          height={size.height}
          findings={boxes}
          active={active?.id}
          onSelect={(id) => {
            const finding = findings.find((item) => item.id === id);
            if (finding) onSelect(finding);
          }}
          className="bg-card"
        />
        </div>
      ) : (
        <Skeleton className="mx-auto aspect-[10/16] h-auto w-full max-w-md" />
      )}
      {size && unplaced > 0 ? (
        <p className="mt-3 text-center text-caption text-faint">
          {plural(unplaced, 'finding')} on this screen {unplaced === 1 ? 'has' : 'have'} no single place to outline: page wide.
        </p>
      ) : null}
    </div>
  );
}

function Missing({ title, text, warn }: { title: string; text: string; warn?: boolean }) {
  return (
    <div className="grid min-h-60 place-items-center bg-inset px-6 py-12 text-center">
      <div className="grid max-w-sm justify-items-center gap-2">
        <span aria-hidden="true" className="grid size-10 place-items-center rounded-lg border border-line bg-card text-muted shadow-lift [&_svg]:size-5">
          {warn ? <TriangleAlert className="text-bad" /> : <ImageOff />}
        </span>
        <h3 className="text-card-title">{title}</h3>
        <p className="text-body text-muted">{text}</p>
      </div>
    </div>
  );
}

/** The foot of the bench: the font caveat and the keys. */
export function BenchFoot({ fontsNote }: { fontsNote: boolean }) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line px-5 py-3 text-caption text-muted">
      {fontsNote ? (
        <span className="inline-flex min-w-0 items-center gap-2">
          <TriangleAlert aria-hidden="true" className="size-3.5 shrink-0" />
          Text used this computer's fonts; another computer can wrap a little differently.
        </span>
      ) : null}
      <span className="ml-auto hidden items-center gap-1 md:inline-flex">
        <Kbd>1</Kbd>
        <span>to</span>
        <Kbd>9</Kbd>
        <span>screens,</span>
        <Kbd>J</Kbd>
        <Kbd>K</Kbd>
        <span>findings</span>
      </span>
    </div>
  );
}
