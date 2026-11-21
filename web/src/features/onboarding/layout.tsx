/**
 * The frame every setup step shares (startup-onboarding.html): the business at the top with
 * "Save and finish later", Getting started on the left with what is really done, one step in the
 * middle, and a footer with Back, a quiet note, "I'll do this later" and Continue.
 */
import { Link } from '@tanstack/react-router';
import { Check } from 'lucide-react';
import type { ReactNode } from 'react';
import { useLiveEvents } from '@/api/live';
import type { SetupItemKey } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { Badge } from '@/components/ui/badge';
import { Logo } from '@/components/ui/misc';
import { cn } from '@/lib/cn';
import type { Setup } from './setup';
import { minutesLeft } from './setup';

export type StepKey = 'goal' | 'computer' | 'accounts' | 'ai' | 'try' | 'people';

export interface StepInfo {
  key: StepKey;
  title: string;
  hint: string;
  /** Which checklist items make this step done. */
  items: SetupItemKey[];
}

export const STEPS: StepInfo[] = [
  { key: 'goal', title: 'Its first job', hint: 'What it should do', items: ['goal'] },
  { key: 'computer', title: 'A computer', hint: 'Where it works', items: ['computer'] },
  { key: 'accounts', title: 'Accounts', hint: 'The websites it uses', items: ['account'] },
  { key: 'ai', title: 'Your AI', hint: 'Claude, ChatGPT and more', items: ['ai'] },
  { key: 'try', title: 'A first try', hint: 'See it ask you', items: ['approval'] },
  { key: 'people', title: 'Phone and people', hint: 'Who says yes', items: ['phone', 'invite'] },
];

/** Old step names in links from before the rebuild (Home, emails, bookmarks) still land somewhere. */
export const STEP_ALIASES: Record<string, StepKey> = { node: 'computer', template: 'accounts', agent: 'ai', phone: 'people' };

export function stepDone(setup: Setup | null, step: StepInfo): boolean {
  if (!setup) return false;
  const items = setup.items.filter((item) => step.items.includes(item.key));
  // Accounts with none chosen are not done: there is nothing for the AI to use yet.
  return items.length > 0 && items.every((item) => item.done);
}

function Progress({ done, total, className }: { done: number; total: number; className?: string }) {
  return (
    <div
      role="progressbar"
      aria-label="Setup progress"
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={done}
      className={cn('h-1 overflow-hidden rounded-full bg-fill-pressed', className)}
    >
      <div className="h-full rounded-full bg-accent transition-[width] duration-(--duration-slow) ease-standard" style={{ width: `${(done / total) * 100}%` }} />
    </div>
  );
}

/**
 * Getting started, on the night panel (the sidebar's sky, 300 px): what is really done, with a numbered
 * ring per step. Done is a violet ring with a tick, the open step a white disc, the rest outlined.
 */
function Sidebar({ org, current, setup, hints }: { org: string | null; current: StepKey; setup: Setup | null; hints: Partial<Record<StepKey, string>> }) {
  const done = STEPS.filter((step) => stepDone(setup, step)).length;
  const left = minutesLeft(setup);
  return (
    <aside aria-label="Getting started" className="rail-night sticky top-0 hidden h-dvh w-75 shrink-0 flex-col overflow-y-auto px-5 pt-6 pb-6 lg:flex">
      <Link to="/" aria-label="openajar" className="inline-flex items-center gap-3 self-start rounded-lg px-1 text-section-title text-fg">
        <Logo className="size-7" />
        openajar
      </Link>
      <div className="mt-8 px-1">
        <p className="text-card-title text-fg">Getting started</p>
        <p className="tabular mt-1 text-secondary text-muted">
          {done} of {STEPS.length} done · about {left} min left
        </p>
        <Progress done={done} total={STEPS.length} className="mt-4" />
      </div>
      <ol className="mt-5 grid gap-1">
        {STEPS.map((step, index) => {
          const finished = stepDone(setup, step);
          const here = step.key === current;
          const body = (
            <>
              <span
                aria-hidden="true"
                className={cn(
                  'inline-flex size-6 shrink-0 items-center justify-center rounded-full border text-caption font-semibold',
                  here
                    ? 'border-transparent bg-(--sidebar-text) text-(--sidebar-sky-1)'
                    : finished
                      ? 'border-(--violet-300) text-(--violet-300)'
                      : 'border-(--sidebar-text-tertiary) text-muted',
                )}
              >
                {finished && !here ? <Check className="size-3.5" strokeWidth={2.5} /> : index + 1}
              </span>
              <span className="min-w-0">
                <span className={cn('block text-body', here || finished ? 'text-fg' : 'text-muted', here && 'font-semibold')}>{step.title}</span>
                <span className="block truncate text-secondary text-muted">{hints[step.key] ?? step.hint}</span>
              </span>
              {finished ? <span className="sr-only">, done</span> : null}
            </>
          );
          const className = cn('flex items-start gap-3 rounded-lg border px-3 py-2', here ? 'border-(--sidebar-line) bg-(--sidebar-fill-selected)' : 'border-transparent');
          return (
            <li key={step.key}>
              {org && step.key !== 'goal' ? (
                <Link to="/onboarding/$org/$step" params={{ org, step: step.key }} aria-current={here ? 'step' : undefined} className={cn(className, !here && 'hover:bg-hover')}>
                  {body}
                </Link>
              ) : (
                <div aria-current={here ? 'step' : undefined} className={className}>
                  {body}
                </div>
              )}
            </li>
          );
        })}
      </ol>
      <p className="mt-auto rounded-lg bg-side-foot px-3 py-3 text-secondary text-muted">
        Leave whenever you like. This list waits on Home, and each step picks up where you stopped.
      </p>
    </aside>
  );
}

