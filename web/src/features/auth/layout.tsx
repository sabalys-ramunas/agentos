import { Link } from '@tanstack/react-router';
import { Card } from '@/components/ui/card';
import { Check, CircleAlert, Monitor } from 'lucide-react';
import type { ReactNode } from 'react';
import dawn from '@/assets/dawn/dawn-1600.webp';
import { BusinessMark } from '@/components/brand/identity';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Logo } from '@/components/ui/misc';
import { StatusDot } from '@/components/ui/status';
import { cn } from '@/lib/cn';

/**
 * The Dawn picture behind sign-in and first run (web/DESIGN.md, Glass and the Dawn picture): one of the few
 * places it may sit, always under one card or the demo, dimmed at night, never moving. Mirrored, the sun
 * rises on the right, away from the form.
 */
export function DawnPicture({ mirrored = false, className }: { mirrored?: boolean; className?: string }) {
  return (
    <div aria-hidden="true" className={cn('absolute inset-0 overflow-hidden bg-stage', className)}>
      <img src={dawn} alt="" className={cn('size-full object-cover', mirrored ? 'scale-x-[-1] object-[30%_50%]' : 'object-[35%_50%]')} />
      <div className="absolute inset-0 bg-(--dawn-dim)" />
    </div>
  );
}

/** The mark and the name, white on the picture or ink on the form. */
export function Brand({ onPicture = false }: { onPicture?: boolean }) {
  return (
    <Link
      to="/"
      aria-label="openajar"
      className={cn('inline-flex items-center gap-3 rounded-lg text-[18px] leading-7 font-semibold tracking-[-0.025em]', onPicture ? 'text-(--sidebar-text) [text-shadow:0_1px_8px_rgb(4_6_24/0.35)]' : 'text-fg')}
    >
      <Logo className="size-8" />
      openajar
    </Link>
  );
}

const glass = 'rounded-xl bg-glass shadow-glass backdrop-blur-[22px] backdrop-saturate-[1.6] [--text-tertiary:var(--text-secondary)]';

const TASKS: { text: string; done?: boolean; chip: ReactNode }[] = [
  { text: 'Fix the failing checkout test', done: true, chip: <Badge tone="ok"><StatusDot tone="ok" />Done</Badge> },
  { text: 'Build the pricing page, check on a phone', done: true, chip: <Badge tone="ok"><StatusDot tone="ok" />Done</Badge> },
  { text: 'Upgrade to React 19, push a branch', chip: <Badge tone="live"><StatusDot tone="live" />Working</Badge> },
  { text: 'Post for Aster Coffee every Tuesday', chip: <Badge tone="live"><StatusDot tone="live" />Asks you</Badge> },
];

/**
 * The promise in one picture, on the Dawn picture: the day's work done on the agent's own computer, and the
 * one question that reaches you. A drawing, so nothing in it can be pressed or focused.
 */
export function PromisePicture() {
  return (
    <div aria-hidden="true" className="grid w-full max-w-[420px] gap-4">
      <div className={cn(glass, 'px-4 pt-3 pb-1')}>
        <div className="flex items-baseline justify-between gap-3 pb-2 text-secondary">
          <span className="font-semibold text-fg">Today</span>
          <span className="truncate text-muted">12 done on their own · 1 needs you</span>
        </div>
        <ul>
          {TASKS.map((task) => (
            <li key={task.text} className="flex h-10 items-center gap-3 border-t border-window-edge text-secondary text-fg">
              {task.done ? <Check className="size-4 shrink-0 text-muted" /> : <Monitor className="size-4 shrink-0 text-muted" />}
              <span className="min-w-0 flex-1 truncate">{task.text}</span>
              {task.chip}
            </li>
          ))}
        </ul>
      </div>
      <GlassAsk className="ml-auto w-[min(100%,330px)]" />
    </div>
  );
}

/** The one question that reaches you, as it looks: a business, a post, Not now and Approve. A drawing. */
export function GlassAsk({ business = 'Aster Coffee', className }: { business?: string; className?: string }) {
  return (
    <div className={cn(glass, 'p-4', className)}>
      <div className="flex items-center gap-3">
        <BusinessMark name={business} size={24} />
        <span className="truncate text-label text-fg">{business}</span>
      </div>
      <p className="mt-3 text-label text-fg">Claude wants to post this. OK?</p>
      <p className="mt-1 text-secondary text-muted">Our autumn blend is back. Washed Ethiopian with a little dried plum on the finish.</p>
      <div className="mt-3 flex justify-end gap-2">
        <Button asChild size="sm">
          <span>Not now</span>
        </Button>
        <Button asChild size="sm" variant="primary">
          <span>Approve</span>
        </Button>
      </div>
    </div>
  );
}

