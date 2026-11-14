/**
 * A picture of an account: its screen right now, or the last thing it posted. Both are full
 * 1024 x 640 screenshots fetched from the computer on request (api-for-rebuild.md section 7), so a
 * picture is only asked for once it is close to the screen, fades in when it lands, and falls
 * back to the site's own mark when the computer cannot answer.
 */
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { StandIn } from './stand-in';

/** True once the element has come within a screen of the viewport. Stays true. */
function useNear<T extends Element>() {
  const ref = useRef<T>(null);
  const [near, setNear] = useState(false);
  useEffect(() => {
    const node = ref.current;
    if (!node || near) return;
    if (typeof IntersectionObserver === 'undefined') {
      setNear(true);
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setNear(true);
          observer.disconnect();
        }
      },
      { rootMargin: '240px' },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [near]);
  return [ref, near] as const;
}

interface PictureProps {
  src: string | null | undefined;
  /** The site, for the fallback mark. */
  site: string;
  /** Asks again on this beat while on screen, for a screen that is changing (an agent at work). */
  refreshMs?: number;
  /** Where the crop sits: the top of the page (a browser window), or its middle (a post). */
  focus?: 'top' | 'center';
  /** Greyed and dimmed: the account is resting. */
  resting?: boolean;
  /** A whole browser window, enlarged so the page's middle (where a post sits) is readable. */
  zoom?: boolean;
  /** For the stand-in shown until the picture lands, or when there is none: whose account, and its last line. */
  title?: string | null;
  line?: string | null;
  /** A small square: the stand-in leaves its words to the card under it. */
  compact?: boolean;
  className?: string;
  children?: ReactNode;
}

export function Picture({ src, site, refreshMs, focus = 'top', resting = false, zoom = false, title, line, compact, className, children }: PictureProps) {
  const [ref, near] = useNear<HTMLDivElement>();
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const [beat, setBeat] = useState(0);

  useEffect(() => {
    setFailed(false);
    setLoaded(false);
  }, [src]);

  useEffect(() => {
    if (!refreshMs || !near || failed) return;
    const timer = setInterval(() => setBeat((value) => value + 1), refreshMs);
    return () => clearInterval(timer);
  }, [refreshMs, near, failed]);

  const url = src && near && !failed ? (beat ? `${src}${src.includes('?') ? '&' : '?'}t=${beat}` : src) : null;

  return (
    <div ref={ref} className={cn('relative overflow-hidden bg-subtle', className)}>
      {/* The stand-in sits under the picture, so it shows while loading and when there is none. */}
      <div className={cn('absolute inset-0 transition-opacity duration-500', loaded && url ? 'opacity-0' : 'opacity-100')}>
        <StandIn site={site} title={title} line={line} compact={compact} />
      </div>
      {url ? (
        <img
          key={src}
          // A picture already in the cache can finish before React listens, so check on mount too.
          ref={(node) => {
            if (node?.complete && node.naturalWidth > 0) setLoaded(true);
          }}
          src={url}
          alt=""
          onLoad={(event) => (event.currentTarget.naturalWidth > 0 ? setLoaded(true) : setFailed(true))}
          onError={() => setFailed(true)}
          // Enlarged by size, not by a transform: a settled picture stays a plain image, which a
          // full-page capture and a slow phone both draw at once.
          style={zoom ? { width: '155%', height: '155%', left: '-25.3%', top: '-18.7%' } : undefined}
          className={cn(
            'absolute object-cover transition-[opacity,filter] duration-500 ease-(--ease-out)',
            zoom ? 'max-w-none' : 'inset-0 size-full',
            focus === 'top' ? 'object-top' : 'object-[50%_42%]',
            !loaded ? 'opacity-0' : resting ? 'opacity-60 grayscale' : 'opacity-100',
          )}
        />
      ) : null}
      {children}
    </div>
  );
}

/** A small chip on a picture: "Working now", "Live", "8m ago". */
export function PictureChip({ children, tone = 'plain', className }: { children: ReactNode; tone?: 'plain' | 'live' | 'dark'; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex h-6 items-center gap-2 rounded-full px-2 text-12 font-medium shadow-[0_1px_2px_rgb(0_0_0/0.12)] backdrop-blur-md',
        tone === 'plain' && 'bg-bg/90 text-fg ring-1 ring-line',
        tone === 'live' && 'bg-bg/90 text-fg ring-1 ring-(--current-line)',
        tone === 'dark' && 'bg-[rgb(0_0_0/0.62)] text-[#fff]',
        className,
      )}
    >
      {children}
    </span>
  );
}

export function LiveDot() {
  return (
    <span aria-hidden="true" className="relative inline-flex size-2">
      <span className="absolute inset-0 animate-ping rounded-full bg-live opacity-60 motion-reduce:hidden" />
      <span className="relative size-2 rounded-full bg-live" />
    </span>
  );
}
