/**
 * Making a standing approval, with every limit on the screen: what it covers, where, how many
 * times, how many a day, and until when. The cloud's own sentence about whether any node applies
 * it sits at the top, before anything is filled in.
 */
import { useState } from 'react';
import { useCreateStandingApproval, useFlows, useStandingSupport, useWorkspaces } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import type { Approval, Effect, NewStandingApproval, StandingApproval } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Field, Input, Textarea } from '@/components/ui/field';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/states';
import { Checkbox } from '@/components/ui/toggle';
import { describeError } from '@/lib/errors';
import { date } from '@/lib/format';
import { effectLabel, laneName } from '@/lib/labels';
import { useRunsAlone } from '@/features/nodes/add-node';
import { standingEffects, standingNote, standsAsTarget, targetWords } from './describe';
import { EnforcementNote } from './enforcement';

const DAY = 86_400_000;
const MAX_DAYS = 90;
const ANY = 'any';

const dayInput = (offsetDays: number) => new Date(Date.now() + offsetDays * DAY).toISOString().slice(0, 10);

/** The end of the chosen day, in the person's own time, so "until the 12th" includes the 12th. */
const endOfDay = (value: string) => new Date(`${value}T23:59:00`).toISOString();

interface Props {
  org: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** "Allow this again": built from an approval the person is looking at. */
  from?: Approval | null;
  onCreated?: (item: StandingApproval) => void;
}

