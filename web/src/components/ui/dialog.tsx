import { Trash2, X } from 'lucide-react';
import { Dialog as Primitive } from 'radix-ui';
import type { ReactNode } from 'react';
import { useId, useLayoutEffect, useRef, useState } from 'react';
import { cn } from '@/lib/cn';
import { Button, IconButton } from './button';
import { Input } from './field';
import { AppTile } from './tile';

/**
 * Radix returns focus to a Dialog.Trigger, and these dialogs open from state, so without this
 * focus falls to <body> on close and a keyboard user is thrown back to the top of the page.
 */
export function useReturnFocus(open: boolean) {
  const previous = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (open) previous.current = document.activeElement as HTMLElement | null;
  }, [open]);
  return (event: Event) => {
    event.preventDefault();
    previous.current?.focus();
  };
}

/**
 * Last in the DOM, so the first field or action takes focus when a dialog opens and Enter
 * does the expected thing. It still sits top right.
 */
function CloseButton() {
  return (
    <Primitive.Close asChild>
      <IconButton label="Close" size="sm" data-dialog-close="" className="absolute top-4 right-4">
        <X className="size-4" />
      </IconButton>
    </Primitive.Close>
  );
}

/** With nothing but the close button to focus, the dialog itself takes focus. */
function focusFirst(event: Event) {
  const root = event.currentTarget as HTMLElement;
  if (!root.querySelector('input, textarea, select, a[href], button:not([data-dialog-close])')) {
    event.preventDefault();
    root.focus();
  }
}

function Overlay({ stepUp }: { stepUp?: boolean }) {
  return (
    <Primitive.Overlay
      data-dialog-overlay=""
      data-step-up-dialog={stepUp || undefined}
      className="fixed inset-0 z-40 bg-overlay data-[state=closed]:animate-fade-out data-[state=open]:animate-fade-in" />
  );
}

interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  className?: string;
  /** Marks the one dialog that may sit above another. See app.css. */
  stepUp?: boolean;
  /** A wrapper tile before the title: amber for a careful question, red for one that deletes for good. */
  tile?: ReactNode;
  /** The dialog that cannot be undone: a red edge. */
  tone?: 'danger';
}

/** Centered on desktop, a bottom sheet on a phone. */
export function Dialog({ open, onOpenChange, title, description, children, footer, className, stepUp, tile, tone }: DialogProps) {
  const returnFocus = useReturnFocus(open);
  return (
    <Primitive.Root open={open} onOpenChange={onOpenChange}>
      <Primitive.Portal>
        <Overlay stepUp={stepUp} />
        <Primitive.Content
          data-step-up-dialog={stepUp || undefined}
          onOpenAutoFocus={focusFirst}
          onCloseAutoFocus={returnFocus}
          className={cn(
            'fixed z-50 flex max-h-[calc(100dvh-32px)] flex-col border bg-raised shadow-dialog outline-none sm:max-h-[calc(88dvh-32px)]',
            tone === 'danger' ? 'border-bad-line' : 'border-line',
            'inset-x-0 bottom-0 rounded-t-2xl data-[state=closed]:animate-sheet-out data-[state=open]:animate-sheet-in',
            'sm:top-[12vh] sm:bottom-auto sm:left-1/2 sm:w-full sm:max-w-[460px] sm:-translate-x-1/2 sm:rounded-[12px]',
            'sm:data-[state=closed]:animate-fade-out sm:data-[state=open]:animate-fade-in',
            className,
          )}
        >
          <div className={cn('flex gap-3 px-6 pt-6 pr-14', tile && 'items-start')}>
            {tile ? <span className="shrink-0">{tile}</span> : null}
            <div className="min-w-0">
              <Primitive.Title className={cn('text-section-title', tile && 'flex min-h-7 items-center')}>{title}</Primitive.Title>
              {description ? (
                <Primitive.Description className="mt-2 text-body text-muted">{description}</Primitive.Description>
              ) : (
                <Primitive.Description className="sr-only">{title}</Primitive.Description>
              )}
            </div>
          </div>
          {children ? <div className="min-h-0 overflow-y-auto px-6 pt-5 pb-2">{children}</div> : null}
          {footer ? (
            <div className="safe-bottom">
              <div className="flex flex-wrap items-center justify-end gap-2 px-6 pt-5 pb-6">{footer}</div>
            </div>
          ) : (
            <div className="h-6" />
          )}
          <CloseButton />
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}

interface DrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children: ReactNode;
}

/** Inspect drawers: where the plumbing lives, hidden by default. */
export function Drawer({ open, onOpenChange, title, description, children }: DrawerProps) {
  const returnFocus = useReturnFocus(open);
  return (
    <Primitive.Root open={open} onOpenChange={onOpenChange}>
      <Primitive.Portal>
        <Overlay />
        <Primitive.Content
          onOpenAutoFocus={focusFirst}
          onCloseAutoFocus={returnFocus}
          className="fixed inset-y-0 right-0 z-50 flex w-full max-w-[480px] flex-col border-l border-line bg-raised shadow-dialog outline-none data-[state=closed]:animate-drawer-out data-[state=open]:animate-drawer-in"
        >
          <div className="border-b border-line py-4 pr-12 pl-6">
            <Primitive.Title className="truncate text-section-title">{title}</Primitive.Title>
            {description ? (
              <Primitive.Description className="mt-1 text-secondary text-muted">{description}</Primitive.Description>
            ) : (
              <Primitive.Description className="sr-only">{title}</Primitive.Description>
            )}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="safe-bottom">
              <div className="px-6 py-4">{children}</div>
            </div>
          </div>
          <CloseButton />
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}

interface SidePanelProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
}

