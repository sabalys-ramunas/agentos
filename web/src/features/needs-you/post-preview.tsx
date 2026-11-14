/**
 * The post as it will look on the site: an Instagram-shaped card for Instagram, a page post for
 * Facebook, a company post for LinkedIn, an article for Medium and blogs, and a plain page card for
 * anything else. A likeness drawn with our own components from what the node sends (the real
 * image and words), never a screenshot of the site and never its CSS (visual.md P1).
 *
 * Node text is always plain React text, so markup stays visible as markup.
 */
import { Bookmark, FileText, Globe, Heart, MessageCircle, MessageSquare, MoreHorizontal, Paperclip, Repeat2, Send, Share2, SquareTerminal, Star, ThumbsUp, Trash2 } from 'lucide-react';
import type { ReactNode } from 'react';
import { useState } from 'react';
import type { Approval } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { cn } from '@/lib/cn';
import { routineName } from '@/lib/principal';
import type { Where } from './words';
import { amountOf, isArticle } from './words';
import { baseName, kindOf, money, size } from './kinds';
import { LanePreview, PushPreview, VmPreview } from './dev-previews';

/** The node's own marker for a preview it had to shorten. */
const CUT = /\n?\[preview truncated: (\d+) more characters are not shown\]\s*$/;

/**
 * A preview that is a record (JSON), not words. The node words every card it knows; anything else that
 * still looks like a record is never shown raw.
 */
