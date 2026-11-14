/**
 * Small parts the Checks pages and Brand share: a share bar, a screen's picture that says plainly when
 * it is gone, the calm state for a computer without studio, and the one for a check that is not yours.
 */
import { ImageOff, Lock, ScanSearch } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { StudioMissing } from '@/api/types';
import { CodeBlock } from '@/components/ui/misc';
import { EmptyState } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { percent } from './model';

/** A share of 0 to 1 as a thin ink bar and its percent. Ink: it is a measure, not a status. */
export function ShareBar({ share, label, wide = false, className }: { share: number; label: string; wide?: boolean; className?: string }) {
  const pct = Math.max(0, Math.min(100, Math.round(share * 100)));
  return (
    <span className={cn('inline-flex min-w-0 items-center gap-2', className)}>
      <span
        role="meter"
        aria-label={label}
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuetext={`${pct}%`}
        className={cn('h-1 shrink-0 overflow-hidden rounded-full bg-chart-grid', wide ? 'w-full max-w-48 shrink' : 'w-16')}
      >
        <span className="block h-full rounded-full bg-chart-bar-strong" style={{ width: `${pct}%` }} />
      </span>
      <span className="font-mono text-number text-fg tabular">{percent(share)}</span>
    </span>
  );
}

export interface ImageSize {
  width: number;
  height: number;
}

/**
 * The natural size of a picture, read once it loads, so outlines can be placed in its own pixels.
 * `failed` when it could not be loaded (pruned, or the screen did not render).
 */
export function useImageSize(src: string | null): { size: ImageSize | null; failed: boolean } {
  const [state, setState] = useState<{ src: string | null; size: ImageSize | null; failed: boolean }>({ src: null, size: null, failed: false });
  useEffect(() => {
    if (!src) return undefined;
    let live = true;
    const image = new Image();
    image.onload = () => live && setState({ src, size: { width: image.naturalWidth, height: image.naturalHeight }, failed: false });
    image.onerror = () => live && setState({ src, size: null, failed: true });
    image.src = src;
    return () => {
      live = false;
    };
  }, [src]);
  return state.src === src ? { size: state.size, failed: state.failed } : { size: null, failed: false };
}

/** A small picture of one screen, or a quiet tile that says it is not kept. */
export function Thumb({ src, alt, className }: { src: string | null; alt: string; className?: string }) {
  const [failed, setFailed] = useState(false);
  return (
    <span className={cn('relative block overflow-hidden rounded-md border border-line bg-inset', className)}>
      {src && !failed ? (
        <img src={src} alt={alt} loading="lazy" className="size-full object-cover object-top" onError={() => setFailed(true)} />
      ) : (
        <span className="grid size-full place-items-center text-faint" title="No picture kept">
          <ImageOff aria-hidden="true" className="size-4" />
          <span className="sr-only">No picture kept</span>
        </span>
      )}
    </span>
  );
}

/** Studio is not on this computer: what it is, and the command that adds it. */
export function StudioMissingState({ missing, className }: { missing: StudioMissing; className?: string }) {
  return (
    <EmptyState
      className={className}
      icon={<ScanSearch />}
      title="Studio is not on this computer"
      description={
        <>
          Studio opens your pages in a real browser at phone, tablet and desktop widths, in light and dark, and measures what is
          wrong. {missing.reason}
        </>
      }
      action={
        <div className="grid w-[min(28rem,80vw)] gap-2 text-left">
          <CodeBlock code={missing.command} label="Copy the command" />
          <CodeBlock code="agentos studio up" label="Copy the laptop command" />
        </div>
      }
    />
  );
}

/** A check in a workspace the person cannot see: calm, not an error. */
export function NotYours({ what = 'This check' }: { what?: string }) {
  return (
    <EmptyState
      icon={<Lock />}
      title={`${what} is not in a workspace you can see`}
      description="Checks belong to the workspace of the product they were checked against. A check with no product is the owner's alone."
    />
  );
}
