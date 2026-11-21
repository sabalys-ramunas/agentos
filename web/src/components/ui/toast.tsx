/**
 * A toast that can carry one action: for a setting that saves on its own, what changed in plain words and Undo
 * (web/DESIGN.md, Forms). The shell's toast stack (sonner, in app.tsx) shows it and announces it to a screen
 * reader (its list is a polite live region).
 *
 *   save.mutate(next, { onSuccess: () => toastChange({ ...switched('Quiet hours', true), undo: () => save.mutateAsync(before) }) });
 *
 * Undo puts the previous value back through the same save path, so whatever that path asks (a step-up, a
 * check on the node) it asks again. Then the toast says it is back, or plainly that it could not be undone.
 *
 * Never for what cannot be undone (a deletion, a sent invite, a revoked grant): those keep their confirm
 * dialogs. And Undo never loosens what an agent may do without asking: a change that made openajar ask more
 * (Ask me first on, a worker off, a stronger yes) gets the toast with no Undo.
 */
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import { isCancelled } from '@/api/http';
import { describeError } from '@/lib/errors';
import { Button } from './button';

/** How long a toast with Undo stays, while nothing holds it (hover, focus, an undo going on). */
export const UNDO_MS = 6_000;

export interface Change {
  /** What changed, in plain words: "Quiet hours turned on". */
  said: string;
  /** Puts the previous value back through the same save path. Leave it out when there is nothing to undo. */
  undo?: () => Promise<unknown>;
  /** What the toast says once Undo worked: "Quiet hours are off again". */
  undone?: string;
}

/** The words for a switch: "Quiet hours turned on", and after Undo "Quiet hours turned off again". */
export function switched(name: string, on: boolean): Pick<Change, 'said' | 'undone'> {
  return { said: `${name} turned ${on ? 'on' : 'off'}`, undone: `${name} turned ${on ? 'off' : 'on'} again` };
}

/** The words for a choice: "Email digest set to Weekly", and after Undo "Email digest set back to Daily". */
export function chosen(name: string, now: string, before: string): Pick<Change, 'said' | 'undone'> {
  return { said: `${name} set to ${now}`, undone: `${name} set back to ${before}` };
}

type Hold = 'hover' | 'focus' | 'undo';

function ChangeToast({ id, said, undo, undone }: Required<Pick<Change, 'said' | 'undo'>> & { id: string | number; undone?: string }) {
  const [undoing, setUndoing] = useState(false);
  const holds = useRef(new Set<Hold>());
  const left = useRef(UNDO_MS);
  const since = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const run = () => {
    since.current = Date.now();
    timer.current = setTimeout(() => toast.dismiss(id), left.current);
  };
  const hold = (why: Hold) => {
    if (!holds.current.size) {
      clearTimeout(timer.current);
      left.current = Math.max(0, left.current - (Date.now() - since.current));
    }
    holds.current.add(why);
  };
  const release = (why: Hold) => {
    if (!holds.current.delete(why) || holds.current.size) return;
    // Let go of after a long read, it still stays a moment more.
    left.current = Math.max(left.current, 2_000);
    run();
  };
  useEffect(() => {
    since.current = Date.now();
    timer.current = setTimeout(() => toast.dismiss(id), left.current);
    return () => clearTimeout(timer.current);
  }, [id]);

  const putBack = async () => {
    hold('undo');
    setUndoing(true);
    try {
      await undo();
      toast.dismiss(id);
      toast(undone ?? 'Put back as it was');
    } catch (error) {
      toast.dismiss(id);
      // Closing the step-up is the person's own no: still say where it stands.
      toast.error(isCancelled(error) ? 'Not undone, so it stays as it is.' : `Could not undo, so it stays as it is. ${describeError(error)}`);
    }
  };

  return (
    <div
      data-change-toast
      className="flex w-full min-w-0 items-center gap-3"
      onMouseEnter={() => hold('hover')}
      onMouseLeave={() => release('hover')}
      onFocus={() => hold('focus')}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) release('focus');
      }}
    >
      <span className="min-w-0 flex-1 text-pretty">{said}</span>
      <Button size="sm" variant="secondary" loading={undoing} onClick={() => void putBack()} className="shrink-0">
        {undoing ? 'Undoing' : 'Undo'}
      </Button>
    </div>
  );
}

/** Says what changed. With `undo`, it offers Undo for about six seconds, longer while hovered or focused. */
export function toastChange({ said, undo, undone }: Change) {
  if (!undo) return toast(said);
  return toast.custom((id) => <ChangeToast id={id} said={said} undo={undo} undone={undone} />, {
    duration: Infinity,
    // Sonner wraps what it is given in its content and title boxes: they stretch, so Undo sits at the right edge.
    classNames: { content: 'min-w-0 flex-1', title: 'w-full' },
  });
}
