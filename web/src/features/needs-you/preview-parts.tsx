/** What every post preview is built from: the node's text, a caption, a photo and the card's frame. */
import { useState } from 'react';
import type { Approval } from '@/api/types';
import { cn } from '@/lib/cn';
import type { Where } from './words';

/** The node's own marker for a preview it had to shorten. */
const CUT = /\n?\[preview truncated: (\d+) more characters are not shown\]\s*$/;

/**
 * A preview that is a record (JSON), not words. The node words every card it knows; anything else that
 * still looks like a record is never shown raw.
 */
export function isRecord(text: string | null | undefined): boolean {
  const head = (text ?? '').trimStart();
  return (head.startsWith('{') || head.startsWith('[')) && (head.includes('":') || /^(\{\}|\[\]|\[\{|\[")/.test(head));
}

export function previewText(approval: Approval): { text: string | null; cut: number | null } {
  const given = approval.preview.text ?? null;
  const raw = isRecord(given) ? null : given;
  const cut = raw ? CUT.exec(raw) : null;
  return { text: cut && raw ? raw.slice(0, cut.index) : raw, cut: cut?.[1] ? Number(cut[1]) : null };
}

/** Hashtags and mentions in the site's link colour, the rest as written. */
export function Caption({ text }: { text: string }) {
  const parts = text.split(/([#@][\p{L}\p{N}_.]+)/u);
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <span key={index} className="text-fg">
            {part}
          </span>
        ) : (
          part
        ),
      )}
    </>
  );
}

export function CutNote({ count }: { count: number | null }) {
  if (!count) return null;
  return (
    <p className="border-t border-line bg-inset px-4 py-2 text-secondary text-muted">
      The preview is cut here. {count.toLocaleString('en')} more characters are not shown, and saying yes covers all of them.
    </p>
  );
}

export function Photo({ src, className }: { src: string; className?: string }) {
  // From the computer, which may be away for a moment. Say so rather than show a broken picture.
  const [broken, setBroken] = useState(false);
  if (broken) {
    return (
      <div className={cn('flex aspect-[4/3] w-full items-center justify-center bg-inset px-6 text-center text-secondary text-muted', className)}>
        The picture cannot be shown right now. It is the one the computer will post.
      </div>
    );
  }
  return <img src={src} alt="What will be posted" onError={() => setBroken(true)} className={cn('block w-full bg-inset object-cover', className)} />;
}

export interface CardProps {
  approval: Approval;
  where: Where;
  color: string | null;
}

export const card = 'w-full overflow-hidden rounded-lg border border-line bg-card text-fg shadow-lift';

export const handleOf = (where: Where) => (where.handle ?? where.business ?? 'account').replace(/^@/, '');
