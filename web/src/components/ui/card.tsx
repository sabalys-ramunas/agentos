import { useId } from 'react';
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

/**
 * A whole card that is a link or a button: the card's look, and its edge firms on hover. Put it on the
 * <Link> itself (`className={cn(cardLink, 'grid gap-3 p-5')}`), so the card is one target.
 */
export const cardLink = 'rounded-xl border border-line bg-card shadow-lift transition-colors duration-(--duration-fast) hover:border-line-strong';

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
  /** Quiet words on the title's baseline: "7 runs", "the newest 200". */
  meta?: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}

/** A titled block of a page: heading on the left, action on the right, content below. */
export function Section({ title, meta, description, action, children, className }: SectionProps) {
  return (
    <section className={cn('grid content-start gap-4', className)}>
      <div className="flex items-end justify-between gap-4">
        <div className="min-w-0">
          {meta ? (
            <div className="flex min-w-0 flex-wrap items-baseline gap-x-3">
              <h2 className="text-section-title">{title}</h2>
              <span className="tabular text-secondary text-faint">{meta}</span>
            </div>
          ) : (
            <h2 className="text-section-title">{title}</h2>
          )}
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
  /** The footer's note, left of the action: what a value does, a limit, or "Saved". */
  footnote?: ReactNode;
  /** The footer's action, usually `FormActions`. */
  action?: ReactNode;
  /** Top right, beside the title: a switch that turns the whole card on, or a button that opens a dialog. */
  aside?: ReactNode;
  /** The section's wrapper tile (web/DESIGN.md, Forms): a channel, a plan. At most one card in three has one. */
  tile?: ReactNode;
  /** A small tile (28) sits level with the title; a larger one (36, 44) is centred on the title and its line. */
  tileAlign?: 'title' | 'center';
  /** A card that does something that cannot be undone: a red edge and a red footer band. */
  tone?: 'danger';
  /** Makes the card a form: Enter in a field submits it, and the action's Save is its submit button. */
  onSubmit?: () => void;
  /** The heading level; a card under a settings section's h2 is h3. */
  as?: 'h2' | 'h3';
  children?: ReactNode;
  className?: string;
}

/**
 * A settings card (web/DESIGN.md, Forms): the title in the section role with one line under it, the
 * fields, and a footer band in the well with a note on the left and the action on the right.
 */
export function FormCard({ title, description, footnote, action, aside, tile, tileAlign = 'title', tone, onSubmit, as: Heading = 'h3', children, className }: FormCardProps) {
  const id = useId();
  const danger = tone === 'danger';
  const body = (
    <>
      <div className="grid gap-4 px-6 py-5">
        <div className={cn('flex min-w-0 flex-wrap gap-x-4 gap-y-3', tile && tileAlign === 'center' ? 'items-center' : 'items-start')}>
          {tile ? <div className="shrink-0">{tile}</div> : null}
          <div className="min-w-0 flex-1 basis-48">
            <Heading id={`${id}-title`} className={cn('text-section-title', tile && tileAlign === 'title' && 'flex min-h-7 items-center')}>
              {title}
            </Heading>
            {description ? <p className="mt-1 text-secondary text-muted">{description}</p> : null}
          </div>
          {aside ? <div className="-my-1 flex min-h-9 shrink-0 items-center gap-2 max-sm:w-full">{aside}</div> : null}
        </div>
        {children}
      </div>
      {footnote || action ? (
        <div
          className={cn(
            'flex min-h-14 flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-b-xl border-t px-6 py-3',
            danger ? 'border-bad-line bg-bad-fill' : 'border-line bg-inset',
          )}
        >
          <div className="min-w-0 flex-1 basis-60 text-secondary text-faint">{footnote}</div>
          {action ? <div className="ml-auto flex shrink-0 items-center gap-2">{action}</div> : null}
        </div>
      ) : null}
    </>
  );
  const frame = cn(danger && 'border-bad-line', className);
  if (onSubmit) {
    return (
      <Card className={frame}>
        <form
          aria-labelledby={`${id}-title`}
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            onSubmit();
          }}
        >
          {body}
        </form>
      </Card>
    );
  }
  return (
    <Card className={frame}>
      <section aria-labelledby={`${id}-title`}>{body}</section>
    </Card>
  );
}