function useWide(query = '(min-width: 1024px)') {
  const [wide, setWide] = useState(() => typeof window !== 'undefined' && window.matchMedia?.(query).matches === true);
  useLayoutEffect(() => {
    const list = window.matchMedia?.(query);
    if (!list) return;
    const update = () => setWide(list.matches);
    update();
    list.addEventListener('change', update);
    return () => list.removeEventListener('change', update);
  }, [query]);
  return wide;
}

/**
 * A side panel: the detail of a picked row beside the list (a machine beside the machines), on the
 * page, not over it. From 1024 px it is a card that sits in the layout and the page stays usable;
 * under that it opens as a Drawer. Esc and the close button close it.
 */
export function SidePanel({ open, onOpenChange, title, description, children, className }: SidePanelProps) {
  const wide = useWide();
  const headingId = useId();
  useLayoutEffect(() => {
    if (!open || !wide) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) onOpenChange(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, wide, onOpenChange]);
  if (!wide) {
    return (
      <Drawer open={open} onOpenChange={onOpenChange} title={title} description={description}>
        {children}
      </Drawer>
    );
  }
  if (!open) return null;
  return (
    <aside
      aria-labelledby={headingId}
      className={cn('sticky top-[calc(var(--topbar-height)+16px)] flex max-h-[calc(100dvh-var(--topbar-height)-32px)] min-w-0 flex-col rounded-xl border border-line bg-card shadow-lift', className)}
    >
      <div className="relative border-b border-line py-4 pr-12 pl-5">
        <h2 id={headingId} className="truncate text-section-title">
          {title}
        </h2>
        {description ? <p className="mt-1 text-secondary text-muted">{description}</p> : null}
        <IconButton label="Close" size="sm" className="absolute top-3 right-3" onClick={() => onOpenChange(false)}>
          <X className="size-4" />
        </IconButton>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
    </aside>
  );
}

interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  loading?: boolean;
  danger?: boolean;
  /** When set, the person has to type this exact text first. */
  typeToConfirm?: string;
  /** Holds the confirm button until what the dialog asks for is there, such as a required reason. */
  confirmDisabled?: boolean;
  /** The word on the way out, when "Cancel" would be unclear ("Stay", "Keep it going"). */
  cancelLabel?: string;
  /** A wrapper tile before the title. A dangerous confirm with `typeToConfirm` gets the red one by itself. */
  tile?: ReactNode;
  /** Anything a decision needs beside its words: a list of what happens, an optional reason. */
  children?: ReactNode;
}

export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  onConfirm,
  loading,
  danger,
  typeToConfirm,
  confirmDisabled = false,
  cancelLabel = 'Cancel',
  tile,
  children,
}: ConfirmDialogProps) {
  const [typed, setTyped] = useState('');
  const fieldId = useId();
  const typing = typeToConfirm !== undefined;
  const matched = !typing || typed.trim() === typeToConfirm;
  const blocked = confirmDisabled || !matched;
  // Deleting for good: the red tile, the red edge, and the one red fill in the product, only once the name matches.
  const forGood = danger && typing;
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setTyped('');
        onOpenChange(next);
      }}
      title={title}
      description={description}
      tone={forGood ? 'danger' : undefined}
      tile={tile ?? (forGood ? <AppTile tone="red" icon={Trash2} size="sm" /> : undefined)}
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            {cancelLabel}
          </Button>
          <Button
            variant={danger ? 'danger' : 'primary'}
            loading={loading}
            disabled={blocked}
            onClick={onConfirm}
            className={cn(forGood && 'border-transparent bg-(--status-bad-text) text-on-inverse shadow-none hover:border-transparent hover:bg-(--status-bad-text) hover:opacity-90 off:border-transparent off:bg-(--status-bad-text) off:text-on-inverse off:opacity-45')}
          >
            {confirmLabel}
          </Button>
          {typing && !matched ? <p className="w-full text-right text-secondary text-faint">The button wakes up when the name matches.</p> : null}
        </>
      }
    >
      {children ? <div className={cn(typing && 'mb-4')}>{children}</div> : null}
      {typing ? (
        <div className="grid gap-2">
          <label htmlFor={fieldId} className="text-label text-fg">
            Type <span className="font-semibold">{typeToConfirm}</span> to confirm
          </label>
          <Input id={fieldId} value={typed} onChange={(event) => setTyped(event.target.value)} autoComplete="off" spellCheck={false} autoCapitalize="none" />
        </div>
      ) : null}
    </Dialog>
  );
}
