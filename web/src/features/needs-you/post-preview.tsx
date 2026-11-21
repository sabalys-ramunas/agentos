/**
 * The post as it will look on the site: an Instagram-shaped card for Instagram, a page post for
 * Facebook, a company post for LinkedIn, an article for Medium and blogs, and a plain page card for
 * anything else. A likeness drawn with our own components from what the node sends (the real
 * image and words), never a screenshot of the site and never its CSS (visual.md P1).
 *
 * Node text is always plain React text, so markup stays visible as markup.
 */
import type { ReactNode } from 'react';
import type { Approval } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { cn } from '@/lib/cn';
import type { Where } from './words';
import { isArticle } from './words';
import { kindOf } from './kinds';
import { LanePreview, PushPreview, VmPreview } from './dev-previews';
import type { CardProps } from './preview-parts';
import { Article, Facebook, Instagram, LinkedIn } from './social-cards';
import { Command, Deletion, Email, Files, Form, Generic, Payment, Refund, Thread } from './work-cards';

/** Which likeness to draw. */
export function previewKind(approval: Approval, where: Where) {
  const kind = kindOf(approval);
  switch (kind) {
    case 'email':
      return 'email';
    case 'form':
      return 'form';
    case 'order':
      return 'refund';
    case 'message':
      return 'thread';
    case 'command':
      return 'command';
    case 'files':
      return 'files';
    case 'delete':
      return 'deletion';
    case 'payment':
      return 'payment';
    case 'push':
      return 'push';
    case 'vm':
      return 'vm';
    case 'lane':
      return 'lane';
    default:
      break;
  }
  if (approval.effect === 'pay') return 'payment';
  if (approval.effect === 'irreversible') return 'deletion';
  if (approval.effect !== 'commit' && approval.effect !== 'draft') return 'generic';
  if (where.site === 'instagram') return 'instagram';
  if (where.site === 'facebook') return 'facebook';
  if (where.site === 'linkedin') return 'linkedin';
  if (isArticle(where)) return 'article';
  return 'generic';
}

const CARDS = {
  instagram: Instagram,
  facebook: Facebook,
  linkedin: LinkedIn,
  article: Article,
  payment: Payment,
  deletion: Deletion,
  generic: Generic,
  email: Email,
  form: Form,
  refund: Refund,
  thread: Thread,
  command: Command,
  files: Files,
  push: PushPreview,
  vm: VmPreview,
  lane: LanePreview,
};

/** The pill over the preview: what this picture is. */
function stageLabel(kind: ReturnType<typeof previewKind>, where: Where): string {
  switch (kind) {
    case 'payment':
      return 'What you pay';
    case 'deletion':
      return 'Exactly what goes';
    case 'email':
      return 'The email as it will be sent';
    case 'form':
      return 'The form as it will be sent';
    case 'refund':
      return 'The refund';
    case 'thread':
      return 'The reply in its thread';
    case 'command':
      return 'Exactly what will run';
    case 'files':
      return 'The files';
    case 'push':
      return 'What lands on the remote';
    case 'vm':
      return 'What it costs, and for how long';
    case 'lane':
      return 'Where it runs, and what runs first';
    case 'generic':
      if (where.site === null) return 'What it will do';
      break;
    default:
      break;
  }
  return `How it will look on ${where.siteLabel ?? 'the site'}`;
}

/**
 * The preview on a soft stage tinted with the business colour, with a pill naming what it is.
 * `children` sits over the card: the done state draws its check there.
 */
export function PostPreview({ approval, where, color, children, className, bare }: CardProps & { children?: ReactNode; className?: string; bare?: boolean }) {
  const kind = previewKind(approval, where);
  const Card = CARDS[kind];
  if (bare) {
    // On the desk's card, as drawn: the preview itself, full width, with what it is said in one quiet line.
    return (
      <div className={cn('grid gap-2', className)}>
        <p className="flex items-center gap-2 text-caption text-faint">
          {where.site ? <SiteMark site={where.site} size={14} label={false} /> : null}
          {stageLabel(kind, where)}
        </p>
        <div className="relative grid [&>article]:w-full [&>article]:max-w-none">
          <Card approval={approval} where={where} color={color} />
          {children}
        </div>
      </div>
    );
  }
  return (
    <div className={cn('relative flex flex-col items-center gap-4 rounded-lg border border-line bg-inset px-4 py-6 sm:px-6', className)}>
      <span className="inline-flex h-7 items-center gap-2 rounded-full border border-line bg-card px-3 text-caption text-muted">
        {where.site ? <SiteMark site={where.site} size={14} label={false} /> : null}
        {stageLabel(kind, where)}
      </span>
      <div className="relative flex w-full justify-center">
        <Card approval={approval} where={where} color={color} />
        {children}
      </div>
    </div>
  );
}
