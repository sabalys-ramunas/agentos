/**
 * A text field that can hold data from earlier steps. Data shows as a named chip, never as
 * `{{curly.braces}}`, and the value underneath stays plain text with references in it. The DOM is
 * built with text nodes only, so nothing a computer or a stranger wrote can become markup.
 */
import { Braces, ShieldAlert } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent, MouseEvent } from 'react';
import { Label } from '@/components/ui/field';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/menu';
import { cn } from '@/lib/cn';
import { piecesOf, refName, refOf } from '@/lib/routines';
import type { DataSource } from '@/lib/routines';

/** A named chip for a reference, with a button that takes it out (Backspace and Delete do too, beside it). */
function chip(ref: string, name: string, untrusted: boolean): HTMLElement {
  const node = document.createElement('span');
  node.contentEditable = 'false';
  node.dataset.ref = ref;
  node.className = `mx-px inline-flex h-5 items-center gap-1 rounded-sm border px-1 align-baseline text-12 font-medium ${untrusted ? 'border-(--wait-line) bg-fill-1' : 'border-line bg-fill-2'}`;
  const label = document.createElement('span');
  label.textContent = name;
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.dataset.remove = '';
  remove.setAttribute('aria-label', `Remove ${name}`);
  remove.className = 'grid size-4 place-items-center rounded-sm text-faint hover:text-fg';
  remove.textContent = '×';
  node.append(label, remove);
  return node;
}

/** The chip a caret touches on one side, if there is one: the caret must sit right against it, with nothing selected. */
function chipBeside(root: HTMLElement, side: 'before' | 'after'): HTMLElement | null {
  const selection = window.getSelection();
  if (!selection?.rangeCount || !selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer)) return null;
  const { startContainer: at, startOffset: offset } = range;
  let next: Node | null;
  if (at.nodeType === Node.TEXT_NODE) {
    if (side === 'before' ? offset > 0 : offset < (at.textContent ?? '').length) return null;
    next = side === 'before' ? at.previousSibling : at.nextSibling;
  } else {
    next = side === 'before' ? (at.childNodes[offset - 1] ?? null) : (at.childNodes[offset] ?? null);
  }
  return next instanceof HTMLElement && next.dataset.ref !== undefined ? next : null;
}

function textOf(root: HTMLElement): string {
  let out = '';
  root.childNodes.forEach((node, index) => {
    if (node.nodeType === Node.TEXT_NODE) out += node.textContent ?? '';
    else if (node instanceof HTMLElement && node.dataset.ref) out += node.dataset.ref;
    else if (node instanceof HTMLBRElement) out += '\n';
    else if (node instanceof HTMLElement) out += `${index ? '\n' : ''}${textOf(node)}`;
  });
  return out;
}

const untrustedField = (sources: DataSource[], step: string, field: string) => Boolean(sources.find((item) => item.step === step)?.fields.find((item) => item.key === field)?.untrusted);

/** The text with every reference that no longer points at data a step can read taken out, so nothing is left dangling. */
export function withoutDeadRefs(text: string, sources: DataSource[]): string {
  return piecesOf(text)
    .map((piece) => ('ref' in piece ? (sources.find((item) => item.step === piece.step)?.fields.some((field) => field.key === piece.field) ? piece.ref : '') : piece.text))
    .join('');
}

interface DataFieldProps {
  label: string;
  hint?: string;
  value: string;
  sources: DataSource[];
  multiline?: boolean;
  placeholder?: string;
  onChange: (value: string) => void;
}

