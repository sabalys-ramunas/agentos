/**
 * A picture of a page (a screenshot from a design check) with its findings outlined and numbered.
 * Boxes are given in the picture's own pixels, so they stay on their element at any width. A pin is
 * a button when the findings can be picked. Zoom opens the picture large, findings and all.
 */
import { Maximize2 } from 'lucide-react';
import { useState } from 'react';
import { cn } from '@/lib/cn';
import { IconButton } from './button';
import { Dialog } from './dialog';

export interface Finding {
  id: string;
  /** The number on the pin. */
  n: number;
  /** In the picture's pixels. */
  box: { x: number; y: number; w: number; h: number };
  tone: 'bad' | 'warn' | 'idle' | 'ok';
  /** What is wrong, for the pin's name: "Text under 4.5:1". */
  label: string;
}

export interface ImageFrameProps {
  src: string;
  alt: string;
  /** The picture's size in pixels, so boxes can be placed before it loads. */
  width: number;
  height: number;
  findings?: Finding[];
  active?: string;
  onSelect?: (id: string) => void;
  zoomable?: boolean;
  className?: string;
}

function Picture({ src, alt, width, height, findings = [], active, onSelect }: Omit<ImageFrameProps, 'zoomable' | 'className'>) {
  const pct = (value: number, of: number) => `${(value / of) * 100}%`;
  return (
    <div className="relative w-full" style={{ aspectRatio: `${width} / ${height}` }}>
      <img src={src} alt={alt} width={width} height={height} className="absolute inset-0 size-full object-contain" />
      {findings.map((finding) => {
        const { x, y, w, h } = finding.box;
        const inside = x < width * 0.03 || y < height * 0.03;
        const name = `Finding ${finding.n}: ${finding.label}`;
        return (
          <div
            key={finding.id}
            data-tone={finding.tone}
            data-inside={inside ? '' : undefined}
            data-active={active === finding.id ? '' : undefined}
            data-dim={active && active !== finding.id ? '' : undefined}
            className="ui-finding"
            style={{ left: pct(x, width), top: pct(y, height), width: pct(w, width), height: pct(h, height) }}
          >
            {onSelect ? (
              <button type="button" data-pin="" aria-label={name} aria-pressed={active === finding.id} onClick={() => onSelect(finding.id)}>
                {finding.n}
              </button>
            ) : (
              <span data-pin="" title={name}>
                {finding.n}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function ImageFrame({ zoomable = true, className, ...picture }: ImageFrameProps) {
  const [zoom, setZoom] = useState(false);
  return (
    <figure className={cn('relative overflow-hidden rounded-lg border border-line bg-inset', className)}>
      <Picture {...picture} />
      {zoomable ? (
        <>
          <IconButton
            label="Zoom in"
            variant="secondary"
            size="sm"
            className="absolute right-2 bottom-2"
            onClick={() => setZoom(true)}
          >
            <Maximize2 className="size-4" />
          </IconButton>
          <Dialog open={zoom} onOpenChange={setZoom} title={picture.alt} className="sm:max-w-[min(1200px,calc(100vw-32px))]">
            <div className="overflow-hidden rounded-lg border border-line bg-inset">
              <Picture {...picture} />
            </div>
          </Dialog>
        </>
      ) : null}
    </figure>
  );
}
