/**
 * Small drawings that explain a step without words a person has to learn: an approval card, a
 * phone, a computer that stays on, a terminal line, a connector screen. All colour comes from
 * tokens, so they follow light and dark. They are pictures: hidden from screen readers, because
 * the words beside them say the same thing.
 */
import { Check, Download, Monitor } from 'lucide-react';
import type { ReactNode } from 'react';
import { AgentMark, BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Logo } from '@/components/ui/misc';
import { cn } from '@/lib/cn';

/** A calm photo stand-in: a sun over two hills, in the sage of a real post. */
export function Landscape({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 240 150" preserveAspectRatio="xMidYMid slice" aria-hidden="true" className={cn('block w-full', className)}>
      <rect width="240" height="150" fill="color-mix(in srgb, var(--ok-soft) 70%, var(--background-subtle))" />
      <circle cx="160" cy="58" r="28" fill="color-mix(in srgb, var(--green) 40%, var(--background))" />
      <path d="M0 108 C60 92 120 96 240 104 L240 150 L0 150 Z" fill="color-mix(in srgb, var(--green) 28%, var(--background))" />
      <path d="M0 122 C80 110 160 112 240 116 L240 150 L0 150 Z" fill="color-mix(in srgb, var(--green) 62%, var(--foreground))" />
    </svg>
  );
}

/** A second photo, warmer, for "which photo should lead". */
function WarmPhoto({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 120 80" preserveAspectRatio="xMidYMid slice" aria-hidden="true" className={cn('block w-full', className)}>
      <rect width="120" height="80" fill="color-mix(in srgb, var(--wait-soft) 80%, var(--background))" />
      <circle cx="60" cy="38" r="16" fill="color-mix(in srgb, var(--amber) 55%, var(--background))" />
      <rect y="58" width="120" height="22" fill="color-mix(in srgb, var(--amber) 35%, var(--background))" />
    </svg>
  );
}

interface CardProps {
  business: string;
  site?: string;
  agent?: string;
  question?: string;
  caption?: string;
  yes?: string;
  className?: string;
  /** Smaller type, for a card inside a phone or an email. */
  small?: boolean;
}

/** What an approval looks like, drawn: whose account, the question, the post, two buttons. */
export function ApprovalPicture({
  business,
  site = 'instagram',
  agent = 'claude',
  question = 'Claude wants to post this. OK?',
  caption = 'Our autumn blend is back. Washed Ethiopian with a little dried plum on the finish. Roasted Tuesday, on shelves Thursday.',
  yes = 'Approve',
  small,
  className,
}: CardProps) {
  return (
    <div aria-hidden="true" className={cn('grid gap-3 rounded-xl border border-line bg-card p-4 shadow-card', className)}>
      <p className="flex items-center gap-2 text-caption text-muted">
        <BusinessMark name={business} size={16} />
        <span className="truncate">{business}</span>
        <SiteMark site={site} size={14} label={false} />
        <span className="ml-auto inline-flex items-center gap-1">
          <AgentMark principal={{ kind: 'agent', id: agent, thread: null }} size={14} />
        </span>
      </p>
      <p className={cn('font-semibold text-balance', small ? 'text-body' : 'text-section-title')}>{question}</p>
      <div className="overflow-hidden rounded-lg border border-line">
        <Landscape className={small ? 'h-20' : 'h-28'} />
        <p className={cn('px-3 py-2 text-muted', small ? 'text-caption' : 'text-secondary')}>
          <span className={cn(small && 'line-clamp-2')}>{caption}</span>
        </p>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <span className="inline-flex h-8 items-center justify-center rounded-md border border-line text-secondary">Not now</span>
        <span className="inline-flex h-8 items-center justify-center rounded-md bg-primary text-secondary font-medium text-on-primary">{yes}</span>
      </div>
    </div>
  );
}

/** A phone outline around anything, for "this is what reaches your phone". */
export function PhoneFrame({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div aria-hidden="true" className={cn('relative w-64 rounded-[36px] border-[6px] border-fg bg-card p-3 pt-8 shadow-dialog', className)}>
      <span className="absolute top-2 left-1/2 h-4 w-20 -translate-x-1/2 rounded-full bg-fg" />
      {children}
    </div>
  );
}

/** "Boost 1 post on Facebook for $40.00": the money question, drawn. */
export function MoneyPicture({ className }: { className?: string }) {
  return (
    <div aria-hidden="true" className={cn('grid place-items-center gap-1 rounded-lg border border-line bg-inset p-3 text-center', className)}>
      <span className="inline-flex items-center gap-1 text-micro text-muted font-normal">
        <SiteMark site="facebook" size={14} label={false} />
        Boost 1 post
      </span>
      <span className="tabular text-heading">$40.00</span>
      <span className="text-micro text-faint font-normal">once, on Facebook</span>
    </div>
  );
}

/** "Which photo should lead Thursday's post?": a choice only a person can make. */
export function QuestionPicture({ className }: { className?: string }) {
  return (
    <div aria-hidden="true" className={cn('grid gap-2 rounded-lg border border-line bg-inset p-3', className)}>
      <span className="text-micro text-muted font-normal">Which photo should lead Thursday's post?</span>
      <span className="grid grid-cols-2 gap-2">
        <span className="overflow-hidden rounded-md ring-2 ring-fg">
          <Landscape className="h-10" />
        </span>
        <span className="overflow-hidden rounded-md border border-line">
          <WarmPhoto className="h-10" />
        </span>
      </span>
    </div>
  );
}

