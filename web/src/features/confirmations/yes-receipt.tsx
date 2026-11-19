/**
 * A yes, on the record: who, where and how strong. A yes in chat is as much a yes as any, so it reads
 * the same, and it says plainly that agentos wrote the question and the agent could not answer it.
 */
import { AppWindow, Fingerprint, Laptop, MessageCircle } from 'lucide-react';
import type { ConfirmLevel, ConfirmMethodId } from '@/api/types';
import { PersonMark } from '@/components/brand/identity';
import { cn } from '@/lib/cn';
import { clock } from '@/lib/format';
import { METHOD_WORDS, RUNGS, rungIndex, rungOfLevel, RUNG_WORDS } from '@/lib/confirm';
import { RungIcon } from './marks';

const PLACE_ICON: Partial<Record<ConfirmMethodId, typeof Laptop>> = { chat: MessageCircle, panel: AppWindow, passkey: Fingerprint, mac: Laptop };

/** The ladder in four strokes, filled to this yes. */
function Strength({ level }: { level: ConfirmLevel }) {
  const at = rungIndex(rungOfLevel(level));
  return (
    <span aria-hidden="true" className="flex gap-1">
      {RUNGS.slice(1).map((rung, index) => (
        <span key={rung} className={cn('h-1 w-4 rounded-full', index + 1 <= at ? 'bg-fg' : 'bg-fill-strong')} />
      ))}
    </span>
  );
}

export interface YesReceiptProps {
  by: string;
  at: string;
  method: ConfirmMethodId;
  level: ConfirmLevel;
  where: string | null;
}

export function YesReceipt({ by, at, method, level, where }: YesReceiptProps) {
  const Icon = PLACE_ICON[method] ?? AppWindow;
  const rung = rungOfLevel(level);
  const place = method === 'chat' ? `A chat in ${where ?? 'a chat app'}` : (where ?? METHOD_WORDS[method].word);
  return (
    <section aria-label="How the yes was given" className="rounded-xl border border-line">
      <dl className="divide-y divide-line px-4 text-body">
        <div className="flex items-center justify-between gap-4 py-3">
          <dt className="shrink-0 text-secondary text-faint">Who</dt>
          <dd className="flex min-w-0 items-center gap-2">
            <PersonMark id={by} name={by} size={20} />
            <span className="truncate font-medium">{by}</span>
            <span className="tabular shrink-0 text-secondary text-muted">at {clock(at)}</span>
          </dd>
        </div>
        <div className="flex items-center justify-between gap-4 py-3">
          <dt className="shrink-0 text-secondary text-faint">Where</dt>
          <dd className="flex min-w-0 items-center gap-2">
            <Icon aria-hidden="true" className="size-4 shrink-0 text-faint" />
            <span className="truncate font-medium">{place}</span>
          </dd>
        </div>
        <div className="flex items-center justify-between gap-4 py-3">
          <dt className="shrink-0 text-secondary text-faint">How strong</dt>
          <dd className="flex min-w-0 items-center gap-3">
            <Strength level={level} />
            <RungIcon rung={rung} className="size-4 shrink-0 text-faint" />
            <span className="truncate font-medium">{RUNG_WORDS[rung].word}</span>
          </dd>
        </div>
      </dl>
      {method === 'chat' ? (
        <p className="border-t border-line p-4 text-secondary text-pretty text-muted">agentos wrote the question and the chat app showed it. The agent could not change the words, and could not answer for you.</p>
      ) : null}
    </section>
  );
}
