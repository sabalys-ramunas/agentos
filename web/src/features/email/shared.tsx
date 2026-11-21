/** The pieces every email page shares: where a person is, and the line that says where text came from. */
import { Link } from '@tanstack/react-router';
import { Monitor } from 'lucide-react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';
import './email.css';

type Place = 'mailboxes' | 'new-mail' | 'leads';

export function EmailTabs({ org, here, newCount, waiting, className }: { org: string; here: Place; newCount?: number; waiting?: number; className?: string }) {
  const item = (place: Place, word: string, count?: number) => ({ place, word, count });
  const items = [item('mailboxes', 'Mailboxes'), item('new-mail', 'New mail', newCount), item('leads', 'Leads', waiting)];
  const to = { mailboxes: '/$org/email', 'new-mail': '/$org/email/new-mail', leads: '/$org/email/leads' } as const;
  return (
    // The three views as the drawings' segmented well, each with its count; the chosen one is the card.
    <nav aria-label="Email" className={cn('scrollbar-none mb-6 inline-flex max-w-full gap-px overflow-x-auto rounded-full border border-line bg-inset p-1', className)}>
      {items.map(({ place, word, count }) => (
        <Link
          key={place}
          to={to[place]}
          params={{ org }}
          aria-current={place === here ? 'page' : undefined}
          className={cn(
            'flex h-8 shrink-0 items-center gap-2 rounded-full border px-3 text-label whitespace-nowrap transition-colors duration-(--duration-fast) ease-standard',
            place === here ? 'border-line bg-card font-semibold text-fg shadow-lift' : 'border-transparent text-muted hover:text-fg',
          )}
        >
          {word}
          {count ? <span className="tabular text-secondary font-normal text-faint">{count}</span> : null}
        </Link>
      ))}
    </nav>
  );
}

/** Mail text and leads' addresses stay on the computer. This says so wherever they are shown. */
export function FromComputer({ computers, className }: { computers: string[]; className?: string }) {
  return (
    <p className={cn('flex items-center gap-2 text-caption text-faint', className)}>
      <Monitor aria-hidden="true" className="size-3 shrink-0" />
      <span>
        Read live from <span className="mono">{computers.join(' and ')}</span>. It stays there.
      </span>
    </p>
  );
}

/** A lock, a rule, no control: what the computer always does. */
export function Rule({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <li className="flex items-start gap-3 py-2 text-body text-pretty">
      <span className="mt-px shrink-0 text-faint">{icon}</span>
      <span className="min-w-0">{children}</span>
    </li>
  );
}
