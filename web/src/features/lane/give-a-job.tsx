import { useState } from 'react';
import { useStartWorkerThing } from '@/api/hooks';
import type { Lane } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Field, Textarea } from '@/components/ui/field';
import { describeError } from '@/lib/errors';
import { accountName } from '@/features/home/words';

/**
 * A person says what to do, as an agent does. The worker does the steps on this account, and a post
 * still goes by the account's Ask me first. What it does shows in the strip above and in History.
 */
export function GiveAJob({ org, lane }: { org: string; lane: Lane }) {
  const [open, setOpen] = useState(false);
  const [told, setTold] = useState('');
  const start = useStartWorkerThing(org);
  const close = () => {
    setOpen(false);
    setTold('');
    start.reset();
  };
  return (
    <>
      <Button size="sm" className="mt-2 ml-7" onClick={() => setOpen(true)}>Give it a job</Button>
      <Dialog
        open={open}
        onOpenChange={(next) => (next ? setOpen(true) : close())}
        title={`What should it do on ${accountName(lane)}?`}
        description="Say it the way you would tell a person. It starts on the page this account shows now."
        footer={
          <>
            <Button onClick={close}>Don't start</Button>
            <Button variant="primary" loading={start.isPending} disabled={!told.trim()} onClick={() => start.mutate({ lane: lane.id, told }, { onSuccess: close })}>
              Start
            </Button>
          </>
        }
      >
        <Field label="The job" error={start.isError ? describeError(start.error) : null}>
          <Textarea autoFocus rows={4} maxLength={2000} value={told} onChange={(event) => setTold(event.target.value)} />
        </Field>
      </Dialog>
    </>
  );
}
