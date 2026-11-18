/**
 * How much this account may do in a day or an hour, against what it has done. A limit is a number of posts
 * a person can read; money and model use wait for Technical details. Only the owner sets a limit.
 */
import { useState } from 'react';
import { useAccess, useLaneMeter, useSetLaneLimits } from '@/api/hooks';
import type { Lane, Limits, Meter, MeterLimit } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Field, Input } from '@/components/ui/field';
import { Meter as UsageBar } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { describeError } from '@/lib/errors';
import { usd } from '@/lib/format';
import { useTechnical } from '@/lib/technical';
import { AccountSection } from './account-section';
import { isPlainLimit, limitName, limitRule, limitStatus, limitUsed, resetWords, sortedLimits, wholeOrNull } from './account-words';
import { InlineError } from '@/components/ui/states';

/** The limits a person sees, with the number beside each bar. Shown when a limit is set, or to offer one. */
export function LimitsSection({ org, lane }: { org: string; lane: Lane }) {
  const meter = useLaneMeter(org, lane.workspace, lane.name).data;
  const canChange = useAccess(org).can('lanes.limits');
  const [technical] = useTechnical();
  const [editing, setEditing] = useState(false);
  if (!meter) return null;
  const limits = sortedLimits(meter);
  const plain = limits.filter((limit) => isPlainLimit(limit.name));
  const money = limits.filter((limit) => !isPlainLimit(limit.name));
  if (!plain.length && !canChange && !money.length) return null;
  return (
    <AccountSection
      id="account-limits"
      title="Limits"
      action={
        canChange ? (
          <Button size="sm" onClick={() => setEditing(true)}>
            {limits.length ? 'Change limits' : 'Set a limit'}
          </Button>
        ) : null
      }
    >
      {plain.length ? (
        <ul className="grid gap-4">
          {plain.map((limit) => (
            <LimitRow key={limit.name} limit={limit} meter={meter} />
          ))}
        </ul>
      ) : (
        <p className="text-13 text-muted">No limit on posts. Agents may post as much as the pace and the approvals allow.</p>
      )}
      {plain.length ? <p className="text-13 text-faint">{limitRule(meter)}</p> : null}
      {money.length ? (
        technical ? (
          <LimitMoney limits={money} />
        ) : (
          <p className="text-13 text-faint">Also a spending limit, shown in Technical details.</p>
        )
      ) : null}
      {editing ? <LimitsDialog org={org} lane={lane} meter={meter} onClose={() => setEditing(false)} /> : null}
    </AccountSection>
  );
}

function LimitRow({ limit, meter }: { limit: MeterLimit; meter: Meter }) {
  const status = limitStatus(limit.state);
  return (
    <li className="grid gap-2">
      <div className="flex items-baseline justify-between gap-3 text-13">
        <span className="min-w-0 truncate">{limitName(limit.name)}</span>
        <span className="tabular shrink-0 text-muted">{limitUsed(limit)}</span>
      </div>
      <UsageBar value={limit.used} max={limit.value} label={`${limitName(limit.name)}, ${limitUsed(limit)}`} className="shrink-0" />
      <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-12 text-faint">
        <Status status={{ label: status.word, tone: status.tone }} className="text-muted" />
        {limit.period === 'day' ? <span>{resetWords(limit.resets_at)}</span> : null}
      </p>
      {limit.state === 'cut_off' && meter.explain ? <p className="text-13 text-muted">{meter.explain}</p> : null}
    </li>
  );
}

/** Spending and model use, with Technical details on only. Money and tokens are shown as the node counts them. */
function LimitMoney({ limits }: { limits: MeterLimit[] }) {
  return (
    <ul className="grid gap-3 border-t border-line pt-3">
      {limits.map((limit) => (
        <li key={limit.name} className="flex items-baseline justify-between gap-3 text-13">
          <span className="text-faint">{limitName(limit.name)}</span>
          <span className="tabular mono">{limit.name === 'usd_per_day' ? `${usd(limit.used)} of ${usd(limit.value)}` : `${limit.used} of ${limit.value} tokens`}</span>
        </li>
      ))}
    </ul>
  );
}

/** Posts a day and an hour, as whole numbers. Empty means no limit there. Saving nothing lifts the limits. */
function LimitsDialog({ org, lane, meter, onClose }: { org: string; lane: Lane; meter: Meter; onClose: () => void }) {
  const current = (name: 'commits_per_day' | 'commits_per_hour') => meter.limits.find((limit) => limit.name === name)?.value;
  const [day, setDay] = useState(current('commits_per_day') != null ? String(current('commits_per_day')) : '');
  const [hour, setHour] = useState(current('commits_per_hour') != null ? String(current('commits_per_hour')) : '');
  const [formError, setFormError] = useState<string | null>(null);
  const set = useSetLaneLimits(org, lane.workspace, lane.name);
  const replaces = meter.limits_from !== null && meter.limits_from !== 'owner';

  const save = (limits: Limits | null) => {
    setFormError(null);
    set.mutate(limits, {
      onSuccess: onClose,
      onError: (error) => setFormError(describeError(error)),
    });
  };
  const submit = () => {
    const dayValue = wholeOrNull(day);
    const hourValue = wholeOrNull(hour);
    if ((day.trim() && dayValue === null) || (hour.trim() && hourValue === null)) {
      setFormError('Use whole numbers of zero or more. Leave a box empty for no limit there.');
      return;
    }
    const limits: Limits = {};
    if (dayValue !== null) limits.commits_per_day = dayValue;
    if (hourValue !== null) limits.commits_per_hour = hourValue;
    save(limits);
  };

  return (
    <Dialog
      open
      onOpenChange={(next) => !next && onClose()}
      title="Limits"
      description={`How many posts ${lane.name} may make. At the limit its posts pause until the limit resets. Reads always go on.`}
      footer={
        <>
          {meter.limits_from === 'owner' ? (
            <Button variant="ghost" aria-disabled={set.isPending || undefined} onClick={() => save(null)} className="mr-auto">
              Remove my limits
            </Button>
          ) : null}
          <Button variant="primary" loading={set.isPending} onClick={submit}>
            Save the limits
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
        <Field label="Posts a day" hint="Counted in the account's day. Empty for no limit.">
          <Input type="number" inputMode="numeric" min={0} value={day} onChange={(event) => setDay(event.target.value)} />
        </Field>
        <Field label="Posts an hour" hint="Counted in each clock hour. Empty for no limit.">
          <Input type="number" inputMode="numeric" min={0} value={hour} onChange={(event) => setHour(event.target.value)} />
        </Field>
        {replaces ? <p className="text-13 text-muted">These replace the limits this business sets for its accounts, for this one only.</p> : null}
        {formError ? (
          <InlineError>
            {formError}
          </InlineError>
        ) : null}
      </form>
    </Dialog>
  );
}
