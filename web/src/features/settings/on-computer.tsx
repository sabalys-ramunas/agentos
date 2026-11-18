import { ArrowUpRight } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { toast } from 'sonner';
import { useFormTrip, useStartForm } from '@/api/hooks';
import type { FormTrip, StartFormKind } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { InlineError } from '@/components/ui/states';

/** Plain words for a form on the computer: it is open, done, or it ran out. Nothing here shows the value. */
export const FORM_WORDS: Record<FormTrip['stage'], string> = {
  waiting: 'Waiting for you in the new tab. This page updates when it is done.',
  done: 'Done.',
  ended: 'That form ended before it was used. Start it again.',
};

/** A form the computer serves opens in a new tab. The tab is opened on the click, so a blocker lets it through. */
export function useFormOnComputer(org: string) {
  const start = useStartForm(org);
  const [trip, setTrip] = useState<string | undefined>();
  const [link, setLink] = useState<string | null>(null);
  const status = useFormTrip(org, trip);
  const open = async (kind: StartFormKind, fields?: Record<string, string>) => {
    const tab = window.open('', '_blank');
    const made = await start.mutateAsync({ kind, fields });
    setTrip(made.id);
    setLink(made.form_url);
    if (tab && made.form_url) tab.location.href = made.form_url;
    else if (tab) tab.close();
    return made;
  };
  return {
    open,
    pending: start.isPending,
    failed: start.isError,
    stage: status.data?.stage ?? (trip ? 'waiting' : undefined),
    link,
    reset: () => {
      setTrip(undefined);
      setLink(null);
    },
  };
}

/**
 * One step that only opens a form on the computer: say what it is for, then open it. The dialog closes
 * when the form is done, and keeps its link while it is open, in case the tab was closed.
 */
export function FormDialog({
  org,
  open,
  onOpenChange,
  title,
  description,
  kind,
  fields,
  confirmLabel,
  doneWords,
  children,
}: {
  org: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  kind: StartFormKind;
  fields?: Record<string, string>;
  confirmLabel: string;
  doneWords: string;
  /** Plain fields the person types here, before the form opens on the computer. Never a secret. */
  children?: ReactNode;
}) {
  const form = useFormOnComputer(org);
  const done = form.stage === 'done';
  useEffect(() => {
    if (!done) return;
    toast(doneWords);
    onOpenChange(false);
  }, [done, doneWords, onOpenChange]);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) form.reset();
        onOpenChange(next);
      }}
      title={title}
      description={description}
      footer={
        form.link ? (
          <Button asChild>
            <a href={form.link} target="_blank" rel="noreferrer">
              Open the page again
              <ArrowUpRight aria-hidden="true" className="size-4" />
            </a>
          </Button>
        ) : (
          <>
            <Button onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button variant="primary" loading={form.pending} onClick={() => void form.open(kind, fields)}>
              {confirmLabel}
            </Button>
          </>
        )
      }
    >
      {form.link ? null : children}
      {form.failed ? <InlineError>That did not start. Nothing was changed.</InlineError> : null}
      {form.link ? <p className="text-14 text-muted" role="status">{FORM_WORDS[form.stage ?? 'waiting']}</p> : null}
    </Dialog>
  );
}
