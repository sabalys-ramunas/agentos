/**
 * How a form saves (web/DESIGN.md, Forms): Save stays dim until something changes, shows that it is
 * working while it saves, and then says Saved in its own footer for a moment, where the eye already is.
 */
import { Check } from 'lucide-react';
import { cloneElement, useCallback, useEffect, useId, useRef, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { Button } from './button';

/**
 * Three seconds of "Saved" after a save went through (states-forms: the button and the note both say so).
 * `flash()` starts it; a new edit calls `clear()`.
 */
export function useSaved(ms = 3000) {
  const [savedAt, setSavedAt] = useState<Date | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const flash = useCallback(() => {
    setSavedAt(new Date());
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setSavedAt(null), ms);
  }, [ms]);
  const clear = useCallback(() => {
    clearTimeout(timer.current);
    setSavedAt(null);
  }, []);
  return { saved: savedAt !== null, savedAt, flash, clear };
}

interface FormNoteProps {
  /** The card's own line about its value ("Up to 80 characters."). Shown at rest and while editing. */
  hint?: ReactNode;
  dirty?: boolean;
  saving?: boolean;
  savedAt?: Date | null;
  /** How many fields still need fixing, once the person has been told. */
  problems?: number;
}

/**
 * The footer's note: it says where the form is. Its own hint at rest ("Nothing changed yet" when it has
 * none), "2 things to fix" in red, "Saving your changes", then "Saved at 10:42" for three seconds.
 */
export function FormNote({ hint, dirty = false, saving = false, savedAt = null, problems = 0 }: FormNoteProps) {
  let body: ReactNode = hint ?? (dirty ? null : 'Nothing changed yet');
  let tone = 'text-faint';
  if (saving) body = 'Saving your changes';
  else if (problems > 0) {
    body = problems === 1 ? '1 thing to fix' : `${problems} things to fix`;
    tone = 'text-bad';
  } else if (savedAt && !dirty) body = `Saved at ${savedAt.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}`;
  return (
    <span role="status" className={cn('block', tone)}>
      {body}
    </span>
  );
}

interface FormActionsProps {
  /** Something changed since the last save. Save is dim until it has, then it is the violet primary. */
  dirty: boolean;
  saving?: boolean;
  /** Shows "Saved" with a tick on the button for three seconds (from `useSaved`). */
  saved?: boolean;
  /** Puts the fields back as they were. Shown only while something changed. */
  onDiscard?: () => void;
  /** A save that is not possible yet: Save stays dim even though something changed. */
  blocked?: boolean;
  label?: string;
  /** Save is a submit button; pass a handler instead when the form is not a `<form>`. */
  onSave?: () => void;
  className?: string;
}

/** The footer's actions: Discard (while edited) and Save, which says Saving and then Saved. */
export function FormActions({ dirty, saving = false, saved = false, onDiscard, blocked = false, label = 'Save', onSave, className }: FormActionsProps) {
  const done = saved && !dirty && !saving;
  return (
    <div className={cn('flex items-center gap-2', className)}>
      {dirty && onDiscard && !saving ? (
        <Button size="sm" variant="ghost" onClick={onDiscard}>
          Discard
        </Button>
      ) : null}
      {done ? (
        // Not a button any more for a moment: there is nothing to press, and it must not look pressable.
        <span className="inline-flex h-8 items-center gap-1 rounded-full border border-line-strong bg-card px-3 text-secondary font-medium text-ok shadow-lift md:h-(--control-height-sm)">
          <Check aria-hidden className="size-4" />
          Saved
        </span>
      ) : (
        <Button
          size="sm"
          variant={dirty && !blocked ? 'primary' : 'secondary'}
          type={onSave ? 'button' : 'submit'}
          onClick={onSave}
          loading={saving}
          disabled={!dirty || blocked}
        >
          {saving ? 'Saving' : label}
        </Button>
      )}
    </div>
  );
}

interface SettingRowProps {
  label: string;
  hint?: ReactNode;
  /** One control: a switch, a select, a button. It receives the id the label points at, and the hint. */
  children: ReactElement<Record<string, unknown>>;
  className?: string;
}

/**
 * One setting in a list of them (web/DESIGN.md, Forms): its name and one line under it on the left, its
 * control on the right. The name is the control's label, so a click on it works the switch.
 */
export function SettingRow({ label, hint, children, className }: SettingRowProps) {
  const id = useId();
  return (
    <div className={cn('flex items-center justify-between gap-6 py-4 first:pt-0 last:pb-0', className)}>
      <div className="min-w-0">
        <label htmlFor={id} className="block text-label text-fg">
          {label}
        </label>
        {hint ? (
          <p id={`${id}-hint`} className="mt-1 text-secondary text-faint">
            {hint}
          </p>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center">{cloneElement(children, { id, 'aria-describedby': hint ? `${id}-hint` : undefined })}</div>
    </div>
  );
}

/**
 * The quiet line a form that saves on its own shows in its footer: Saving, or what went wrong. Saved is not
 * said here: the toast says what changed, with Undo (`toastChange` in toast.tsx, web/DESIGN.md, Forms).
 */
export function SaveStatus({ state, error }: { state: 'idle' | 'pending' | 'success' | 'error'; error?: ReactNode }) {
  return (
    <span role="status" className={cn('flex min-h-5 items-center gap-1 text-secondary', state === 'error' ? 'text-bad' : 'text-faint')}>
      {state === 'pending' ? 'Saving' : null}
      {state === 'error' ? error : null}
    </span>
  );
}
