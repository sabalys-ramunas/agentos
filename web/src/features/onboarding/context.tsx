/** What every setup step after the first needs: the setup, its business, and how to move on. */
import { useNavigate } from '@tanstack/react-router';
import type { ReactNode } from 'react';
import { useWorkspaces } from '@/api/hooks';
import type { Workspace } from '@/api/types';
import { Button } from '@/components/ui/button';
import type { Goal } from './goals';
import { goalOf } from './goals';
import type { StepKey } from './layout';
import { STEPS } from './layout';
import type { Setup } from './setup';
import { remember, useSetup } from './setup';

export interface StepContext {
  org: string;
  setup: Setup | null;
  goal: Goal | null;
  workspace: Workspace | null;
  business: { name: string; color?: string | null } | null;
  /** Goes to a step, and remembers it so coming back lands there. */
  go: (step: StepKey | 'done') => void;
}

export function useStepContext(org: string): StepContext {
  const navigate = useNavigate();
  const { setup } = useSetup(org);
  const workspaces = useWorkspaces(org).data ?? [];
  const workspace = workspaces.find((item) => item.id === setup?.workspace) ?? workspaces[0] ?? null;
  const go = (step: StepKey | 'done') => {
    remember(org, { step });
    if (step === 'done') void navigate({ to: '/$org/needs-you', params: { org } });
    else void navigate({ to: '/onboarding/$org/$step', params: { org, step } });
  };
  return {
    org,
    setup,
    goal: goalOf(setup?.goal),
    workspace,
    business: workspace ? { name: workspace.name, color: workspace.color } : null,
    go,
  };
}

export function nextOf(step: StepKey): StepKey | 'done' {
  const index = STEPS.findIndex((item) => item.key === step);
  return STEPS[index + 1]?.key ?? 'done';
}

export function previousOf(step: StepKey): StepKey {
  const index = STEPS.findIndex((item) => item.key === step);
  return STEPS[Math.max(0, index - 1)]?.key ?? 'goal';
}

interface FooterProps {
  ctx: StepContext;
  step: StepKey;
  note?: ReactNode;
  /** Shown as "I'll do this later" when the step can be skipped. */
  later?: string | false;
  continueLabel?: string;
  canContinue?: boolean;
  onContinue?: () => void;
  busy?: boolean;
  /** Continue as a quiet button, when the main action is inside the step. */
  quiet?: boolean;
}

export function StepFooter({ ctx, step, note, later = "I'll do this later", continueLabel = 'Continue', canContinue = true, onContinue, busy, quiet }: FooterProps) {
  const next = nextOf(step);
  return (
    <>
      {/* On a phone the note goes first, on its own line, and the buttons sit under it. */}
      {note ? <p className="order-first w-full text-secondary text-faint lg:order-1 lg:w-auto lg:flex-1">{note}</p> : null}
      <Button variant="ghost" size="lg" className="-ml-3 lg:order-first" onClick={() => ctx.go(previousOf(step))}>
        Back
      </Button>
      <span className="ml-auto flex items-center gap-2 lg:order-2">
        {later ? (
          <Button variant="ghost" size="lg" onClick={() => ctx.go(next)}>
            {later}
          </Button>
        ) : null}
        <Button variant={quiet ? 'secondary' : 'primary'} size="lg" disabled={!canContinue} loading={busy} onClick={onContinue ?? (() => ctx.go(next))}>
          {continueLabel}
        </Button>
      </span>
    </>
  );
}
