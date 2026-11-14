/**
 * Step 6: stay in the loop (startup.md, `#s6`). The phone's QR code with pictures of the three
 * things to do, and who else can say yes: an email and a sentence instead of a role name. The
 * invited person gets an email that explains what they will be asked, and one click joins.
 */
import { Smartphone, UserPlus } from 'lucide-react';
import { useState } from 'react';
import { useApprovals, useCreateInvite } from '@/api/hooks';
import type { Role } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/field';
import { StatusDot } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { describeError } from '@/lib/errors';
import { plural } from '@/lib/format';
import type { StepContext } from './context';
import { StepFooter } from './context';
import { SetupLayout } from './layout';
import { DoneRow } from './pictures';
import { itemOf, useRefreshSetup } from './setup';
import { Qr } from './qr';

/** The three things a phone needs, numbered, in the words the phone itself uses. */
export function PhoneSteps({ org }: { org: string }) {
  const url = `${location.origin}/${org}/approve`;
  return (
    <div className="grid gap-4 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-center">
      <div className="justify-self-center rounded-xl border border-line bg-[#fff] p-2">
        <Qr value={url} label="QR code that opens Needs you on your phone" />
      </div>
      <ol className="grid gap-3 text-14">
        {['Point your phone’s camera here', 'Tap Share, then Add to Home Screen', 'Open it from your Home Screen and allow notifications'].map((line, index) => (
          <li key={line} className="flex gap-2">
            <span aria-hidden="true" className="tabular inline-flex size-5 shrink-0 items-center justify-center rounded-full bg-fg text-11 font-medium text-bg">
              {index + 1}
            </span>
            {line}
          </li>
        ))}
      </ol>
    </div>
  );
}

interface Choice {
  role: Role;
  label: (business: string) => string;
  hint: string;
}

const CHOICES: Choice[] = [
  { role: 'approver', label: (business) => `Can approve for ${business}`, hint: 'Says yes or no. Sees nothing else.' },
  { role: 'viewer', label: () => 'Can see everything', hint: 'Sees what it does, with proof. Changes nothing.' },
  { role: 'admin', label: () => 'Can set things up', hint: 'Adds accounts, computers and people.' },
];

export function PeopleStep({ ctx }: { ctx: StepContext }) {
  const { org, setup, workspace, business } = ctx;
  const approvals = useApprovals(org).data ?? [];
  const invite = useCreateInvite(org);
  const refresh = useRefreshSetup(org);
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<Role>('approver');
  const [sent, setSent] = useState<string[]>([]);
  const name = business?.name ?? 'your business';
  const phoneDone = itemOf(setup, 'phone')?.done ?? false;

  const send = (event: React.FormEvent) => {
    event.preventDefault();
    const address = email.trim();
    invite.mutate(
      // An approver sees only this business; the others see the whole organization.
      { email: address, role, workspaces: role === 'admin' || !workspace ? null : [workspace.id] },
      {
        onSuccess: () => {
          refresh();
          setSent([...sent, address]);
          setEmail('');
        },
      },
    );
  };

  const first = email.trim().split('@')[0] || 'They';
  const who = first.charAt(0).toUpperCase() + first.slice(1);

  return (
    <SetupLayout
      org={org}
      business={business}
      step="people"
      setup={setup}
      wide
      title="Stay in the loop, from anywhere"
      description="Most days, agentos needs a few seconds of your time. Get asked on your phone, and choose who else can say yes."
      footer={<StepFooter ctx={ctx} step="people" note="You can do both of these later from Home." later={false} continueLabel="Finish and go to Needs you" />}
    >
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <section aria-labelledby="phone-title" className="grid gap-4 rounded-xl border border-line p-4 sm:p-6">
          <h2 id="phone-title" className="flex items-center gap-2 text-16 font-medium">
            <Smartphone className="size-4 text-faint" aria-hidden="true" />
            Get asked on your phone
          </h2>
          <PhoneSteps org={org} />
          <div role="status" className="flex min-h-12 items-center gap-2 rounded-lg border border-line px-4 py-2 text-14">
            {phoneDone ? (
              <DoneRow>Your phone is set up.</DoneRow>
            ) : approvals.length ? (
              <>
                <StatusDot tone="ok" />
                {plural(approvals.length, 'request')} ready to try on your phone
              </>
            ) : (
              <>
                <StatusDot tone="info" pulse />
                <span className="text-muted">Nothing waits yet. New requests reach your phone too.</span>
              </>
            )}
          </div>
        </section>

        <section aria-labelledby="people-title" className="grid gap-4 rounded-xl border border-line p-4 sm:p-6">
          <div>
            <h2 id="people-title" className="flex items-center gap-2 text-16 font-medium">
              <UserPlus className="size-4 text-faint" aria-hidden="true" />
              Who else can say yes?
            </h2>
            <p className="mt-1 text-14 text-muted">A partner, a manager, anyone you trust. They don't set anything up.</p>
          </div>
          <form onSubmit={send} className="grid gap-3">
            <Field label="Their email" error={invite.isError ? describeError(invite.error) : null}>
              <Input type="email" required placeholder="priya@example.com" className="h-10" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="off" />
            </Field>
            <div role="radiogroup" aria-label="What they can do" className="divide-y divide-line overflow-hidden rounded-xl border border-line">
              {CHOICES.map((choice) => {
                const on = role === choice.role;
                return (
                  <button
                    key={choice.role}
                    type="button"
                    role="radio"
                    aria-checked={on}
                    onClick={() => setRole(choice.role)}
                    className={cn('flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-fill-1', on && 'bg-subtle')}
                  >
                    <span aria-hidden="true" className={cn('inline-flex size-4 shrink-0 rounded-full border', on ? 'border-[5px] border-fg' : 'border-line-strong')} />
                    <span className="min-w-0 flex-1">
                      <span className="block text-14">{choice.label(name)}</span>
                      <span className="block text-13 text-muted">{choice.hint}</span>
                    </span>
                    {choice.role === 'approver' ? <span className="hidden text-12 text-faint sm:inline">Recommended</span> : null}
                  </button>
                );
              })}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <p className="min-w-0 flex-1 text-13 text-muted">
                {email.trim() ? `${who} gets` : 'They get'} an email that explains what they will be asked, and joins with one click.
              </p>
              <Button type="submit" loading={invite.isPending} disabled={!email.trim()}>
                Send invite
              </Button>
            </div>
          </form>
          {sent.length ? (
            <div role="status" className="grid gap-2">
              {sent.map((address) => (
                <DoneRow key={address}>Invite sent to {address}</DoneRow>
              ))}
            </div>
          ) : null}
        </section>
      </div>
    </SetupLayout>
  );
}
