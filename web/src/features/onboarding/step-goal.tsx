/**
 * Step 1: what should your AI do first? (startup.md, `#s1`). Purpose before plumbing: a goal and
 * who it is for. The organization and the business are made silently from the name, with an
 * address the cloud picks (Settings can change it).
 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate } from '@tanstack/react-router';
import { useState } from 'react';
import { fleet, orgs } from '@/api/client';
import { useCreateOrg, useSession } from '@/api/hooks';
import { ApiError } from '@/api/http';
import { keys } from '@/api/keys';
import type { GoalKey } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ChoiceGroup, Choice } from '@/components/ui/choice';
import { Input, Textarea } from '@/components/ui/field';
import { SegmentedControl } from '@/components/ui/tabs';
import { describeError } from '@/lib/errors';
import { slugify } from '@/lib/format';
import { GOALS, goalOf } from './goals';
import { SetupLayout } from './layout';
import { remember, useSetup } from './setup';
import { InlineError } from '@/components/ui/states';

type Who = 'business' | 'personal';

const PERSONAL = 'Personal';

function WhoChoice({ who, onChange }: { who: Who; onChange: (who: Who) => void }) {
  return (
    <SegmentedControl
      label="Who is this for?"
      size="md"
      value={who}
      onValueChange={onChange}
      options={[
        { value: 'business', label: 'A business' },
        { value: 'personal', label: 'Just me' },
      ]}
    />
  );
}

function GoalCards({ goal, onChange }: { goal: GoalKey | null; onChange: (goal: GoalKey) => void }) {
  return (
    <ChoiceGroup label="Its first job" showLabel value={goal} onValueChange={onChange} columns={3}>
      {GOALS.map((item) => (
        <Choice
          key={item.key}
          value={item.key}
          layout="stacked"
          icon={
            <span aria-hidden="true" className="inline-flex size-8 items-center justify-center rounded-md bg-inset text-muted group-data-[state=checked]:bg-accent-soft group-data-[state=checked]:text-accent-text">
              <item.icon className="size-4" />
            </span>
          }
          title={item.title}
          description={item.key === 'other' ? item.example : `\u201c${item.example}\u201d`}
        />
      ))}
    </ChoiceGroup>
  );
}

/**
 * `org` is null on the very first visit: Continue then makes the organization too. With an org
 * it changes the first job, and adds the business if it is new.
 */
export function GoalStep({ org }: { org: string | null }) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const user = useSession().data?.user;
  const createOrg = useCreateOrg();
  const workspaces = useQuery({ queryKey: keys.workspaces(org ?? ''), queryFn: () => fleet.workspaces(org ?? ''), enabled: Boolean(org) });
  const existing = org ? (workspaces.data ?? []) : [];
  const { setup } = useSetup(org ?? '', Boolean(org));
  const current = existing.find((item) => item.id === setup?.workspace) ?? existing[0];

  const [who, setWho] = useState<Who | null>(null);
  const [name, setName] = useState<string | null>(null);
  const [goal, setGoal] = useState<GoalKey | null>(null);
  const [own, setOwn] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // What is saved shows until the person changes it.
  const shownWho: Who = who ?? (current?.name === PERSONAL ? 'personal' : 'business');
  const shownName = name ?? (current && current.name !== PERSONAL ? current.name : '');
  const shownGoal = goal ?? setup?.goal ?? null;
  const shownOwn = own ?? setup?.goal_note ?? '';
  const business = shownWho === 'personal' ? PERSONAL : shownName.trim();
  const ready = Boolean(shownGoal) && business.length > 0;
  const first = user?.name?.split(' ')[0];

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const chosen = goalOf(shownGoal);
    if (!chosen || !business) return;
    setBusy(true);
    setError(null);
    const note = chosen.key === 'other' ? shownOwn.trim() || null : null;
    try {
      let slug = org;
      let workspace: string | null = null;
      if (!slug) {
        // One call: the organization, its first workspace and the goal, with an address the cloud picks.
        const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
        const made = await createOrg.mutateAsync({ name: business, workspace: business, goal: chosen.key, goal_note: note, timezone });
        slug = made.slug;
        workspace = made.workspace ?? null;
      } else {
        await orgs.update(slug, { goal: chosen.key, goal_note: note });
        workspace = existing.find((item) => item.name === business)?.id ?? null;
      }
      if (!workspace) {
        // An older cloud, or a new business in an existing organization.
        const id = slugify(business) || 'personal';
        try {
          workspace = (await fleet.createWorkspace(slug, { id, name: business, goal: chosen.key, goal_note: note })).id;
        } catch (failure) {
          if (!(failure instanceof ApiError && failure.status === 409)) throw failure;
          workspace = id;
        }
      }
      const changed = setup?.goal !== chosen.key;
      remember(slug, {
        goal: chosen.key,
        goal_note: note,
        workspace,
        // A new first job brings its own sites and questions; the same one keeps what was chosen.
        ...(changed || !setup?.sites ? { sites: chosen.sites, asks: chosen.asks } : {}),
        step: 'computer',
      });
      void client.invalidateQueries({ queryKey: keys.workspaces(slug) });
      void client.invalidateQueries({ queryKey: keys.setup(slug) });
      void navigate({ to: '/onboarding/$org/$step', params: { org: slug, step: 'computer' } });
    } catch (failure) {
      setError(describeError(failure));
      setBusy(false);
    }
  };

  return (
    <SetupLayout
      org={org}
      business={business && org ? { name: business, color: current?.color } : business ? { name: business } : null}
      step="goal"
      setup={setup}
      eyebrow={first ? `Welcome, ${first}` : 'Welcome'}
      title="What should your AI do first?"
      description="Pick one to start. You can add more jobs, and more businesses, any time."
      hints={{ goal: goalOf(shownGoal) ? `${goalOf(shownGoal)?.title}${business ? ` for ${business}` : ''}` : undefined }}
      footer={
        <>
          <p className="mr-auto text-secondary text-muted">About 7 minutes. You can stop any time and pick up where you left off.</p>
          <Button type="submit" form="first-job" variant="primary" size="lg" disabled={!ready} loading={busy}>
            Continue
          </Button>
        </>
      }
    >
      <form id="first-job" onSubmit={(event) => void submit(event)} className="grid gap-8">
        <div className="grid gap-3 sm:flex sm:flex-wrap sm:items-center sm:gap-4">
          <span className="text-label text-fg">Who is this for?</span>
          <WhoChoice who={shownWho} onChange={setWho} />
          {shownWho === 'business' ? (
            <Input
              aria-label="Business name"
              placeholder="Aster Coffee"
              autoFocus={!org}
              className="sm:w-72"
              value={shownName}
              onChange={(event) => setName(event.target.value)}
              autoComplete="organization"
            />
          ) : (
            <span className="text-secondary text-muted">Your own accounts, kept as "Personal".</span>
          )}
        </div>
        <div className="grid gap-3">
          <GoalCards goal={shownGoal} onChange={setGoal} />
          {shownGoal === 'other' ? (
            <Textarea
              aria-label="Describe the job in your own words"
              placeholder="Every Friday, check our bookings and send me a summary"
              rows={3}
              value={shownOwn}
              onChange={(event) => setOwn(event.target.value)}
            />
          ) : null}
        </div>
        {error ? (
          <InlineError>
            {error}
          </InlineError>
        ) : null}
      </form>
    </SetupLayout>
  );
}