/** A numbered picture step: the drawing on top, the words under it. */
export function PictureStep({ n, picture, children }: { n: number; picture: ReactNode; children: ReactNode }) {
  return (
    <li className="grid grid-cols-[112px_minmax(0,1fr)] items-center overflow-hidden rounded-xl border border-line bg-card sm:grid-cols-1 sm:content-start sm:items-stretch">
      <div className="grid h-24 place-items-center overflow-hidden border-r border-line bg-inset sm:h-32 sm:border-r-0 sm:border-b sm:px-4 [&>*]:scale-[0.6] sm:[&>*]:scale-100">{picture}</div>
      <p className="flex gap-2 p-3 text-body">
        <span aria-hidden="true" className="tabular inline-flex size-5 shrink-0 items-center justify-center rounded-full bg-fg text-micro text-card">
          {n}
        </span>
        <span>{children}</span>
      </p>
    </li>
  );
}

/** A small computer that stays on: a Mac mini, a laptop or a server. */
export function ComputerPicture() {
  return (
    <span aria-hidden="true" className="flex items-end gap-3 text-muted">
      <span className="grid h-10 w-20 place-items-center rounded-lg border-2 border-current bg-card">
        <span className="size-1.5 rounded-full bg-(--ok-dot)" />
      </span>
      <Monitor className="size-12" strokeWidth={1.25} />
    </span>
  );
}

/** One line in a terminal window, blurred: the shape of it, not something to read. */
export function TerminalPicture() {
  return (
    <span aria-hidden="true" className="w-44 overflow-hidden rounded-lg border border-line bg-fg text-card shadow-card">
      <span className="flex gap-1 border-b border-[color-mix(in_srgb,var(--background)_20%,transparent)] px-2 py-1">
        <span className="size-1.5 rounded-full bg-current opacity-40" />
        <span className="size-1.5 rounded-full bg-current opacity-40" />
        <span className="size-1.5 rounded-full bg-current opacity-40" />
      </span>
      <span className="mono block px-2 py-2 text-micro font-normal">
        $ <span className="inline-block h-2 w-28 rounded-sm bg-current align-middle opacity-50" />
      </span>
    </span>
  );
}

/** "It turns green here": the status row the panel shows when a computer says hello. */
export function GreenPicture({ name = 'Mac mini at home' }: { name?: string }) {
  return (
    <span aria-hidden="true" className="flex items-center gap-2 rounded-lg border border-line bg-card px-3 py-2 text-secondary shadow-card">
      <span className="size-2 rounded-full bg-(--ok-dot)" />
      {name} is ready
    </span>
  );
}

/** A download that finished, for the Mac app that is coming. */
export function DownloadPicture() {
  return (
    <span aria-hidden="true" className="flex items-center gap-2 rounded-lg border border-line bg-card px-3 py-2 text-caption shadow-card">
      <Logo className="size-6" />
      <span className="grid">
        <span className="font-medium">agentos.dmg</span>
        <span className="text-faint">Downloaded</span>
      </span>
      <Download className="size-4 text-faint" />
    </span>
  );
}

/** A settings menu with one row lit: "open Settings, then Connectors". */
export function MenuPicture({ items, lit }: { items: string[]; lit: string }) {
  return (
    <span aria-hidden="true" className="grid w-40 gap-px rounded-lg border border-line bg-card p-1 text-caption shadow-card">
      {items.map((item) => (
        <span key={item} className={cn('rounded-md px-2 py-1', item === lit ? 'bg-(--current-soft) font-medium text-fg ring-1 ring-(--current-line)' : 'text-muted')}>
          {item}
        </span>
      ))}
    </span>
  );
}

/** A field with a link pasted in, and an Add button. */
export function PastePicture({ label, value }: { label: string; value: string }) {
  return (
    <span aria-hidden="true" className="grid w-44 gap-2 rounded-lg border border-line bg-card p-2 text-micro shadow-card font-normal">
      <span className="font-medium">{label}</span>
      <span className="mono truncate rounded-sm border border-(--current-line) px-1 py-1 text-faint">{value}</span>
      <span className="justify-self-end rounded-sm bg-fg px-2 py-px text-card">Add</span>
    </span>
  );
}

/** Our consent screen: "Let Claude use Aster Coffee's computer?" and Allow. */
export function AllowPicture({ agent, business }: { agent: string; business: string }) {
  return (
    <span aria-hidden="true" className="grid w-44 gap-1 rounded-lg border border-line bg-card p-2 text-micro shadow-card font-normal">
      <span className="flex items-center gap-1 font-medium">
        <Logo className="size-3.5" />
        Let {agent} use {business}'s computer?
      </span>
      <span className="text-faint">It works on its own, and asks you before it pays or deletes.</span>
      <span className="rounded-sm bg-fg py-px text-center text-card">Allow</span>
    </span>
  );
}

/** A done row, for a step that is finished. */
export function DoneRow({ children }: { children: ReactNode }) {
  return (
    <p className="flex items-center gap-2 text-body">
      <span aria-hidden="true" className="inline-flex size-5 items-center justify-center rounded-full bg-(--ok-soft) text-(--ok-dot)">
        <Check className="size-3.5" strokeWidth={2.5} />
      </span>
      {children}
    </p>
  );
}
