import { useState } from 'react';
import { useApproveProgram, useEngageProgram, useSetCap } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EngageCapRow } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, Section } from '@/components/ui/card';
import { CodeBlock } from '@/components/ui/misc';
import { Input } from '@/components/ui/field';
import { Status } from '@/components/ui/status';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { Table, Td, Th, Tr, mainCell } from '@/components/ui/table';
import { dateTime } from '@/lib/format';
import { planStatus } from '@/lib/labels';
import { useTechnical } from '@/lib/technical';
import { ENGAGEMENT } from '@/lib/vocabulary';
import { kindName } from './words';

/** The plan an account follows, its daily limits, and its versions. Only the owner changes any of it. */
export function PlanTab({ org, ws, name }: { org: string; ws: string; name: string }) {
  const program = useEngageProgram(org, ws, name);
  const approve = useApproveProgram(org);
  const [technical] = useTechnical();
  const data = program.data;

  if (program.isError) {
    return (
      <Card>
        <ErrorState error={program.error} onRetry={() => void program.refetch()} />
      </Card>
    );
  }
  if (program.isPending) return <Skeleton className="h-64 rounded-xl" />;
  if (!data?.available) {
    return <EmptyState title="Engagement is not on this computer" description="This computer does not run engagement, so no plan can be set here." className="py-16" />;
  }
  if (data.state === null || data.program === null) {
    return (
      <EmptyState
        title="No plan yet"
        description="A plan says what the account does by itself: how many likes, comments and follows, and when. An agent drafts one, and nothing runs until you approve it."
        className="py-16"
      />
    );
  }

  const shown = data.program;
  const approvedBy = shown.approved_by === 'owner' ? 'the owner' : shown.approved_by;
  const approvalError = approve.error instanceof ApiError ? approve.error.message : null;

  return (
    <div className="grid gap-10">
      <Section title={ENGAGEMENT.plan.word}>
        <Card className="grid gap-4 p-4">
          <div className="flex flex-wrap items-center gap-3">
            <Status status={planStatus[data.state]} />
            <span className="text-13 text-muted">
              Version {shown.version}
              {shown.approved_at ? `, approved ${dateTime(shown.approved_at)} by ${approvedBy}` : ', not approved yet'}
            </span>
          </div>
          <p className="text-14">{shown.summary}</p>
          {data.state === 'draft' ? (
            data.may_change ? (
              <div className="grid gap-2">
                <div>
                  <Button variant="primary" loading={approve.isPending} onClick={() => approve.mutate({ id: shown.id, version: shown.version })}>
                    {ENGAGEMENT.approvePlan.word}
                  </Button>
                </div>
                <p className="text-13 text-muted">Nothing runs until you approve it. Each later change needs your approval again.</p>
                {approvalError ? <p role="alert" className="text-13 text-red">{approvalError}</p> : null}
              </div>
            ) : (
              <p className="text-13 text-muted">Only the owner can approve it.</p>
            )
          ) : null}
        </Card>
      </Section>

      <Section title={ENGAGEMENT.limits.word} description="The most it does in a day, and in an hour. Nothing goes above the hard ceiling.">
        <Caps org={org} ws={ws} name={name} caps={data.caps} mayChange={data.may_change} />
      </Section>

      {data.versions.length > 1 ? (
        <Section title="Versions">
          <Card>
            <ul className="divide-y divide-line">
              {data.versions.map((version) => (
                <li key={version.version} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 text-14">
                  <span className="tabular">Version {version.version}</span>
                  <span className="text-13 text-muted">{version.state === 'approved' ? 'Running' : version.state === 'superseded' ? 'Replaced' : 'Not approved'}</span>
                  <span className="ml-auto text-13 text-faint">{dateTime(version.created_at)}</span>
                </li>
              ))}
            </ul>
          </Card>
        </Section>
      ) : null}

      {technical && shown.yaml ? (
        <Section title="Program file">
          <CodeBlock code={shown.yaml} label={`${shown.id}.yaml`} />
        </Section>
      ) : null}
    </div>
  );
}

function Caps({ org, ws, name, caps, mayChange }: { org: string; ws: string; name: string; caps: EngageCapRow[]; mayChange: boolean }) {
  if (!caps.length) return <p className="text-14 text-muted">No limits apply to this account.</p>;
  return (
    <Card className="overflow-x-auto">
      <Table>
        <thead>
          <Tr>
            <Th className={mainCell}>Action</Th>
            <Th className="hidden md:table-cell">Ramp</Th>
            <Th>Daily limit</Th>
            <Th className="hidden md:table-cell">Hard ceiling</Th>
          </Tr>
        </thead>
        <tbody>
          {caps.map((row) => (
            <CapRowView key={row.kind} org={org} ws={ws} name={name} row={row} mayChange={mayChange} />
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

function CapRowView({ org, ws, name, row, mayChange }: { org: string; ws: string; name: string; row: EngageCapRow; mayChange: boolean }) {
  const set = useSetCap(org);
  const [draft, setDraft] = useState(row.manual === null ? '' : String(row.manual));
  const error = set.error instanceof ApiError ? set.error.message : null;
  const value = draft === '' ? null : Number(draft);
  const invalid = value !== null && (!Number.isInteger(value) || value < 0 || value > row.hard_ceiling);
  const save = () => {
    if (value === null || invalid) return;
    set.mutate({ ws, name, kind: row.kind, value });
  };
  const back = () => {
    setDraft('');
    set.mutate({ ws, name, kind: row.kind, value: null });
  };
  return (
    <Tr>
      <Td className={mainCell}>
        <span className="block text-14">{kindName(row.kind)}</span>
        <span className="mt-1 block whitespace-normal text-13 text-muted md:hidden">Ramp {row.careful_start} to {row.careful_ceiling}, hard ceiling {row.hard_ceiling}</span>
      </Td>
      <Td className="hidden md:table-cell">
        <span className="tabular text-13 text-muted">{row.careful_start} to {row.careful_ceiling}</span>
      </Td>
      <Td>
        {mayChange ? (
          <div className="grid w-max gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <Input
                type="number"
                inputMode="numeric"
                min={0}
                max={row.hard_ceiling}
                step={1}
                value={draft}
                placeholder={row.today === null ? 'Ramp' : String(row.today)}
                aria-label={`${kindName(row.kind)} a day`}
                aria-invalid={invalid || undefined}
                onChange={(event) => setDraft(event.target.value)}
                className="tabular w-24 shrink-0"
              />
              <Button size="sm" variant="secondary" className="shrink-0" loading={set.isPending} disabled={value === null || invalid} onClick={save}>
                Save
              </Button>
            </div>
            {row.manual !== null ? (
              <div className="justify-self-start">
                <Button size="sm" variant="ghost" onClick={back}>
                  Back to the ramp
                </Button>
              </div>
            ) : null}
            {invalid ? <p className="text-13 text-red">Pick a whole number from 0 to {row.hard_ceiling}.</p> : null}
            {error ? <p role="alert" className="text-13 text-red">{error}</p> : null}
          </div>
        ) : (
          <span className="tabular text-14">{row.manual ?? `Ramp, ${row.today ?? row.careful_start} today`}</span>
        )}
      </Td>
      <Td className="hidden md:table-cell">
        <span className="tabular text-14">{row.hard_ceiling} a day</span>
        <span className="block text-12 text-faint">{row.hourly} an hour</span>
      </Td>
    </Tr>
  );
}
