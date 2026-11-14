import type { HTMLAttributes, ReactNode } from 'react';
import { cn } from '@/lib/cn';
import type { Kind } from './kind';

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** A kind of work: the card takes the kind's mark as a 2 px top edge (a job card). */
  kind?: Kind;
  /** Turn the lift off for a card inside a card, or a card that is a picture's frame. */
  flat?: boolean;
}

/**
 * A card: white on the off-white page (one step above black in dark), held by the hairline and the
 * lift. Radius 10. No grey band on its head.
 */
export function Card({ className, kind, flat = false, ...props }: CardProps) {
  return (
    <div
      data-kind={kind}
      className={cn('rounded-xl border border-line bg-card', !flat && 'shadow-lift', kind && 'ui-kind-edge overflow-hidden', className)}
      {...props}
    />
  );
}

interface CardHeaderProps {
  title: ReactNode;
  /** Quiet words after the title: a count, "the oldest for 2 h". */
  meta?: ReactNode;
  /** Right side: a link, a segmented control, a button. */
  action?: ReactNode;
  /** The heading level; a card in a page section is usually h2. */
  as?: 'h2' | 'h3';
  className?: string;
}

/** A card's head: title and meta on the left, the action on the right, one hairline under it. */
export function CardHeader({ title, meta, action, as: Heading = 'h2', className }: CardHeaderProps) {
  return (
    <div className={cn('flex min-h-13 flex-wrap items-center gap-x-3 gap-y-1 border-b border-line py-2 pr-3 pl-5', className)}>
      <Heading className="flex min-w-0 items-center gap-2 text-card-title">{title}</Heading>
      {meta ? <span className="min-w-0 text-secondary text-faint">{meta}</span> : null}
      {action ? <div className="ml-auto flex items-center gap-2">{action}</div> : null}
    </div>
  );
}

/** A card's foot: a well for a note and the action. */
export function CardFooter({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('flex items-center gap-2 rounded-b-xl border-t border-line px-5 py-3 text-caption text-muted', className)}
      {...props}
    />
  );
}

interface SectionProps {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}

/** A titled block of a page: heading on the left, action on the right, content below. */
export function Section({ title, description, action, children, className }: SectionProps) {
  return (
    <section className={cn('grid content-start gap-4', className)}>
      <div className="flex items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="text-section-title">{title}</h2>
          {description ? <p className="mt-1 text-secondary text-muted">{description}</p> : null}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

interface FormCardProps {
  title: string;
  description?: ReactNode;
  footnote?: ReactNode;
  action?: ReactNode;
  children?: ReactNode;
  className?: string;
}

/** A settings card: content on top, a well footer with a note and the action. */
export function FormCard({ title, description, footnote, action, children, className }: FormCardProps) {
  return (
    <Card className={className}>
      <div className="grid gap-4 p-6">
        <div>
          <h2 className="text-section-title">{title}</h2>
          {description ? <p className="mt-1 text-14 text-muted">{description}</p> : null}
        </div>
        {children}
      </div>
      {footnote || action ? (
        <div className="flex min-h-14 flex-col gap-2 rounded-b-xl border-t border-line bg-inset px-6 py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
          <p className="min-w-0 text-13 text-faint">{footnote}</p>
          {action ? <div className="self-end sm:self-auto">{action}</div> : null}
        </div>
      ) : null}
    </Card>
  );
}