export function StandingDialog({ org, open, onOpenChange, from = null, onCreated }: Props) {
  const workspaces = useWorkspaces(org).data ?? [];
  const flows = useFlows(org).data ?? [];
  const create = useCreateStandingApproval(org);
  const fromWorkspace = from?.lane.split('/')[0] ?? '';
  const [workspace, setWorkspace] = useState(fromWorkspace);
  // A computer on its own takes only a commit (a publish or a send); the hosted service keeps a few more.
  const choices = standingEffects(useRunsAlone(org));
  const [chosenEffect, setEffect] = useState<Effect>(from?.effect ?? 'commit');
  const effect: Effect = choices.includes(chosenEffect) ? chosenEffect : 'commit';
  const [target, setTarget] = useState(ANY);
  const [scope, setScope] = useState<'all' | 'some'>(from ? 'some' : 'all');
  const [lanes, setLanes] = useState<string[]>(from?.lane ? [from.lane] : []);
  const [count, setCount] = useState('10');
  const [perDay, setPerDay] = useState('');
  const [until, setUntil] = useState(dayInput(7));
  const [note, setNote] = useState('');

  const chosen = workspace || workspaces[0]?.id || '';
  const support = useStandingSupport(org, open ? chosen : '');
  const nameOf = (id: string) => workspaces.find((item) => item.id === id)?.name ?? id;
  const laneOptions = workspaces.find((item) => item.id === chosen)?.lanes ?? [];
  // Every routine of this kind in the business, recorded ones too: each names what the approval covers, by its name.
  // Only those a standing approval can match are offered; the note says why the others are not.
  const routines = flows.filter((flow) => flow.lane.startsWith(`${chosen}/`) && flow.effect === effect);
  const targets = routines.filter((flow) => standsAsTarget(flow)).map((flow) => `flow:${flow.id}@${flow.version}`);
  const routineNote = standingNote(routines);
  const names = new Map(flows.map((flow) => [`flow:${flow.id}@${flow.version}`, flow.name ?? flow.id]));
  const targetName = (value: string) => (names.has(value) ? `Routine ${names.get(value)}` : targetWords(value));
  const needsTarget = !from && effect === 'commit';
  const total = Number(count);
  const daily = perDay ? Number(perDay) : null;
  const ends = until ? new Date(endOfDay(until)).getTime() : NaN;
  const tooLong = ends > Date.now() + MAX_DAYS * DAY;
  const invalid =
    !chosen ||
    !Number.isInteger(total) ||
    total < 1 ||
    (daily !== null && (!Number.isInteger(daily) || daily < 1)) ||
    !(ends > Date.now()) ||
    tooLong ||
    (scope === 'some' && lanes.length === 0) ||
    (needsTarget && target === ANY);

  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next) create.reset();
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (invalid) return;
    const body: NewStandingApproval = {
      count: total,
      per_day: daily,
      until: endOfDay(until),
      lanes: scope === 'all' ? null : lanes,
      note: note.trim() || undefined,
    };
    if (from) body.from_approvals = [{ id: from.id, content_hash: from.content_hash }];
    else {
      body.workspace = chosen;
      body.effect = effect;
      body.targets = target === ANY ? [] : [target];
    }
    create.mutate(body, {
      onSuccess: (item) => {
        close(false);
        onCreated?.(item);
      },
    });
  };

  const error = create.isError && !isCancelled(create.error) ? describeError(create.error) : null;

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={from ? 'Allow this again' : 'New standing approval'}
      description={
        from
          ? 'The same routine or tool, with the same content, without asking each time. Anything different still comes to Needs you.'
          : 'Approve one kind of thing ahead of time, a set number of times, until a date.'
      }
      className="sm:max-w-[560px]"
      footer={
        <>
          <Button onClick={() => close(false)}>Cancel</Button>
          <Button variant="primary" type="submit" form="standing-form" disabled={invalid} loading={create.isPending}>
            Create standing approval
          </Button>
        </>
      }
    >
      <form id="standing-form" className="grid gap-6" onSubmit={submit}>
        <div className="rounded-lg border border-line bg-subtle px-4 py-3">
          {support.data ? (
            <>
              <EnforcementNote enforcement={support.data} />
              {effect === 'commit' ? <p className="mt-1 text-13 text-muted">It only limits what asks first. An account or routine that runs on its own does not use it.</p> : null}
            </>
          ) : support.isError ? (
            <p className="text-13 text-muted">Could not check whether a computer applies this. Assume none does, and that whatever asks first still asks each time.</p>
          ) : (
            <Skeleton className="h-5" />
          )}
        </div>

        {from ? (
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-2 text-13">
            <dt className="text-faint">Covers</dt>
            <dd className="text-fg">{effectLabel[from.effect]}</dd>
            <dt className="text-faint">With</dt>
            <dd className="mono break-all text-fg">{from.flow_target ? targetWords(from.flow_target) : `tool ${from.tool}`}</dd>
            <dt className="text-faint">Content</dt>
            <dd className="text-fg">{from.flow_target && !from.tool ? 'This routine version, with these exact contents' : 'These exact contents'}</dd>
          </dl>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Workspace">
              <Select value={chosen} onValueChange={(value) => { setWorkspace(value); setLanes([]); setTarget(ANY); }} options={workspaces.map((item) => ({ value: item.id, label: item.name }))} />
            </Field>
            <Field label="Covers" hint={choices.length > 1 ? 'Payments, irreversible actions and base images are always decided by a person.' : 'On this computer only publishing and sending can be allowed ahead. Everything else is decided by a person each time.'}>
              {choices.length > 1 ? (
                <Select
                  value={effect}
                  onValueChange={(value) => {
                    setEffect(value as Effect);
                    setTarget(ANY);
                  }}
                  options={choices.map((value) => ({ value, label: effectLabel[value] }))}
                />
              ) : (
                <p className="flex h-9 items-center text-14 text-fg">{effectLabel[effect]}</p>
              )}
            </Field>
            <Field
              label="Flow"
              className="sm:col-span-2"
              hint={[needsTarget ? 'Publishing is only covered for one routine version. A change to the routine asks again.' : 'Leave as any to cover every routine and tool of this kind.', routineNote].filter(Boolean).join(' ')}
            >
              <Select
                value={needsTarget && target === ANY ? '' : target}
                onValueChange={setTarget}
                placeholder={targets.length ? 'Choose a routine' : 'No routine of this kind in this workspace'}
                options={[...(needsTarget ? [] : [{ value: ANY, label: 'Any routine or tool' }]), ...targets.map((value) => ({ value, label: targetName(value) }))]}
              />
            </Field>
          </div>
        )}

        <fieldset className="grid gap-2">
          <legend className="mb-2 text-13 font-medium">Lanes</legend>
          <label className="flex items-center gap-3 text-14">
            <input type="radio" name="scope" className="size-4 accent-[var(--foreground)]" checked={scope === 'all'} onChange={() => setScope('all')} />
            Every lane in {nameOf(chosen)}
          </label>
          <label className="flex items-center gap-3 text-14">
            <input type="radio" name="scope" className="size-4 accent-[var(--foreground)]" checked={scope === 'some'} onChange={() => setScope('some')} />
            Only the lanes I choose
          </label>
          {scope === 'some' ? (
            <div className="mt-2 grid gap-2 pl-7 sm:grid-cols-2">
              {laneOptions.map((lane) => (
                <label key={lane.id} className="flex items-center gap-2 text-13">
                  <Checkbox
                    checked={lanes.includes(lane.id)}
                    onCheckedChange={(checked) => setLanes((current) => (checked === true ? [...current, lane.id] : current.filter((item) => item !== lane.id)))}
                  />
                  {laneName(lane.name)}
                </label>
              ))}
            </div>
          ) : null}
        </fieldset>

        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Times in total">
            <Input type="number" inputMode="numeric" min={1} max={1000} className="mono tabular" value={count} onChange={(event) => setCount(event.target.value)} />
          </Field>
          <Field label="At most a day" hint="Optional">
            <Input type="number" inputMode="numeric" min={1} className="mono tabular" placeholder="No limit" value={perDay} onChange={(event) => setPerDay(event.target.value)} />
          </Field>
          <Field label="Ends after" error={tooLong ? `${MAX_DAYS} days at most` : null}>
            <Input type="date" className="mono tabular" min={dayInput(0)} max={dayInput(MAX_DAYS - 1)} value={until} onChange={(event) => setUntil(event.target.value)} />
          </Field>
        </div>
        <Field label="Note" hint="Why, for whoever reads the audit log later. Optional.">
          <Textarea rows={2} maxLength={300} value={note} onChange={(event) => setNote(event.target.value)} />
        </Field>
        {!invalid ? (
          <p className="text-13 text-muted">
            Up to {total} {total === 1 ? 'time' : 'times'}
            {daily ? `, at most ${daily} a day,` : ''} until the end of {date(endOfDay(until))}, then it stops by itself. You can revoke it at any time.
          </p>
        ) : null}
        {error ? (
          <p role="alert" className="text-13 text-red">
            {error}
          </p>
        ) : null}
      </form>
    </Dialog>
  );
}
