/**
 * Nothing waits: calm, and a little proud. The sunrise from the agent's own desktop, small, with one line under it
 * (the one place the Dawn picture sits on a plain page is an empty state).
 */
import { Link } from '@tanstack/react-router';
import type { Approval } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { DawnCrop } from '@/components/ui/states';
import { DecidedChips } from './outcome';

export function AllClear({ org, decided, canDecide }: { org: string; decided: Approval[]; canDecide: boolean }) {
  return (
    <Card role="region" aria-label="All clear" className="flex flex-col items-center gap-4 px-6 py-14 text-center">
      <DawnCrop />
      <div className="grid max-w-md gap-1">
        <h2 className="text-section-title">All clear</h2>
        <p className="text-body text-muted">Nothing is waiting for you. Your routines carry on by themselves, and you will hear from us when something asks you first.</p>
      </div>
      <DecidedChips org={org} items={decided} />
      {canDecide ? (
        <Button asChild variant="ghost">
          <Link to="/$org/settings/notifications" params={{ org }}>
            Get told on your phone
          </Link>
        </Button>
      ) : null}
    </Card>
  );
}
