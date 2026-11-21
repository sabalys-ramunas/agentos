import { Check, Copy } from 'lucide-react';
import { useId } from 'react';
import type { InputHTMLAttributes, ReactElement, ReactNode, Ref, TextareaHTMLAttributes } from 'react';
import { cloneElement } from 'react';
import { cn } from '@/lib/cn';
import { Button } from './button';
import { useCopy } from './misc';
import { InlineError } from './states';

const control =
  'w-full rounded-md border border-line-strong bg-card text-body text-fg placeholder:text-faint ' +
  'transition-colors duration-(--duration-fast) ease-standard hover:border-control ' +
  'read-only:bg-inset read-only:text-muted read-only:hover:border-line-strong ' +
  'disabled:cursor-not-allowed disabled:border-line disabled:bg-inset disabled:text-disabled ' +
  'aria-invalid:border-bad-dot aria-invalid:hover:border-bad-dot';

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  ref?: Ref<HTMLInputElement>;
  mono?: boolean;
}

export function Input({ className, mono, ...props }: InputProps) {
  return <input className={cn(control, 'h-10 px-3', mono && 'mono', className)} {...props} />;
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(control, 'min-h-20 px-3 py-2', className)} {...props} />;
}

export interface AffixInputProps extends Omit<InputProps, 'prefix'> {
  /** Fixed words before what is typed: an address's host, a currency. Read as part of the field's name. */
  prefix?: ReactNode;
  /** Fixed words after it: a unit ("minutes", "a month"). */
  suffix?: ReactNode;
}

/**
 * A field with fixed words on one side or both, drawn as one control: the words sit in the well, the
 * input on the card, one edge around both. Focus and errors colour the whole edge.
 */
export function AffixInput({ prefix, suffix, className, mono, ...props }: AffixInputProps) {
  const affix = 'flex h-full shrink-0 items-center bg-inset px-3 text-body text-faint select-none';
  return (
    <div
      className={cn(
        'flex h-10 w-full items-stretch overflow-hidden rounded-md border border-line-strong bg-card',
        'transition-colors duration-(--duration-fast) ease-standard hover:border-control',
        'has-[input[aria-invalid=true]]:border-bad-dot has-[input:read-only]:hover:border-line-strong',
        'focus-within:border-(--focus-ring) focus-within:outline-3 focus-within:outline-halo-focus',
        'has-[input[aria-invalid=true]]:focus-within:border-bad-dot has-[input[aria-invalid=true]]:focus-within:outline-halo-bad',
        className,
      )}
    >
      {prefix ? <span className={cn(affix, 'max-w-[45%] border-r border-line')}><span className="truncate">{prefix}</span></span> : null}
      <input
        className={cn(
          'h-full min-w-0 flex-1 bg-transparent px-3 text-body text-fg outline-none placeholder:text-faint read-only:bg-inset read-only:text-muted',
          '[&:focus-visible]:outline-none',
          mono && 'mono',
        )}
        {...props}
      />
      {suffix ? <span className={cn(affix, 'border-l border-line')}>{suffix}</span> : null}
    </div>
  );
}

export function Label({ className, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  // The control is associated by htmlFor at every call site.
  // eslint-disable-next-line jsx-a11y/label-has-associated-control
  return <label className={cn('block text-label text-fg', className)} {...props} />;
}

interface FieldProps {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  /**
   * A field that may be left empty says so after its label. Required fields are the rule and carry no
   * mark (web/DESIGN.md, Forms); pass `required` so a screen reader hears it.
   */
  optional?: boolean;
  required?: boolean;
  /** The card's title already names the field: the label is read, not shown. */
  hideLabel?: boolean;
  /** Quiet words on the label's line, at its far end: a count, "Shown to the agent". */
  aside?: ReactNode;
  /** One input-like element. It receives id and the aria wiring. */
  children: ReactElement<Record<string, unknown>>;
  className?: string;
}

export function Field({ label, hint, error, optional, required, hideLabel, aside, children, className }: FieldProps) {
  const id = useId();
  // The hint stays tied while an error shows, so the field is read with both.
  const describedBy = [error ? `${id}-error` : null, hint && !error ? `${id}-hint` : null].filter(Boolean).join(' ') || undefined;
  return (
    <div className={cn('grid content-start gap-2', className)}>
      <div className={cn('flex min-w-0 items-baseline justify-between gap-3', hideLabel && !aside && 'sr-only')}>
        <Label htmlFor={id} className={hideLabel ? 'sr-only' : undefined}>
          {label}
          {optional ? <span className="ml-1 font-normal text-faint">(optional)</span> : null}
        </Label>
        {aside ? <span className="shrink-0 text-caption text-faint">{aside}</span> : null}
      </div>
      {cloneElement(children, {
        id,
        'aria-describedby': describedBy,
        'aria-invalid': error ? true : undefined,
        'aria-required': required || undefined,
      })}
      {error ? (
        <InlineError id={`${id}-error`}>{error}</InlineError>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-secondary text-faint">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

interface CopyFieldProps {
  value: string;
  /** What it is, for a screen reader and the button: "Invite link". */
  label: string;
  mono?: boolean;
  className?: string;
}

/**
 * Something to copy (web/DESIGN.md, Forms): a read-only field in the well, its text selected on focus, and
 * Copy beside it, which says Copied with a tick for a moment. A phone puts the button under the field.
 */
export function CopyField({ value, label, mono = true, className }: CopyFieldProps) {
  const { copied, copy } = useCopy();
  return (
    <div className={cn('flex flex-col gap-2 sm:flex-row', className)}>
      <Input
        aria-label={label}
        readOnly
        mono={mono}
        value={value}
        className={cn('min-w-0 flex-1 truncate', mono && 'text-code')}
        onFocus={(event) => event.currentTarget.select()}
      />
      <Button icon={copied ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />} onClick={() => copy(value)} aria-label={`Copy ${label.toLowerCase()}`} className="sm:min-w-28">
        {copied ? 'Copied' : 'Copy'}
      </Button>
      <span role="status" className="sr-only">
        {copied ? 'Copied' : ''}
      </span>
    </div>
  );
}