interface AuthLayoutProps {
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  /**
   * The demo on the Dawn picture beside the form (`PromisePicture`, or what an invited person will be asked).
   * Without one the page is a single card in the middle of the picture.
   */
  aside?: ReactNode;
  /** The sentence at the foot of the picture, beside the form. */
  caption?: ReactNode;
  /** A wrapper tile over a single card's title (the mail tile on Check your email). */
  tile?: ReactNode;
}

export function AuthLayout({ title, description, children, footer, aside, caption, tile }: AuthLayoutProps) {
  if (!aside) {
    // One card in the middle of the picture: Check your email, an expired link, an invite.
    return (
      <main className="relative flex min-h-dvh flex-col px-4 py-6 sm:px-10 sm:py-8">
        <DawnPicture />
        <div className="relative">
          <Brand onPicture />
        </div>
        <div className="relative my-auto flex justify-center py-10">
          <Card role="region" aria-labelledby="auth-title" className="w-full max-w-[440px] px-6 py-8 text-center shadow-dialog sm:px-10">
            {tile ? <div className="mb-4 flex justify-center">{tile}</div> : null}
            <h1 id="auth-title" className="text-page-title-compact text-balance">
              {title}
            </h1>
            {description ? <div className="mt-2 text-body text-pretty text-muted">{description}</div> : null}
            {children ? <div className="mt-6 text-left">{children}</div> : null}
            {footer ? <div className="mt-5 text-secondary text-muted">{footer}</div> : null}
          </Card>
        </div>
      </main>
    );
  }
  const form = (
    <>
      <h1 className="text-page-title-compact text-balance md:text-page-title">{title}</h1>
      {description ? <div className="mt-2 text-lede text-muted">{description}</div> : null}
      {children ? <div className="mt-7">{children}</div> : null}
      {footer ? <p className="mt-6 text-secondary text-muted max-lg:text-center">{footer}</p> : null}
    </>
  );
  return (
    <main className="min-h-dvh bg-card lg:grid lg:grid-cols-[640px_minmax(0,1fr)]">
      {/* A phone and a tablet: the picture is a band at the top, and the form a sheet over its edge. */}
      <div className="relative h-62 lg:hidden">
        <DawnPicture />
        <div className="relative px-6 pt-14">
          <Brand onPicture />
        </div>
      </div>
      <div className="relative -mt-4 flex flex-col rounded-t-2xl bg-card px-6 pt-8 pb-10 sm:px-10 lg:mt-0 lg:min-h-dvh lg:rounded-none lg:px-24 lg:pt-10">
        <div className="hidden lg:block">
          <Brand />
        </div>
        <div className="mx-auto w-full max-w-[448px] lg:my-auto lg:py-12">{form}</div>
      </div>
      <div className="relative hidden min-h-dvh overflow-hidden lg:block">
        <DawnPicture mirrored />
        <div className="scrim-auth absolute inset-0" aria-hidden="true" />
        <div className="relative flex h-full min-h-dvh flex-col justify-between px-20 pt-36 pb-14 xl:px-22">
          {aside}
          {caption ? <p className="max-w-[38em] text-lede text-(--sidebar-text) [text-shadow:0_1px_8px_rgb(4_6_24/0.4)]">{caption}</p> : null}
        </div>
      </div>
    </main>
  );
}

/** A short message above a form, for what happened before this page. A failure is red, with a mark. */
export function Notice({ children, alert }: { children: ReactNode; alert?: boolean }) {
  return (
    <div
      role={alert ? 'alert' : 'status'}
      className={cn(
        'mb-6 flex gap-3 rounded-lg border px-4 py-3 text-secondary',
        alert ? 'border-bad-line bg-bad-fill text-bad' : 'border-line bg-inset text-muted',
      )}
    >
      {alert ? (
        <span className="flex h-5 shrink-0 items-center">
          <CircleAlert aria-hidden className="size-4" />
        </span>
      ) : null}
      <div className="min-w-0">{children}</div>
    </div>
  );
}

/** "or", between the email and the other ways in. */
export function Or() {
  return (
    <div className="my-1 flex items-center gap-3 text-secondary text-faint" aria-hidden="true">
      <span className="h-px flex-1 bg-line" />
      or
      <span className="h-px flex-1 bg-line" />
    </div>
  );
}

/** A way in, 44 px tall on these pages, full width. */
export const authButton = 'h-11 w-full';