export function isRecord(text: string | null | undefined): boolean {
  const head = (text ?? '').trimStart();
  return (head.startsWith('{') || head.startsWith('[')) && (head.includes('":') || /^(\{\}|\[\]|\[\{|\[")/.test(head));
}

export function previewText(approval: Approval): { text: string | null; cut: number | null } {
  const given = approval.preview.text ?? null;
  const raw = isRecord(given) ? null : given;
  const cut = raw ? CUT.exec(raw) : null;
  return { text: cut && raw ? raw.slice(0, cut.index) : raw, cut: cut?.[1] ? Number(cut[1]) : null };
}

/** Hashtags and mentions in the site's link colour, the rest as written. */
function Caption({ text }: { text: string }) {
  const parts = text.split(/([#@][\p{L}\p{N}_.]+)/u);
  return (
    <>
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <span key={index} className="text-blue">
            {part}
          </span>
        ) : (
          part
        ),
      )}
    </>
  );
}

function CutNote({ count }: { count: number | null }) {
  if (!count) return null;
  return (
    <p className="border-t border-line bg-subtle px-4 py-2 text-13 text-muted">
      The preview is cut here. {count.toLocaleString('en')} more characters are not shown, and saying yes covers all of them.
    </p>
  );
}

function Photo({ src, className }: { src: string; className?: string }) {
  // From the computer, which may be away for a moment. Say so rather than show a broken picture.
  const [broken, setBroken] = useState(false);
  if (broken) {
    return (
      <div className={cn('flex aspect-[4/3] w-full items-center justify-center bg-subtle px-6 text-center text-13 text-muted', className)}>
        The picture cannot be shown right now. It is the one the computer will post.
      </div>
    );
  }
  return <img src={src} alt="What will be posted" onError={() => setBroken(true)} className={cn('block w-full bg-subtle object-cover', className)} />;
}

interface CardProps {
  approval: Approval;
  where: Where;
  color: string | null;
}

const card = 'w-full overflow-hidden rounded-lg border border-line bg-card text-fg shadow-lift';

const handleOf = (where: Where) => (where.handle ?? where.business ?? 'account').replace(/^@/, '');

function Instagram({ approval, where, color }: CardProps) {
  const { text, cut } = previewText(approval);
  const handle = handleOf(where);
  return (
    <article aria-label="Instagram post preview" className={cn(card, 'max-w-[440px]')}>
      <header className="flex items-center gap-2 px-3 py-2">
        <span className="rounded-full bg-[conic-gradient(from_200deg,var(--wait-dot),var(--brand),var(--ws-4),var(--wait-dot))] p-[2px]">
          <span className="block rounded-full bg-bg p-[2px]">
            <BusinessMark name={where.business ?? handle} color={color} size={24} className="rounded-full" />
          </span>
        </span>
        <span className="min-w-0 flex-1 leading-tight">
          <span className="block truncate text-13 font-semibold">{handle}</span>
          {where.business ? <span className="block truncate text-12 text-faint">{where.business}</span> : null}
        </span>
        <MoreHorizontal aria-hidden="true" className="size-4 text-muted" />
      </header>
      {approval.preview.image ? (
        <Photo src={approval.preview.image} className="aspect-square" />
      ) : (
        // Instagram always shows a picture, so words alone sit on a square of the business colour.
        <div className="flex aspect-square items-center justify-center bg-subtle p-8 text-center text-20 font-semibold break-words">{text}</div>
      )}
      <div className="flex items-center gap-4 px-3 pt-3 text-fg" aria-hidden="true">
        <Heart className="size-6" strokeWidth={1.75} />
        <MessageCircle className="size-6 -scale-x-100" strokeWidth={1.75} />
        <Send className="size-6" strokeWidth={1.75} />
        <Bookmark className="ml-auto size-6" strokeWidth={1.75} />
      </div>
      {text && approval.preview.image ? (
        <p className="px-3 pt-2 text-14 break-words whitespace-pre-wrap">
          <span className="font-semibold">{handle}</span> <Caption text={text} />
        </p>
      ) : null}
      <p className="px-3 pt-2 pb-3 text-11 tracking-wide text-faint uppercase">Not posted yet. It goes up when you say yes</p>
      <CutNote count={cut} />
    </article>
  );
}

function Facebook({ approval, where, color }: CardProps) {
  const { text, cut } = previewText(approval);
  return (
    <article aria-label="Facebook post preview" className={cn(card, 'max-w-[500px]')}>
      <header className="flex items-center gap-2 px-4 pt-3">
        <BusinessMark name={where.business ?? 'Page'} color={color} size={40} className="rounded-full" />
        <span className="min-w-0 flex-1 leading-tight">
          <span className="block truncate text-14 font-semibold">{where.business ?? 'Your page'}</span>
          <span className="flex items-center gap-1 text-12 text-faint">
            Not posted yet · <Globe aria-hidden="true" className="size-3" />
          </span>
        </span>
        <MoreHorizontal aria-hidden="true" className="size-4 text-muted" />
      </header>
      {text ? (
        <p className="px-4 pt-3 pb-3 text-14 break-words whitespace-pre-wrap">
          <Caption text={text} />
        </p>
      ) : (
        <div className="h-3" />
      )}
      {approval.preview.image ? <Photo src={approval.preview.image} className="max-h-[360px]" /> : null}
      <div className="mx-4 grid grid-cols-3 border-t border-line py-1 text-13 font-medium text-muted" aria-hidden="true">
        <span className="flex h-8 items-center justify-center gap-2">
          <ThumbsUp className="size-4" /> Like
        </span>
        <span className="flex h-8 items-center justify-center gap-2">
          <MessageCircle className="size-4" /> Comment
        </span>
        <span className="flex h-8 items-center justify-center gap-2">
          <Share2 className="size-4" /> Share
        </span>
      </div>
      <CutNote count={cut} />
    </article>
  );
}

function LinkedIn({ approval, where, color }: CardProps) {
  const { text, cut } = previewText(approval);
  return (
    <article aria-label="LinkedIn post preview" className={cn(card, 'max-w-[520px]')}>
      <header className="flex items-start gap-2 px-4 pt-3">
        <BusinessMark name={where.business ?? 'Company'} color={color} size={40} className="rounded-sm" />
        <span className="min-w-0 flex-1 leading-tight">
          <span className="block truncate text-14 font-semibold">{where.business ?? 'Your company'}</span>
          <span className="block truncate text-12 text-faint">Company page</span>
          <span className="flex items-center gap-1 text-12 text-faint">
            Not posted yet · <Globe aria-hidden="true" className="size-3" />
          </span>
        </span>
        <MoreHorizontal aria-hidden="true" className="size-4 text-muted" />
      </header>
      {text ? (
        <p className="px-4 pt-3 pb-3 text-14 break-words whitespace-pre-wrap">
          <Caption text={text} />
        </p>
      ) : (
        <div className="h-3" />
      )}
      {approval.preview.image ? <Photo src={approval.preview.image} className="max-h-[360px]" /> : null}
      <div className="mx-2 grid grid-cols-4 border-t border-line py-1 text-12 font-medium text-muted" aria-hidden="true">
        {[
          [ThumbsUp, 'Like'],
          [MessageSquare, 'Comment'],
          [Repeat2, 'Repost'],
          [Send, 'Send'],
        ].map(([Icon, label]) => {
          const Glyph = Icon as typeof ThumbsUp;
          return (
            <span key={label as string} className="flex h-12 flex-col items-center justify-center gap-1 text-11">
              <Glyph className="size-4" /> {label as string}
            </span>
          );
        })}
      </div>
      <CutNote count={cut} />
    </article>
  );
}

function Article({ approval, where, color }: CardProps) {
  const { text, cut } = previewText(approval);
  const [title = '', ...rest] = (text ?? approval.summary).split(/(?<=[.!?])\s+|\n/);
  const body = rest.join(' ').trim();
  return (
    <article aria-label={`${where.siteLabel ?? 'Article'} story preview`} className={cn(card, 'max-w-[560px]')}>
      <SiteBar where={where} />
      {approval.preview.image ? <Photo src={approval.preview.image} className="max-h-[280px]" /> : null}
      <div className="px-6 pt-5 pb-6">
        <h3 className="font-serif text-24 leading-tight font-semibold break-words">{title}</h3>
        <p className="mt-3 flex items-center gap-2 text-13 text-muted">
          <BusinessMark name={where.business ?? 'Author'} color={color} size={20} className="rounded-full" />
          {where.business ?? 'You'} · Not published yet
        </p>
        {body ? <p className="mt-4 font-serif text-16 leading-relaxed break-words whitespace-pre-wrap text-muted">{body}</p> : null}
      </div>
      <CutNote count={cut} />
    </article>
  );
}

/** A thin address bar, so a generic page still says which site it is on. */
function SiteBar({ where }: { where: Where }) {
  return (
    <div className="flex h-8 items-center gap-2 border-b border-line bg-subtle px-3 text-12 text-muted">
      <SiteMark site={where.site ?? where.domain ?? 'web'} size={14} label={false} />
      <span className="truncate">{where.domain ?? where.siteLabel ?? 'Website'}</span>
    </div>
  );
}

/** A routine's run to approve: its name, and one "Field: value" line per input it runs with. */
export interface RoutineRun {
  name: string;
  inputs: string[];
}

/** "post_date" reads "Post date". */
const fieldLabel = (field: string): string => {
  const words = field.replace(/[_-]+/g, ' ').trim();
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}`;
};

/** The node holds back a value it reads as a secret (a PIN, say) and writes it as this bare word, which is not JSON. */
const HELD_BACK = /:\s*\[redacted\]/g;

/** An input's value in words. Text as it is, Yes or No, a list with commas. A nested object has no words to show. */
function valueWords(value: unknown): string | null {
  if (value === '[redacted]') return 'Not shown';
  if (typeof value === 'string') return value.trim() ? value : 'Not set';
  if (typeof value === 'number') return String(value);
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (value === null || value === undefined) return 'Not set';
  if (Array.isArray(value)) return value.flatMap((item) => valueWords(item) ?? []).join(', ') || 'Not set';
  return null;
}

/**
 * The node sends a routine's run for approval as JSON text: `{"workflow", "name", "values"}`. Read it here, so the card
 * says the routine's name and its inputs in words, never the JSON. Null for any other preview, which keeps its own card.
 */
export function routineRunOf(text: string | null | undefined): RoutineRun | null {
  if (!text) return null;
  let data: unknown;
  try {
    // A held-back value is put in quotes first, so the rest still reads as JSON.
    data = JSON.parse(text.replace(HELD_BACK, ': "[redacted]"'));
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const { workflow, name, values } = data as { workflow?: unknown; name?: unknown; values?: unknown };
  if (typeof workflow !== 'string' || !workflow) return null;
  const title = typeof name === 'string' && name.trim() ? name.trim() : routineName(workflow);
  const inputs = values && typeof values === 'object' && !Array.isArray(values)
    ? Object.entries(values).flatMap(([field, value]) => {
        const words = valueWords(value);
        return words === null ? [] : [`${fieldLabel(field)}: ${words}`];
      })
    : [];
  return { name: title, inputs };
}

function Generic({ approval, where }: CardProps) {
  const { text, cut } = previewText(approval);
  const run = routineRunOf(text);
  const technical = ['exec', 'install_overlay', 'install_base', 'service'].includes(approval.effect);
  // A setup change is said in words (its summary); its raw content is never shown as the preview.
  const raw = ['install_overlay', 'install_base', 'service', 'secret'].includes(approval.effect) ? null : text;
  return (
    <article aria-label="Preview" className={cn(card, 'max-w-[520px]')}>
      <SiteBar where={where} />
      {approval.preview.image ? <Photo src={approval.preview.image} className="max-h-[320px]" /> : null}
      {run ? (
        <div className="grid gap-2 px-4 py-4">
          <p className="text-14 font-medium break-words">{run.name}</p>
          {run.inputs.map((line, index) => (
            <p key={index} className="text-14 break-words text-muted">
              {line}
            </p>
          ))}
        </div>
      ) : (
        <div className="grid gap-2 px-4 py-4">
          <p className="text-14 font-medium break-words">{approval.summary}</p>
          {raw ? <p className={cn('text-14 break-words whitespace-pre-wrap text-muted', technical && 'mono text-13')}>{raw}</p> : null}
        </div>
      )}
      <CutNote count={cut} />
    </article>
  );
}

function Payment({ approval, where }: CardProps) {
  const { text } = previewText(approval);
  const d = approval.preview.data ?? {};
  const amount = money(d.amount, d.currency) ?? amountOf(approval.summary);
  const rows: [string, string | undefined][] = [
    ['To', d.payee],
    ['For', d.for],
    ['Reference', d.reference],
    ['From', d.from_account],
    ['Due', d.due],
  ];
  const known = rows.filter((row): row is [string, string] => Boolean(row[1]));
  return (
    <article aria-label="Payment preview" className={cn(card, 'max-w-[440px]')}>
      <SiteBar where={where} />
      <div className="grid gap-1 px-6 pt-6 pb-4">
        <p className="text-13 text-muted">You pay</p>
        <p className="tabular text-32 font-semibold">{amount ?? 'An amount'}</p>
        {d.payee ? <p className="text-14">to {d.payee}</p> : null}
      </div>
      {known.length ? (
        <dl className="mx-6 mb-6 divide-y divide-line border-t border-line">
          {known.map(([label, value]) => (
            <div key={label} className="flex items-baseline justify-between gap-4 py-2 text-13">
              <dt className="text-faint">{label}</dt>
              <dd className="min-w-0 text-right break-words">{value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <div className="grid gap-2 px-6 pb-6">
          <p className="text-14 break-words">{approval.summary}</p>
          {text ? <p className="text-13 break-words whitespace-pre-wrap text-muted">{text}</p> : null}
        </div>
      )}
    </article>
  );
}

/** An email as it will be sent: who it goes to, the subject, the words and what is attached. */
function Email({ approval, where }: CardProps) {
  const d = approval.preview.data ?? {};
  const body = d.body ?? previewText(approval).text ?? '';
  const head: [string, string | undefined][] = [
    ['From', d.from],
    ['To', d.to?.join(', ')],
    ['Cc', d.cc?.length ? d.cc.join(', ') : undefined],
  ];
  return (
    <article aria-label="Email preview" className={cn(card, 'max-w-[560px]')}>
      <div className="flex h-10 items-center gap-2 border-b border-line bg-subtle px-4 text-13 font-medium">
        <SiteMark site={where.site ?? 'mail'} size={16} label={false} />
        New message
      </div>
      <dl className="divide-y divide-line px-4">
        {head
          .filter((row): row is [string, string] => Boolean(row[1]))
          .map(([label, value]) => (
            <div key={label} className="flex gap-3 py-2 text-13">
              <dt className="w-10 shrink-0 text-faint">{label}</dt>
              {/* A long address breaks after its @, never in the middle of a word. */}
              <dd className="min-w-0 break-words">
                {value.split('@').map((part, index) => (
                  <span key={index}>
                    {index > 0 ? (
                      <>
                        @<wbr />
                      </>
                    ) : null}
                    {part}
                  </span>
                ))}
              </dd>
            </div>
          ))}
      </dl>
      <p className="border-t border-line px-4 py-3 text-16 font-semibold break-words">{d.subject ?? approval.summary}</p>
      <p className="px-4 pb-4 text-14 break-words whitespace-pre-wrap">{body}</p>
      {d.attachments?.length ? (
        <ul aria-label="Attachments" className="flex flex-wrap gap-2 border-t border-line px-4 py-3">
          {d.attachments.map((file) => (
            <li key={file.name} className="inline-flex h-8 items-center gap-2 rounded-lg border border-line bg-subtle px-2 text-13">
              <Paperclip aria-hidden="true" className="size-4 text-muted" />
              <span className="break-all">{file.name}</span>
              {size(file.size) ? <span className="text-faint">{size(file.size)}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </article>
  );
}

/** A form as it will be submitted, every field, with what changed marked and what it was. */
function Form({ approval, where }: CardProps) {
  const d = approval.preview.data ?? {};
  const changed = new Set(d.changed ?? (d.fields ?? []).filter((field) => field.before != null).map((field) => field.label));
  return (
    <article aria-label="Form preview" className={cn(card, 'max-w-[520px]')}>
      <SiteBar where={where} />
      <div className="px-4 pt-4 pb-2">
        <h3 className="text-16 font-semibold break-words">{d.form ?? approval.summary}</h3>
        {changed.size ? <p className="mt-1 text-12 text-muted">{changed.size === 1 ? 'One field changes' : `${changed.size} fields change`}, marked below.</p> : null}
      </div>
      <dl className="grid gap-2 px-4 pb-4">
        {(d.fields ?? []).map((field) => {
          const isChanged = changed.has(field.label);
          return (
            <div key={field.label} className={cn('grid gap-1 rounded-lg border px-3 py-2', isChanged ? 'border-(--wait-line) bg-(--wait-soft)' : 'border-line')}>
              <dt className="flex items-center gap-2 text-12 text-faint">
                {field.label}
                {isChanged ? <span className="rounded-sm bg-(--wait-dot) px-1 text-11 font-medium text-(--wait-ink)">Changes</span> : null}
              </dt>
              <dd className="text-14 break-words">
                {isChanged && field.before ? (
                  <>
                    <span className="text-faint line-through">{field.before}</span>
                    <span aria-hidden="true" className="text-faint">{' → '}</span>
                    <span className="sr-only"> becomes </span>
                  </>
                ) : null}
                <span className={cn(isChanged && 'font-semibold')}>{field.value}</span>
              </dd>
            </div>
          );
        })}
      </dl>
    </article>
  );
}

/** A refund on an order: who, which order, what and how much. */
function Refund({ approval, where }: CardProps) {
  const d = approval.preview.data ?? {};
  const amount = money(d.amount, d.currency);
  return (
    <article aria-label="Refund preview" className={cn(card, 'max-w-[460px]')}>
      <SiteBar where={where} />
      <div className="flex items-baseline justify-between gap-4 px-6 pt-6">
        <div>
          <p className="text-13 text-muted">Refund {d.order ? `for order ${d.order}` : ''}</p>
          <p className="text-16 font-semibold">{d.customer ?? 'The customer'}</p>
        </div>
        <p className="tabular text-32 font-semibold">{amount ?? ''}</p>
      </div>
      {d.items?.length ? (
        <ul className="mx-6 mt-4 divide-y divide-line border-y border-line">
          {d.items.map((item) => (
            <li key={item.name} className="flex justify-between gap-4 py-2 text-13">
              <span className="min-w-0 break-words">
                {item.quantity ? `${item.quantity} × ` : ''}
                {item.name}
              </span>
              <span className="tabular">{money(item.price ?? undefined, d.currency) ?? ''}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {d.reason ? <p className="px-6 pt-3 text-13 text-muted">Why: {d.reason}</p> : null}
      <div className="h-6" />
    </article>
  );
}

function Stars({ count }: { count: number }) {
  return (
    <span role="img" aria-label={`${count} of 5 stars`} className="inline-flex gap-px">
      {Array.from({ length: 5 }, (_, index) => (
        <Star key={index} aria-hidden="true" className={cn('size-3', index < count ? 'fill-(--wait-dot) text-(--wait-dot)' : 'text-(--border-strong)')} />
      ))}
    </span>
  );
}

/** A reply in its thread: what they said, then the reply from the business, as on the site. */
function Thread({ approval, where, color }: CardProps) {
  const d = approval.preview.data ?? {};
  const reply = d.reply ?? previewText(approval).text ?? '';
  return (
    <article aria-label="Reply preview" className={cn(card, 'max-w-[520px]')}>
      <SiteBar where={where} />
      <ol className="grid gap-3 p-4">
        {(d.thread ?? []).map((message, index) => (
          <li key={index} className="flex items-start gap-2">
            <span aria-hidden="true" className="flex size-8 shrink-0 items-center justify-center rounded-full bg-fill-2 text-12 font-semibold text-muted">
              {message.from.charAt(0)}
            </span>
            <div className="min-w-0 rounded-xl rounded-tl-sm bg-subtle px-3 py-2">
              <p className="flex flex-wrap items-center gap-2 text-13 font-medium">
                {message.from}
                {message.stars ? <Stars count={message.stars} /> : null}
              </p>
              <p className="text-14 break-words whitespace-pre-wrap">{message.text}</p>
            </div>
          </li>
        ))}
        <li className="flex items-start justify-end gap-2">
          <div className="min-w-0 rounded-xl rounded-tr-sm border border-line bg-bg px-3 py-2 shadow-[0_1px_2px_rgb(0_0_0/0.04)]">
            <p className="text-13 font-medium">
              {where.business ?? 'You'} <span className="font-normal text-faint">· Not sent yet</span>
            </p>
            <p className="text-14 break-words whitespace-pre-wrap">{reply}</p>
          </div>
          <BusinessMark name={where.business ?? 'You'} color={color} size={32} className="rounded-full" />
        </li>
      </ol>
    </article>
  );
}

/** A command, exactly as it will run, and on which computer. */
function Command({ approval }: CardProps) {
  const d = approval.preview.data ?? {};
  const command = d.command ?? previewText(approval).text ?? approval.summary;
  return (
    <article aria-label="Command preview" className="w-full max-w-[560px] overflow-hidden rounded-lg border border-line bg-terminal text-terminal-text">
      <div className="flex h-8 items-center gap-2 border-b border-line px-3 font-mono text-code-caption text-terminal-dim">
        <SquareTerminal aria-hidden="true" className="size-4" />
        {d.computer ?? 'The computer'}
        {d.cwd ? <span>· {d.cwd}</span> : null}
      </div>
      <pre className="overflow-x-auto px-4 py-4 font-mono text-code break-all whitespace-pre-wrap">
        <span aria-hidden="true" className="text-terminal-dim">$ </span>
        {command}
      </pre>
    </article>
  );
}

/** Files: their names and sizes, and where they go. */
function Files({ approval, where }: CardProps) {
  const d = approval.preview.data ?? {};
  return (
    <article aria-label="Files preview" className={cn(card, 'max-w-[480px]')}>
      <SiteBar where={where} />
      <p className="px-4 pt-4 text-14 font-medium break-words">{d.destination ? `To ${d.destination}` : approval.summary}</p>
      <FileList items={d.items ?? []} />
    </article>
  );
}

function FileList({ items, gone = false }: { items: { name: string; size?: number | null }[]; gone?: boolean }) {
  const total = items.reduce((sum, item) => sum + (item.size ?? 0), 0);
  return (
    <>
      <ul className="m-4 mb-2 divide-y divide-line rounded-lg border border-line">
        {items.map((item) => (
          <li key={item.name} className="flex items-center gap-2 px-3 py-2 text-13">
            {gone ? <Trash2 aria-hidden="true" className="size-4 shrink-0 text-red" /> : <FileText aria-hidden="true" className="size-4 shrink-0 text-muted" />}
            <span className="min-w-0 flex-1">
              <span className="block break-all">{baseName(item.name)}</span>
              {item.name !== baseName(item.name) ? <span className="block text-12 break-all text-faint">{item.name}</span> : null}
            </span>
            {size(item.size) ? <span className="tabular shrink-0 text-faint">{size(item.size)}</span> : null}
          </li>
        ))}
      </ul>
      {total ? <p className="px-4 pb-4 text-12 text-faint">{size(total)} in all</p> : <div className="h-2" />}
    </>
  );
}

function Deletion({ approval, where }: CardProps) {
  const { text } = previewText(approval);
  const d = approval.preview.data ?? {};
  const lines = (text ?? '').split('\n').filter(Boolean);
  const items = d.items?.length ? d.items : lines.map((name) => ({ name }));
  return (
    <article aria-label="What will be deleted" className={cn(card, 'max-w-[480px]')}>
      <SiteBar where={d.computer ? { ...where, site: 'terminal', domain: d.computer } : where} />
      <p className="px-4 pt-4 text-14 font-medium break-words">{approval.summary}</p>
      {items.length ? <FileList items={items} gone /> : <div className="h-4" />}
    </article>
  );
}

/** Which likeness to draw. */
export function previewKind(approval: Approval, where: Where) {
  const kind = kindOf(approval);
  switch (kind) {
    case 'email':
      return 'email';
    case 'form':
      return 'form';
    case 'order':
      return 'refund';
    case 'message':
      return 'thread';
    case 'command':
      return 'command';
    case 'files':
      return 'files';
    case 'delete':
      return 'deletion';
    case 'payment':
      return 'payment';
    case 'push':
      return 'push';
    case 'vm':
      return 'vm';
    case 'lane':
      return 'lane';
    default:
      break;
  }
  if (approval.effect === 'pay') return 'payment';
  if (approval.effect === 'irreversible') return 'deletion';
  if (approval.effect !== 'commit' && approval.effect !== 'draft') return 'generic';
  if (where.site === 'instagram') return 'instagram';
  if (where.site === 'facebook') return 'facebook';
  if (where.site === 'linkedin') return 'linkedin';
  if (isArticle(where)) return 'article';
  return 'generic';
}

const CARDS = {
  instagram: Instagram,
  facebook: Facebook,
  linkedin: LinkedIn,
  article: Article,
  payment: Payment,
  deletion: Deletion,
  generic: Generic,
  email: Email,
  form: Form,
  refund: Refund,
  thread: Thread,
  command: Command,
  files: Files,
  push: PushPreview,
  vm: VmPreview,
  lane: LanePreview,
};

/** The pill over the preview: what this picture is. */
function stageLabel(kind: ReturnType<typeof previewKind>, where: Where): string {
  switch (kind) {
    case 'payment':
      return 'What you pay';
    case 'deletion':
      return 'Exactly what goes';
    case 'email':
      return 'The email as it will be sent';
    case 'form':
      return 'The form as it will be sent';
    case 'refund':
      return 'The refund';
    case 'thread':
      return 'The reply in its thread';
    case 'command':
      return 'Exactly what will run';
    case 'files':
      return 'The files';
    case 'push':
      return 'What lands on the remote';
    case 'vm':
      return 'What it costs, and for how long';
    case 'lane':
      return 'Where it runs, and what runs first';
    case 'generic':
      if (where.site === null) return 'What it will do';
      break;
    default:
      break;
  }
  return `How it will look on ${where.siteLabel ?? 'the site'}`;
}

/**
 * The preview on a soft stage tinted with the business colour, with a pill naming what it is.
 * `children` sits over the card: the done state draws its check there.
 */
export function PostPreview({ approval, where, color, children, className }: CardProps & { children?: ReactNode; className?: string }) {
  const kind = previewKind(approval, where);
  const Card = CARDS[kind];
  return (
    <div className={cn('relative flex flex-col items-center gap-4 rounded-lg border border-line bg-inset px-4 py-6 sm:px-6', className)}>
      <span className="inline-flex h-7 items-center gap-2 rounded-full border border-line bg-card px-3 text-12 text-muted">
        {where.site ? <SiteMark site={where.site} size={14} label={false} /> : null}
        {stageLabel(kind, where)}
      </span>
      <div className="relative flex w-full justify-center">
        <Card approval={approval} where={where} color={color} />
        {children}
      </div>
    </div>
  );
}
