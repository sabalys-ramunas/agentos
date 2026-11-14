import { useId } from 'react';
import type { InputHTMLAttributes, ReactElement, ReactNode, Ref, TextareaHTMLAttributes } from 'react';
import { cloneElement } from 'react';
import { cn } from '@/lib/cn';

const control =
  'w-full rounded-md border border-line-strong bg-card text-14 text-fg placeholder:text-faint ' +
  'transition-colors duration-(--duration-fast) ease-standard hover:border-control ' +
  'disabled:cursor-not-allowed disabled:border-line disabled:bg-inset disabled:text-disabled ' +
  'aria-invalid:border-bad-dot';

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  ref?: Ref<HTMLInputElement>;
  mono?: boolean;
}

export function Input({ className, mono, ...props }: InputProps) {
  return <input className={cn(control, 'h-8 px-3', mono && 'mono', className)} {...props} />;
}

export function Textarea({ className, ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn(control, 'min-h-20 px-3 py-2', className)} {...props} />;
}

export function Label({ className, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) {
  // The control is associated by htmlFor at every call site.
  // eslint-disable-next-line jsx-a11y/label-has-associated-control
  return <label className={cn('block text-13 leading-5 font-medium text-fg', className)} {...props} />;
}

interface FieldProps {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  /** One input-like element. It receives id and the aria wiring. */
  children: ReactElement<Record<string, unknown>>;
  className?: string;
}

export function Field({ label, hint, error, children, className }: FieldProps) {
  const id = useId();
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined;
  return (
    <div className={cn('grid content-start gap-2', className)}>
      <Label htmlFor={id}>{label}</Label>
      {cloneElement(children, {
        id,
        'aria-describedby': describedBy,
        'aria-invalid': error ? true : undefined,
      })}
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-13 text-bad">
          {error}
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-13 text-faint">
          {hint}
        </p>
      ) : null}
    </div>
  );
}
