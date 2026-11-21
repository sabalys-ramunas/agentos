import type { UseQueryResult } from '@tanstack/react-query';
import { AlertCircle, CircleAlert, Lock, Unplug } from 'lucide-react';
import type { HTMLAttributes, ReactNode } from 'react';
import { useProviders } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import { cn } from '@/lib/cn';
import dawnSmall from '@/assets/dawn/dawn-720.webp';
import { Button } from './button';
import { Card } from './card';

export function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div aria-hidden="true" className={cn('skeleton h-4', className)} {...props} />;
}

interface EmptyStateProps {
  icon?: ReactNode;
  /** A small crop of the Dawn picture in place of the icon: for a calm empty that is good news ("All clear") or
   * a first use. `grey` greys it, for "nothing right now". */
  dawn?: boolean | 'grey';
  title: string;
  /** The title's level: h3 inside a section (the default), h2 when the empty state is the page's first part. */
  heading?: 'h2' | 'h3';
  description?: ReactNode;
  action?: ReactNode;
  /** Anything under the action (a code block for Technical details). */
  children?: ReactNode;
  className?: string;
}

/** The empty state's icon: in a small bordered tile with the lift. */
function StateIcon({ children }: { children: ReactNode }) {
  return (
    <div aria-hidden="true" className="mb-4 grid size-11 place-items-center rounded-xl border border-line bg-card text-muted shadow-lift [&_svg]:size-5">
      {children}
    </div>
  );
}

/**
 * The Dawn picture as a small crop (220 by 124 at radius 8, dimmed at night like every Dawn picture). The one place
 * the picture sits on a plain page: an empty state, with one line under it (web/DESIGN.md, Glass and the Dawn picture).
 */
export function DawnCrop({ className, grey = false }: { className?: string; grey?: boolean }) {
  return (
    <span aria-hidden="true" className={cn('relative block aspect-video w-55 max-w-full shrink-0 overflow-hidden rounded-lg bg-stage', className)}>
      <img src={dawnSmall} alt="" className={cn('size-full object-cover object-[50%_38%]', grey && 'opacity-80 saturate-0')} />
      <span className="absolute inset-0 bg-(--dawn-dim)" />
    </span>
  );
}

/** Says what would be here and gives the next action. Never just "No data": an icon, one bold line, one sentence, one action. */
export function EmptyState({ icon, dawn, title, heading: Heading = 'h3', description, action, children, className }: EmptyStateProps) {
  return (
    <div className={cn('flex flex-col items-center px-6 py-16 text-center', className)}>
      {dawn ? <DawnCrop className="mb-5" grey={dawn === 'grey'} /> : icon ? <StateIcon>{icon}</StateIcon> : null}
      <Heading className="text-card-title">{title}</Heading>
      {description ? <p className="mt-1 max-w-sm text-body text-muted">{description}</p> : null}
      {action ? <div className="mt-4">{action}</div> : null}
      {children}
    </div>
  );
}

function errorCopy(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'network') return error.message;
    if (error.code === 'not_found') return 'This does not exist, or you do not have access to it.';
    if (error.status === 403) return 'You do not have access to this.';
    if (error.status >= 500) return 'The service did not answer. Nothing was changed.';
    return error.message;
  }
  return 'Something went wrong. Nothing was changed.';
}

export function ErrorState({ error, onRetry, className }: { error: unknown; onRetry?: () => void; className?: string }) {
  // Not served is not a failure: the part is simply on another machine. See NotLive.
  if (isNotServed(error)) return <NotLive className={className} />;
  return (
    <div role="alert" className={cn('flex flex-col items-center px-6 py-16 text-center', className)}>
      <StateIcon>
        <AlertCircle className="text-bad" />
      </StateIcon>
      <h3 className="text-card-title">Could not load this</h3>
      <p className="mt-1 max-w-sm text-body text-muted">{errorCopy(error)}</p>
      {onRetry ? (
        <Button className="mt-4" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

/**
 * For a part this environment does not serve. Nothing is wrong. On a computer it is a part that is not
 * switched on. On the hosted app it lives on the computer that runs the accounts, so it says where.
 */
export function NotLive({ className }: { className?: string }) {
  const onComputer = useProviders().data?.code === true;
  if (!onComputer) {
    return (
      <EmptyState
        icon={<Unplug />}
        title="Shown on your computer"
        description="openajar Cloud does not show this part. Your computer does, so open it from Computers."
        className={className}
      />
    );
  }
  return (
    <EmptyState
      icon={<Unplug />}
      title="Not switched on yet"
      description="This part of openajar arrives with the computer link. Nothing is wrong with your account."
      className={className}
    />
  );
}

/** A plain page for "you cannot be here", with the way back. */
export function NoAccess({ action }: { action?: ReactNode }) {
  return (
    <EmptyState
      icon={<Lock />}
      title="You do not have access to this"
      description="Your role in this organization does not include it. An owner or admin can change that."
      action={action}
    />
  );
}

/**
 * A refusal or failure said in place, beside what it is about: red words, announced once. For a form, a
 * row action or a switch that did not take. A whole view that could not load is an ErrorState instead.
 */
export function InlineError({ children, id, className }: { children: ReactNode; id?: string; className?: string }) {
  return (
    <p id={id} role="alert" className={cn('flex items-start gap-2 text-secondary text-bad', className)}>
      <span aria-hidden className="flex h-5 shrink-0 items-center">
        <CircleAlert className="size-4" />
      </span>
      <span className="min-w-0">{children}</span>
    </p>
  );
}

interface QueryViewProps<T> {
  query: UseQueryResult<T>;
  skeleton: ReactNode;
  children: (data: T) => ReactNode;
  /** Set when this view is not already inside a Card, so an error sits in one either way. */
  card?: boolean;
}

/** The loading and error half of every screen, so no screen forgets them. */
export function QueryView<T>({ query, skeleton, children, card }: QueryViewProps<T>) {
  if (query.isPending) {
    return (
      <div role="status" aria-label="Loading">
        {skeleton}
      </div>
    );
  }
  if (isNotServed(query.error)) return card ? <Card><NotLive /></Card> : <NotLive />;
  if (query.isError) {
    const state = <ErrorState error={query.error} onRetry={() => void query.refetch()} />;
    return card ? <Card>{state}</Card> : state;
  }
  return <>{children(query.data)}</>;
}
