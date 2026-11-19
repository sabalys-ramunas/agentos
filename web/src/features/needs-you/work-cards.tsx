/** Previews for what is not a social post: a payment, an email, a form, a refund, a reply, a command, files and a deletion. */
import { FileText, Paperclip, SquareTerminal, Star, Trash2 } from 'lucide-react';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { cn } from '@/lib/cn';
import { amountOf } from './words';
import { baseName, money, size } from './kinds';
import type { CardProps } from './preview-parts';
import { CutNote, Photo, card, previewText } from './preview-parts';
import { routineRunOf } from './routine-run';
import { SiteBar } from './social-cards';

export function Generic({ approval, where }: CardProps) {
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
          <p className="text-label break-words">{run.name}</p>
          {run.inputs.map((line, index) => (
            <p key={index} className="text-body break-words text-muted">
              {line}
            </p>
          ))}
        </div>
      ) : (
        <div className="grid gap-2 px-4 py-4">
          <p className="text-label break-words">{approval.summary}</p>
          {raw ? <p className={cn('text-body break-words whitespace-pre-wrap text-muted', technical && 'mono text-secondary')}>{raw}</p> : null}
        </div>
      )}
      <CutNote count={cut} />
    </article>
  );
}

export function Payment({ approval, where }: CardProps) {
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
        <p className="text-secondary text-muted">You pay</p>
        <p className="tabular text-headline">{amount ?? 'An amount'}</p>
        {d.payee ? <p className="text-body">to {d.payee}</p> : null}
      </div>
      {known.length ? (
        <dl className="mx-6 mb-6 divide-y divide-line border-t border-line">
          {known.map(([label, value]) => (
            <div key={label} className="flex items-baseline justify-between gap-4 py-2 text-secondary">
              <dt className="text-faint">{label}</dt>
              <dd className="min-w-0 text-right break-words">{value}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <div className="grid gap-2 px-6 pb-6">
          <p className="text-body break-words">{approval.summary}</p>
          {text ? <p className="text-secondary break-words whitespace-pre-wrap text-muted">{text}</p> : null}
        </div>
      )}
    </article>
  );
}

/** An email as it will be sent: who it goes to, the subject, the words and what is attached. */
export function Email({ approval, where }: CardProps) {
  const d = approval.preview.data ?? {};
  const body = d.body ?? previewText(approval).text ?? '';
  const head: [string, string | undefined][] = [
    ['From', d.from],
    ['To', d.to?.join(', ')],
    ['Cc', d.cc?.length ? d.cc.join(', ') : undefined],
  ];
  return (
    <article aria-label="Email preview" className={cn(card, 'max-w-[560px]')}>
      <div className="flex h-10 items-center gap-2 border-b border-line bg-inset px-4 text-secondary font-medium">
        <SiteMark site={where.site ?? 'mail'} size={16} label={false} />
        New message
      </div>
      <dl className="divide-y divide-line px-4">
        {head
          .filter((row): row is [string, string] => Boolean(row[1]))
          .map(([label, value]) => (
            <div key={label} className="flex gap-3 py-2 text-secondary">
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
      <p className="border-t border-line px-4 py-3 text-section-title break-words">{d.subject ?? approval.summary}</p>
      <p className="px-4 pb-4 text-body break-words whitespace-pre-wrap">{body}</p>
      {d.attachments?.length ? (
        <ul aria-label="Attachments" className="flex flex-wrap gap-2 border-t border-line px-4 py-3">
          {d.attachments.map((file) => (
            <li key={file.name} className="inline-flex h-8 items-center gap-2 rounded-lg border border-line bg-inset px-2 text-secondary">
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
export function Form({ approval, where }: CardProps) {
  const d = approval.preview.data ?? {};
  const changed = new Set(d.changed ?? (d.fields ?? []).filter((field) => field.before != null).map((field) => field.label));
  return (
    <article aria-label="Form preview" className={cn(card, 'max-w-[520px]')}>
      <SiteBar where={where} />
      <div className="px-4 pt-4 pb-2">
        <h3 className="text-section-title break-words">{d.form ?? approval.summary}</h3>
        {changed.size ? <p className="mt-1 text-caption text-muted">{changed.size === 1 ? 'One field changes' : `${changed.size} fields change`}, marked below.</p> : null}
      </div>
      <dl className="grid gap-2 px-4 pb-4">
        {(d.fields ?? []).map((field) => {
          const isChanged = changed.has(field.label);
          return (
            <div key={field.label} className={cn('grid gap-1 rounded-lg border px-3 py-2', isChanged ? 'border-(--wait-line) bg-(--wait-soft)' : 'border-line')}>
              <dt className="flex items-center gap-2 text-caption text-faint">
                {field.label}
                {isChanged ? <span className="rounded-sm bg-(--wait-dot) px-1 text-micro text-(--wait-ink)">Changes</span> : null}
              </dt>
              <dd className="text-body break-words">
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
export function Refund({ approval, where }: CardProps) {
  const d = approval.preview.data ?? {};
  const amount = money(d.amount, d.currency);
  return (
    <article aria-label="Refund preview" className={cn(card, 'max-w-[460px]')}>
      <SiteBar where={where} />
      <div className="flex items-baseline justify-between gap-4 px-6 pt-6">
        <div>
          <p className="text-secondary text-muted">Refund {d.order ? `for order ${d.order}` : ''}</p>
          <p className="text-section-title">{d.customer ?? 'The customer'}</p>
        </div>
        <p className="tabular text-headline">{amount ?? ''}</p>
      </div>
      {d.items?.length ? (
        <ul className="mx-6 mt-4 divide-y divide-line border-y border-line">
          {d.items.map((item) => (
            <li key={item.name} className="flex justify-between gap-4 py-2 text-secondary">
              <span className="min-w-0 break-words">
                {item.quantity ? `${item.quantity} × ` : ''}
                {item.name}
              </span>
              <span className="tabular">{money(item.price ?? undefined, d.currency) ?? ''}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {d.reason ? <p className="px-6 pt-3 text-secondary text-muted">Why: {d.reason}</p> : null}
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
export function Thread({ approval, where, color }: CardProps) {
  const d = approval.preview.data ?? {};
  const reply = d.reply ?? previewText(approval).text ?? '';
  return (
    <article aria-label="Reply preview" className={cn(card, 'max-w-[520px]')}>
      <SiteBar where={where} />
      <ol className="grid gap-3 p-4">
        {(d.thread ?? []).map((message, index) => (
          <li key={index} className="flex items-start gap-2">
            <span aria-hidden="true" className="flex size-8 shrink-0 items-center justify-center rounded-full bg-pressed text-caption font-semibold text-muted">
              {message.from.charAt(0)}
            </span>
            <div className="min-w-0 rounded-xl rounded-tl-sm bg-inset px-3 py-2">
              <p className="flex flex-wrap items-center gap-2 text-secondary font-medium">
                {message.from}
                {message.stars ? <Stars count={message.stars} /> : null}
              </p>
              <p className="text-body break-words whitespace-pre-wrap">{message.text}</p>
            </div>
          </li>
        ))}
        <li className="flex items-start justify-end gap-2">
          <div className="min-w-0 rounded-xl rounded-tr-sm border border-line bg-card px-3 py-2 shadow-[0_1px_2px_rgb(0_0_0/0.04)]">
            <p className="text-secondary font-medium">
              {where.business ?? 'You'} <span className="font-normal text-faint">· Not sent yet</span>
            </p>
            <p className="text-body break-words whitespace-pre-wrap">{reply}</p>
          </div>
          <BusinessMark name={where.business ?? 'You'} color={color} size={32} className="rounded-full" />
        </li>
      </ol>
    </article>
  );
}

/** A command, exactly as it will run, and on which computer. */
export function Command({ approval }: CardProps) {
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
export function Files({ approval, where }: CardProps) {
  const d = approval.preview.data ?? {};
  return (
    <article aria-label="Files preview" className={cn(card, 'max-w-[480px]')}>
      <SiteBar where={where} />
      <p className="px-4 pt-4 text-label break-words">{d.destination ? `To ${d.destination}` : approval.summary}</p>
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
          <li key={item.name} className="flex items-center gap-2 px-3 py-2 text-secondary">
            {gone ? <Trash2 aria-hidden="true" className="size-4 shrink-0 text-bad" /> : <FileText aria-hidden="true" className="size-4 shrink-0 text-muted" />}
            <span className="min-w-0 flex-1">
              <span className="block break-all">{baseName(item.name)}</span>
              {item.name !== baseName(item.name) ? <span className="block text-caption break-all text-faint">{item.name}</span> : null}
            </span>
            {size(item.size) ? <span className="tabular shrink-0 text-faint">{size(item.size)}</span> : null}
          </li>
        ))}
      </ul>
      {total ? <p className="px-4 pb-4 text-caption text-faint">{size(total)} in all</p> : <div className="h-2" />}
    </>
  );
}

export function Deletion({ approval, where }: CardProps) {
  const { text } = previewText(approval);
  const d = approval.preview.data ?? {};
  const lines = (text ?? '').split('\n').filter(Boolean);
  const items = d.items?.length ? d.items : lines.map((name) => ({ name }));
  return (
    <article aria-label="What will be deleted" className={cn(card, 'max-w-[480px]')}>
      <SiteBar where={d.computer ? { ...where, site: 'terminal', domain: d.computer } : where} />
      <p className="px-4 pt-4 text-label break-words">{approval.summary}</p>
      {items.length ? <FileList items={items} gone /> : <div className="h-4" />}
    </article>
  );
}
