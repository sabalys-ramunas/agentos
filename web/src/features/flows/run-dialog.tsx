import { useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useRunFlow } from '@/api/hooks';
import type { Flow } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Field, Input, Textarea } from '@/components/ui/field';
import { routineAsk } from '@/lib/ask-first';
import { routineTitle } from './routine-words';

/**
 * What the computer gets: a form holds text, but a routine's schema says number or boolean, and a
 * real node refuses "10" for a number. A field left empty is not sent.
 */
export function paramsOf(fields: Flow['params'], values: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, param] of Object.entries(fields)) {
    const value = values[key];
    if (value === undefined || value === '') continue;
    out[key] = param.type === 'number' ? Number(value) : param.type === 'boolean' ? value.trim().toLowerCase() === 'true' : value;
  }
  return out;
}

const WAITS_FOR_SOMEONE = 'It waits in Needs you until someone who may decide here says yes.';

/**
 * What the person is told once a run has started. A run that waits on a yes (`waiting_approval` with its approval)
 * started by someone who may not decide here waits for someone who may: the node's own answer, not a guess.
 */
export function startedWords(started: { status: string; approval_id?: string | null }, canDecide: boolean): string {
  if (started.status !== 'waiting_approval') return 'Run started';
  if (started.approval_id && !canDecide) return WAITS_FOR_SOMEONE;
  return 'It is waiting for a yes in Needs you';
}

export function RunDialog({ org, flow, onClose }: { org: string; flow: Flow | null; onClose: () => void }) {
  const run = useRunFlow(org);
  // Someone who may not decide here: a posting run waits for a person who may, and says so before and after.
  const canDecide = useAccess(org).can('approvals.decide');
  const [values, setValues] = useState<Record<string, string>>({});
  const fields = Object.entries(flow?.params ?? {});
  const missing = fields.some(([key, param]) => param.required && !values[key]?.trim());
  const ask = flow ? routineAsk(flow, canDecide) : null;

  const submit = () => {
    if (!flow) return;
    run.mutate(
      { id: flow.id, params: paramsOf(flow.params, values) },
      {
        onSuccess: (started) => {
          toast(startedWords(started, canDecide));
          setValues({});
          onClose();
        },
      },
    );
  };

  return (
    <Dialog
      open={flow !== null}
      onOpenChange={(open) => !open && onClose()}
      title="Run routine"
      description={flow ? routineTitle(flow) : undefined}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" disabled={missing} loading={run.isPending} onClick={submit}>
            Run
          </Button>
        </>
      }
    >
      <div className="grid gap-4">
        {fields.map(([key, param]) => (
          <Field key={key} label={param.label ?? key}>
            {param.type === 'number' ? (
              <Input type="number" value={values[key] ?? ''} onChange={(event) => setValues({ ...values, [key]: event.target.value })} />
            ) : (
              <Textarea value={values[key] ?? ''} onChange={(event) => setValues({ ...values, [key]: event.target.value })} />
            )}
          </Field>
        ))}
        <p className="text-secondary text-muted">
          {ask
            ? ask.asks
              ? canDecide ? `${ask.word}: it waits in Needs you for your yes.` : WAITS_FOR_SOMEONE
              : `${ask.word}: nobody needs to say yes.`
            : 'It runs now, and anything that needs you waits in Needs you.'}
        </p>
      </div>
    </Dialog>
  );
}

