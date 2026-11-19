/** Previews shaped like the site they post to: Instagram, Facebook, LinkedIn and an article. */
import { Bookmark, Globe, Heart, MessageCircle, MessageSquare, MoreHorizontal, Repeat2, Send, Share2, ThumbsUp } from 'lucide-react';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { cn } from '@/lib/cn';
import type { Where } from './words';
import type { CardProps } from './preview-parts';
import { Caption, CutNote, Photo, card, handleOf, previewText } from './preview-parts';

export function Instagram({ approval, where, color }: CardProps) {
  const { text, cut } = previewText(approval);
  const handle = handleOf(where);
  return (
    <article aria-label="Instagram post preview" className={cn(card, 'max-w-[440px]')}>
      <header className="flex items-center gap-2 px-3 py-2">
        <span className="rounded-full bg-[conic-gradient(from_200deg,var(--wait-dot),var(--brand),var(--ws-4),var(--wait-dot))] p-[2px]">
          <span className="block rounded-full bg-card p-[2px]">
            <BusinessMark name={where.business ?? handle} color={color} size={24} className="rounded-full" />
          </span>
        </span>
        <span className="min-w-0 flex-1 leading-tight">
          <span className="block truncate text-secondary font-semibold">{handle}</span>
          {where.business ? <span className="block truncate text-caption text-faint">{where.business}</span> : null}
        </span>
        <MoreHorizontal aria-hidden="true" className="size-4 text-muted" />
      </header>
      {approval.preview.image ? (
        <Photo src={approval.preview.image} className="aspect-square" />
      ) : (
        // Instagram always shows a picture, so words alone sit on a square of the business colour.
        <div className="flex aspect-square items-center justify-center bg-inset p-8 text-center text-heading break-words">{text}</div>
      )}
      <div className="flex items-center gap-4 px-3 pt-3 text-fg" aria-hidden="true">
        <Heart className="size-6" strokeWidth={1.75} />
        <MessageCircle className="size-6 -scale-x-100" strokeWidth={1.75} />
        <Send className="size-6" strokeWidth={1.75} />
        <Bookmark className="ml-auto size-6" strokeWidth={1.75} />
      </div>
      {text && approval.preview.image ? (
        <p className="px-3 pt-2 text-body break-words whitespace-pre-wrap">
          <span className="font-semibold">{handle}</span> <Caption text={text} />
        </p>
      ) : null}
      <p className="px-3 pt-2 pb-3 text-micro tracking-wide text-faint uppercase font-normal">Not posted yet. It goes up when you say yes</p>
      <CutNote count={cut} />
    </article>
  );
}

export function Facebook({ approval, where, color }: CardProps) {
  const { text, cut } = previewText(approval);
  return (
    <article aria-label="Facebook post preview" className={cn(card, 'max-w-[500px]')}>
      <header className="flex items-center gap-2 px-4 pt-3">
        <BusinessMark name={where.business ?? 'Page'} color={color} size={40} className="rounded-full" />
        <span className="min-w-0 flex-1 leading-tight">
          <span className="block truncate text-body font-semibold">{where.business ?? 'Your page'}</span>
          <span className="flex items-center gap-1 text-caption text-faint">
            Not posted yet · <Globe aria-hidden="true" className="size-3" />
          </span>
        </span>
        <MoreHorizontal aria-hidden="true" className="size-4 text-muted" />
      </header>
      {text ? (
        <p className="px-4 pt-3 pb-3 text-body break-words whitespace-pre-wrap">
          <Caption text={text} />
        </p>
      ) : (
        <div className="h-3" />
      )}
      {approval.preview.image ? <Photo src={approval.preview.image} className="max-h-[360px]" /> : null}
      <div className="mx-4 grid grid-cols-3 border-t border-line py-1 text-secondary font-medium text-muted" aria-hidden="true">
        <span className="flex h-8 items-center justify-center gap-2">
          <ThumbsUp className="size-4" /> Like
        </span>
        <span className="flex h-8 items-center justify-center gap-2">
          <MessageCircle className="size-4" /> Comment
        </span>
        <span className="flex h-8 items-center justify-center gap-2">
          <Share2 className="size-4" /> Share
        </span>
      </div>
      <CutNote count={cut} />
    </article>
  );
}

export function LinkedIn({ approval, where, color }: CardProps) {
  const { text, cut } = previewText(approval);
  return (
    <article aria-label="LinkedIn post preview" className={cn(card, 'max-w-[520px]')}>
      <header className="flex items-start gap-2 px-4 pt-3">
        <BusinessMark name={where.business ?? 'Company'} color={color} size={40} className="rounded-sm" />
        <span className="min-w-0 flex-1 leading-tight">
          <span className="block truncate text-body font-semibold">{where.business ?? 'Your company'}</span>
          <span className="block truncate text-caption text-faint">Company page</span>
          <span className="flex items-center gap-1 text-caption text-faint">
            Not posted yet · <Globe aria-hidden="true" className="size-3" />
          </span>
        </span>
        <MoreHorizontal aria-hidden="true" className="size-4 text-muted" />
      </header>
      {text ? (
        <p className="px-4 pt-3 pb-3 text-body break-words whitespace-pre-wrap">
          <Caption text={text} />
        </p>
      ) : (
        <div className="h-3" />
      )}
      {approval.preview.image ? <Photo src={approval.preview.image} className="max-h-[360px]" /> : null}
      <div className="mx-2 grid grid-cols-4 border-t border-line py-1 text-caption font-medium text-muted" aria-hidden="true">
        {[
          [ThumbsUp, 'Like'],
          [MessageSquare, 'Comment'],
          [Repeat2, 'Repost'],
          [Send, 'Send'],
        ].map(([Icon, label]) => {
          const Glyph = Icon as typeof ThumbsUp;
          return (
            <span key={label as string} className="flex h-12 flex-col items-center justify-center gap-1 text-micro font-normal">
              <Glyph className="size-4" /> {label as string}
            </span>
          );
        })}
      </div>
      <CutNote count={cut} />
    </article>
  );
}

export function Article({ approval, where, color }: CardProps) {
  const { text, cut } = previewText(approval);
  const [title = '', ...rest] = (text ?? approval.summary).split(/(?<=[.!?])\s+|\n/);
  const body = rest.join(' ').trim();
  return (
    <article aria-label={`${where.siteLabel ?? 'Article'} story preview`} className={cn(card, 'max-w-[560px]')}>
      <SiteBar where={where} />
      {approval.preview.image ? <Photo src={approval.preview.image} className="max-h-[280px]" /> : null}
      <div className="px-6 pt-5 pb-6">
        <h3 className="font-serif text-page-title-compact leading-tight break-words">{title}</h3>
        <p className="mt-3 flex items-center gap-2 text-secondary text-muted">
          <BusinessMark name={where.business ?? 'Author'} color={color} size={20} className="rounded-full" />
          {where.business ?? 'You'} · Not published yet
        </p>
        {body ? <p className="mt-4 font-serif text-lede leading-relaxed break-words whitespace-pre-wrap text-muted">{body}</p> : null}
      </div>
      <CutNote count={cut} />
    </article>
  );
}

/** A thin address bar, so a generic page still says which site it is on. */
export function SiteBar({ where }: { where: Where }) {
  return (
    <div className="flex h-8 items-center gap-2 border-b border-line bg-inset px-3 text-caption text-muted">
      <SiteMark site={where.site ?? where.domain ?? 'web'} size={14} label={false} />
      <span className="truncate">{where.domain ?? where.siteLabel ?? 'Website'}</span>
    </div>
  );
}