interface LayoutProps {
  org: string | null;
  business: { name: string; color?: string | null } | null;
  step: StepKey;
  setup: Setup | null;
  /** Above the title: "Step 2 of 6", or what just became true ("Mac mini at home is ready"). */
  eyebrow?: ReactNode;
  title: string;
  description: ReactNode;
  children: ReactNode;
  footer: ReactNode;
  hints?: Partial<Record<StepKey, string>>;
  /** Wider content, for steps with a picture beside the work. */
  wide?: boolean;
}

/** The event stream, so a computer turning green and a first request arrive without a reload. */
function Live({ org }: { org: string }) {
  useLiveEvents(org);
  return null;
}

export function SetupLayout({ org, business, step, setup, eyebrow, title, description, children, footer, hints = {}, wide }: LayoutProps) {
  const index = STEPS.findIndex((item) => item.key === step);
  const done = STEPS.filter((item) => stepDone(setup, item)).length;
  return (
    <div className="flex min-h-dvh bg-page">
      {org ? <Live org={org} /> : null}
      <Sidebar org={org} current={step} setup={setup} hints={hints} />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-(--topbar-height) items-center gap-3 border-b border-line bg-page/90 px-4 backdrop-blur-md sm:px-10">
          <span className="lg:hidden">
            <Logo />
          </span>
          {business ? (
            <>
              <BusinessMark name={business.name} color={business.color} size={24} />
              <span className="min-w-0 truncate text-label text-fg">{business.name}</span>
              <Badge className="hidden sm:inline-flex">Setting up</Badge>
            </>
          ) : null}
          {org ? (
            <Link to="/$org" params={{ org }} className="ml-auto inline-flex h-8 shrink-0 items-center rounded-full px-3 text-secondary text-muted hover:bg-hover hover:text-fg">
              Save and finish later
            </Link>
          ) : null}
        </header>
        <main className="min-w-0 flex-1">
          <div className="border-b border-line px-4 py-3 lg:hidden">
            <p className="tabular text-secondary text-muted">
              Step {index + 1} of {STEPS.length} · <span className="text-fg">{STEPS[index]?.title}</span>
            </p>
            <Progress done={done} total={STEPS.length} className="mt-2 bg-chart-grid" />
          </div>
          <div className={cn('mx-auto w-full px-4 pt-8 pb-12 sm:px-10 lg:pt-14', wide ? 'max-w-[960px]' : 'max-w-[760px]')}>
            {eyebrow ? <div className="mb-2 text-secondary text-muted">{eyebrow}</div> : null}
            <h1 className="text-page-title-compact text-balance md:text-page-title">{title}</h1>
            <div className="mt-2 max-w-[46em] text-lede text-muted">{description}</div>
            <div className="mt-8">{children}</div>
            <div className="mt-8 flex flex-wrap items-center gap-2">{footer}</div>
          </div>
        </main>
      </div>
    </div>
  );
}

/** A green dot and a fact, for the eyebrow when the last step is really done. */
export function Ready({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span aria-hidden="true" className="size-2 rounded-full bg-(--ok-dot)" />
      {children}
    </span>
  );
}