export function DataField({ label, hint, value, sources, multiline, placeholder, onChange }: DataFieldProps) {
  const id = useId();
  const box = useRef<HTMLDivElement>(null);
  const said = useRef<string | null>(null);
  // Where the person last had the caret in this field. The popover takes focus, so the place is kept here.
  const caret = useRef<Range | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const keep = () => {
      const root = box.current;
      const selection = window.getSelection();
      if (!root || !selection?.rangeCount) return;
      const range = selection.getRangeAt(0);
      if (root.contains(range.commonAncestorContainer)) caret.current = range.cloneRange();
    };
    document.addEventListener('selectionchange', keep);
    return () => document.removeEventListener('selectionchange', keep);
  }, []);

  useEffect(() => {
    const root = box.current;
    if (!root || value === said.current) return;
    said.current = value;
    root.replaceChildren(
      ...piecesOf(value).map((piece) => ('ref' in piece ? chip(piece.ref, refName(sources, piece.ref) ?? piece.field, untrustedField(sources, piece.step, piece.field)) : document.createTextNode(piece.text))),
    );
  }, [value, sources]);

  const emit = () => {
    if (!box.current) return;
    const next = textOf(box.current);
    said.current = next;
    onChange(next);
  };

  /** Takes a chip out, and leaves the caret where it was, in the field, so the next words land there. */
  const removeChip = (node: HTMLElement) => {
    const root = box.current;
    const selection = window.getSelection();
    const spot = document.createRange();
    spot.setStartBefore(node);
    spot.collapse(true);
    node.remove();
    if (root && selection) {
      root.focus();
      selection.removeAllRanges();
      selection.addRange(spot);
      caret.current = spot.cloneRange();
    }
    emit();
  };

  const onClick = (event: MouseEvent<HTMLDivElement>) => {
    const chipNode = (event.target as HTMLElement).closest<HTMLElement>('[data-remove]')?.parentElement;
    if (chipNode && box.current?.contains(chipNode)) removeChip(chipNode);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' && !multiline) event.preventDefault();
    const root = box.current;
    if (!root || (event.key !== 'Backspace' && event.key !== 'Delete')) return;
    const beside = chipBeside(root, event.key === 'Backspace' ? 'before' : 'after');
    if (!beside) return;
    event.preventDefault();
    removeChip(beside);
  };

  const insert = (step: string, field: string) => {
    const root = box.current;
    if (!root) return;
    setOpen(false);
    root.focus();
    const selection = window.getSelection();
    const node = chip(refOf(step, field), refName(sources, refOf(step, field)) ?? field, untrustedField(sources, step, field));
    const saved = caret.current;
    if (saved && root.contains(saved.commonAncestorContainer) && selection) {
      selection.removeAllRanges();
      selection.addRange(saved);
    }
    const range = selection?.rangeCount && root.contains(selection.anchorNode) ? selection.getRangeAt(0) : null;
    if (range) {
      range.deleteContents();
      range.insertNode(node);
      range.setStartAfter(node);
      range.collapse(true);
      selection?.removeAllRanges();
      selection?.addRange(range);
    } else {
      root.append(node);
    }
    emit();
  };

  return (
    <div className="grid content-start gap-2">
      <div className="flex h-5 items-center justify-between gap-2">
        <Label id={id}>{label}</Label>
        {sources.length ? (
          <Popover open={open} onOpenChange={setOpen}>
            <PopoverTrigger className="inline-flex items-center gap-1 rounded-sm text-12 text-muted hover:text-fg">
              <Braces aria-hidden="true" className="size-3" />
              Use data
            </PopoverTrigger>
            <PopoverContent className="max-h-80 overflow-y-auto p-1">
              {sources.map((source) => (
                <div key={source.step} className="py-1">
                  <p className="truncate px-2 py-1 text-12 text-faint">{source.label}</p>
                  {source.fields.map((field) => (
                    <button
                      key={field.key}
                      type="button"
                      onClick={() => insert(source.step, field.key)}
                      className="flex h-8 w-full items-center justify-between gap-2 rounded-md px-2 text-left text-14 hover:bg-fill-2"
                    >
                      <span className="truncate">{field.label}</span>
                      {field.untrusted ? <ShieldAlert aria-label="Written by someone outside" className="size-4 shrink-0 text-faint" /> : null}
                    </button>
                  ))}
                </div>
              ))}
              <p className="px-2 py-2 text-12 text-pretty text-faint">Text from outside can fill in words. It never decides what the routine does.</p>
            </PopoverContent>
          </Popover>
        ) : null}
      </div>
      <div
        ref={box}
        role="textbox"
        tabIndex={0}
        aria-labelledby={id}
        aria-multiline={multiline || undefined}
        aria-placeholder={placeholder}
        contentEditable="plaintext-only"
        suppressContentEditableWarning
        data-placeholder={placeholder}
        onInput={emit}
        onClick={onClick}
        onKeyDown={onKeyDown}
        className={cn(
          'w-full rounded-md border border-line bg-bg px-3 py-1 text-14 text-fg whitespace-pre-wrap transition-colors duration-(--duration-base) ease-standard outline-none hover:border-line-strong focus-visible:outline-2 focus-visible:outline-blue',
          'empty:before:text-faint empty:before:content-[attr(data-placeholder)]',
          multiline ? 'min-h-28 leading-6' : 'min-h-8 leading-6',
        )}
      />
      {hint ? <p className="text-13 text-faint">{hint}</p> : null}
    </div>
  );
}
