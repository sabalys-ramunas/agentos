/**
 * The first run: purpose, computer, accounts, AI, a real
 * first approval, then phone and people. Every step can be left and picked up again, and Home's
 * Getting started list carries what is not done yet.
 *
 * `/onboarding` is the first job for someone with no organization yet; `/onboarding/$org/$step`
 * is every step after it. Older step names (`node`, `template`, `agent`, `phone`) still work.
 */
import { Navigate, useParams } from '@tanstack/react-router';
import { useStepContext } from './context';
import type { StepKey } from './layout';
import { STEP_ALIASES, STEPS } from './layout';
import { AccountsStep } from './step-accounts';
import { AiStep } from './step-ai';
import { ComputerStep } from './step-computer';
import { GoalStep } from './step-goal';
import { PeopleStep } from './step-people';
import { TryStep } from './step-try';

/** `/onboarding`: the first screen after sign-up. */
export function CreateOrgStep() {
  return <GoalStep org={null} />;
}

function stepOf(value: string): StepKey | null {
  if (STEPS.some((item) => item.key === value)) return value as StepKey;
  return STEP_ALIASES[value] ?? null;
}

export function OnboardingStep() {
  const { org, step } = useParams({ from: '/onboarding/$org/$step' });
  const ctx = useStepContext(org);
  const key = stepOf(step);
  if (!key) return <Navigate to="/onboarding/$org/$step" params={{ org, step: 'goal' }} replace />;
  if (key !== step) return <Navigate to="/onboarding/$org/$step" params={{ org, step: key }} replace />;
  switch (key) {
    case 'goal':
      return <GoalStep org={org} />;
    case 'computer':
      return <ComputerStep ctx={ctx} />;
    case 'accounts':
      return <AccountsStep ctx={ctx} />;
    case 'ai':
      return <AiStep ctx={ctx} />;
    case 'try':
      return <TryStep ctx={ctx} />;
    case 'people':
      return <PeopleStep ctx={ctx} />;
  }
}
