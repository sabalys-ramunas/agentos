/**
 * How fast this account may act, and the business clock its quiet hours are read on. Shown when a pace is
 * set or holds the account back. An owner who has not set one is offered it. Only the owner changes it.
 */
import { useState } from 'react';
import { useAccess, useLanePace, useSetLanePace, useSetWorkspaceZone, useWorkspaceZone } from '@/api/hooks';
import type { Lane, LanePace, Pace } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Field, Input } from '@/components/ui/field';
import { describeError } from '@/lib/errors';
import { AccountSection, Answer } from './account-section';
import { laneTimeWords, nextPostWords, paceSentence, paceStatus, postsWords, quietHoursWords, wholeOrNull, windowText } from './account-words';
import { InlineError } from '@/components/ui/states';

const RECOMMENDED = 'between 08:00 and 22:00, 45 minutes apart, 3 posts a day';

export function PaceSection({ org, lane }: { org: string; lane: Lane }) {
  const pace = useLanePace(org, lane.workspace, lane.name).data;
  const canChange = useAccess(org).can('lanes.pace');
  const [editing, setEditing] = useState(false);
  if (!pace || (!pace.paced && !canChange)) return null;
  const status = paceStatus(pace);
  const quiet = quietHoursWords(pace.pace?.active_hours);
  const next = nextPostWords(pace);
  return (
    <AccountSection
      id="account-pace"
      title="Posting pace"
      action={
        canChange ? (
          <Button size="sm" onClick={() => setEditing(true)}>
            {pace.paced ? 'Change pace' : 'Set a pace'}
          </Button>
        ) : null
      }
    >
      <Answer tone={status.tone} word={status.word}>
        {pace.paced ? paceSentence(pace.pace) : 'Not paced: agents may post at any hour. A pace holds them to set hours and a number of posts.'}
      </Answer>
      {pace.paced ? (
        <div className="grid gap-1 text-secondary text-faint">
          <p className="text-muted">{postsWords(pace)}</p>
          {quiet ? <p>{quiet}</p> : null}
          <p>{laneTimeWords(pace)}</p>
          {next && !pace.may_post_now ? <p className="text-muted">{next}</p> : null}
        </div>
      ) : null}
      {editing ? <PaceDialog org={org} lane={lane} pace={pace} onClose={() => setEditing(false)} /> : null}
    </AccountSection>
  );
}

type Built = Pace | null | 'invalid';

/** Waking hours, the gap and the posts a day, as a person says them. Only the owner can open this. */
function PaceDialog({ org, lane, pace, onClose }: { org: string; lane: Lane; pace: LanePace; onClose: () => void }) {
  const current = pace.pace ?? {};
  const [hoursFrom, hoursTo] = ((current.active_hours ?? [])[0] ?? '').split('-');
  const [from, setFrom] = useState(hoursFrom ?? '');
  const [to, setTo] = useState(hoursTo ?? '');
  const [gap, setGap] = useState(current.min_gap_minutes != null ? String(current.min_gap_minutes) : '');
  const [perDay, setPerDay] = useState(current.commits_per_day != null ? String(current.commits_per_day) : '');
  const zone = useWorkspaceZone(org, lane.workspace).data?.timezone ?? pace.workspace_timezone ?? null;
  const [clock, setClock] = useState(zone ?? '');
  const [formError, setFormError] = useState<string | null>(null);
  const setPace = useSetLanePace(org, lane.workspace, lane.name);
  const setZone = useSetWorkspaceZone(org, lane.workspace);
  const busy = setPace.isPending || setZone.isPending;

  const built = (): Built => {
    const out: Pace = {};
    const typedHours = from.trim() !== '' || to.trim() !== '';
    const hours = typedHours ? windowText(from.trim(), to.trim()) : null;
    if (typedHours && !hours) return 'invalid';
    if (hours) out.active_hours = [hours];
    if (gap.trim()) {
      const minutes = wholeOrNull(gap);
      if (minutes === null) return 'invalid';
      out.min_gap_minutes = minutes;
    }
    if (perDay.trim()) {
      const posts = wholeOrNull(perDay);
      if (posts === null || posts < 1) return 'invalid';
      out.commits_per_day = posts;
    }
    return Object.keys(out).length ? out : null;
  };

  const save = async (body: { pace: Pace | null } | { preset: true }, nextClock: string | null = clock.trim() || null) => {
    setFormError(null);
    try {
      await setPace.mutateAsync(body);
      if (nextClock !== zone) await setZone.mutateAsync(nextClock);
      onClose();
    } catch (error) {
      setFormError(describeError(error));
    }
  };

  const submit = () => {
    const body = built();
    if (body === 'invalid') {
      setFormError('Give both times for the hours, and whole numbers for the gap and the posts.');
      return;
    }
    void save({ pace: body });
  };

  return (
    <Dialog
      open
      onOpenChange={(next) => !next && onClose()}
      title="Posting pace"
      description={`How fast ${lane.name} may post. A post outside this pace is refused, whatever was approved.`}
      footer={
        <>
          {pace.paced ? (
            <Button variant="ghost" aria-disabled={busy || undefined} onClick={() => void save({ pace: null }, zone)} className="mr-auto">
              Remove the pace
            </Button>
          ) : null}
          <Button aria-disabled={busy || undefined} onClick={() => void save({ preset: true }, zone)}>
            Use the recommended
          </Button>
          <Button variant="primary" loading={busy} onClick={submit}>
            Save the pace
          </Button>
        </>
      }
    >
      <form
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <p className="text-secondary text-muted">Recommended for a new account: {RECOMMENDED}.</p>
        <div className="grid gap-2">
          <span className="text-secondary font-medium">Posts go out</span>
          <div className="flex items-center gap-2">
            <Input type="time" aria-label="Posts go out from" value={from} onChange={(event) => setFrom(event.target.value)} className="w-32" />
            <span className="text-secondary text-muted">to</span>
            <Input type="time" aria-label="Posts stop at" value={to} onChange={(event) => setTo(event.target.value)} className="w-32" />
          </div>
          <p className="text-secondary text-faint">In the account&apos;s own time. Leave both empty to post at any hour.</p>
        </div>
        <Field label="Minutes between posts" hint="The least gap between two posts. Empty for no gap.">
          <Input type="number" inputMode="numeric" min={0} value={gap} onChange={(event) => setGap(event.target.value)} />
        </Field>
        <Field label="Posts a day" hint="Counted from midnight to midnight in the account's time. Empty for no limit.">
          <Input type="number" inputMode="numeric" min={1} value={perDay} onChange={(event) => setPerDay(event.target.value)} />
        </Field>
        <Field label="Business clock" hint={`Hours are read on this time zone, such as Europe/Vilnius. Empty uses ${pace.zone.source_words}.`}>
          <Input mono value={clock} placeholder={pace.zone.timezone} onChange={(event) => setClock(event.target.value)} autoComplete="off" />
        </Field>
        {formError ? (
          <InlineError>
            {formError}
          </InlineError>
        ) : null}
      </form>
    </Dialog>
  );
}
