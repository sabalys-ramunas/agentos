/**
 * The proof for work that is not a post: the email as it went out, the files as saved, the
 * payment, the form as sent in with what changed marked, what research found, the order, the reply
 * in its thread, the command and what it printed. Each is drawn as the thing itself, so a person
 * recognises it at a glance, the way a post shows as its screenshot.
 */
import { ArrowDown, ArrowUp, ClipboardList, FileText, Mail, MessageSquare, Paperclip, ShoppingBag, SquareTerminal, Table2 } from 'lucide-react';
import type { ReactNode } from 'react';
import type { StoryArtifact } from '@/api/types';
import { cn } from '@/lib/cn';
import { plural, safeUrl } from '@/lib/format';

/** "48 KB", "1.2 MB". */
export function fileSize(bytes: number): string {
  if (bytes < 1_000) return `${bytes} B`;
  if (bytes < 1_000_000) return `${Math.round(bytes / 1_000)} KB`;
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

function Frame({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <figure aria-label={label} className={cn('overflow-hidden rounded-xl border border-line bg-card', className)}>
      {children}
    </figure>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex gap-3 px-4 py-2 text-secondary">
      <dt className="w-16 shrink-0 text-faint">{label}</dt>
      <dd className="min-w-0 flex-1 break-words">{children}</dd>
    </div>
  );
}

function Email({ artifact }: { artifact: Extract<StoryArtifact, { type: 'email' }> }) {
  return (
    <Frame label="The email as it was sent">
      <dl className="divide-y divide-line border-b border-line bg-inset">
        <Row label="From">{artifact.from}</Row>
        <Row label="To">{artifact.to.join(', ')}</Row>
        {artifact.cc?.length ? <Row label="Cc">{artifact.cc.join(', ')}</Row> : null}
        <Row label="Subject">
          <span className="font-medium">{artifact.subject}</span>
        </Row>
      </dl>
      <p className="max-h-60 overflow-y-auto px-4 py-3 text-body whitespace-pre-line">{artifact.body}</p>
      {artifact.attachments?.length ? (
        <ul aria-label="Attachments" className="flex flex-wrap gap-2 border-t border-line px-4 py-3">
          {artifact.attachments.map((file) => (
            <li key={file.name} className="flex items-center gap-2 rounded-md border border-line px-2 py-1 text-secondary">
              <Paperclip aria-hidden="true" className="size-4 shrink-0 text-faint" />
              <span className="truncate">{file.name}</span>
              <span className="tabular shrink-0 text-caption text-faint">{fileSize(file.size)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </Frame>
  );
}

function Files({ artifact }: { artifact: Extract<StoryArtifact, { type: 'files' }> }) {
  const total = artifact.files.reduce((sum, file) => sum + file.size, 0);
  return (
    <Frame label="The files as saved">
      <p className="flex items-center justify-between gap-3 border-b border-line bg-inset px-4 py-2 text-secondary text-muted">
        <span className="truncate">{artifact.folder ? `Saved to ${artifact.folder}` : 'Saved'}</span>
        <span className="tabular shrink-0 text-faint">
          {plural(artifact.files.length, 'file')} · {fileSize(total)}
        </span>
      </p>
      <ul className="divide-y divide-line">
        {artifact.files.map((file) => {
          const url = safeUrl(file.url) ?? (file.url?.startsWith('/api/') ? file.url : null);
          return (
            <li key={file.name} className="flex items-center gap-3 px-4 py-2 text-secondary">
              <FileText aria-hidden="true" className="size-4 shrink-0 text-faint" />
              {url ? (
                <a href={url} target="_blank" rel="noreferrer noopener" className="link min-w-0 flex-1 truncate">
                  {file.name}
                </a>
              ) : (
                <span className="min-w-0 flex-1 truncate">{file.name}</span>
              )}
              <span className="tabular shrink-0 text-caption text-faint">{fileSize(file.size)}</span>
            </li>
          );
        })}
      </ul>
    </Frame>
  );
}

function Payment({ artifact }: { artifact: Extract<StoryArtifact, { type: 'payment' }> }) {
  return (
    <Frame label="The payment">
      <div className="bg-inset px-4 py-5 text-center">
        <p className="tabular text-headline">{artifact.amount}</p>
        <p className="mt-1 text-body text-muted">to {artifact.payee}</p>
      </div>
      <dl className="divide-y divide-line border-t border-line">
        {artifact.for ? <Row label="For">{artifact.for}</Row> : null}
        {artifact.from_account ? <Row label="From">{artifact.from_account}</Row> : null}
        {artifact.reference ? (
          <Row label="Ref">
            <span className="mono">{artifact.reference}</span>
          </Row>
        ) : null}
      </dl>
    </Frame>
  );
}

function Form({ artifact }: { artifact: Extract<StoryArtifact, { type: 'form' }> }) {
  const changed = artifact.fields.filter((field) => field.changed).length;
  return (
    <Frame label="The form as it was sent in">
      <p className="flex items-center justify-between gap-3 border-b border-line bg-inset px-4 py-2 text-secondary">
        <span className="truncate font-medium">{artifact.title}</span>
        {changed ? <span className="shrink-0 text-faint">{plural(changed, 'change')}</span> : null}
      </p>
      <dl className="divide-y divide-line">
        {artifact.fields.map((field) => (
          <div key={field.label} className={cn('flex gap-3 px-4 py-2 text-secondary', field.changed && 'bg-(--wait-soft)')}>
            <dt className="w-32 shrink-0 text-faint">{field.label}</dt>
            <dd className="min-w-0 flex-1 break-words">
              {field.changed && field.before ? <span className="mr-2 text-faint line-through">{field.before}</span> : null}
              <span className={cn(field.changed && 'font-medium')}>{field.value}</span>
              {field.changed ? <span className="sr-only"> (changed)</span> : null}
            </dd>
          </div>
        ))}
      </dl>
      {artifact.confirmation ? (
        <p className="border-t border-line px-4 py-2 text-secondary text-muted">
          Confirmation <span className="mono text-fg">{artifact.confirmation}</span>
        </p>
      ) : null}
    </Frame>
  );
}

function ResultTable({ artifact }: { artifact: Extract<StoryArtifact, { type: 'table' }> }) {
  return (
    <Frame label={artifact.title}>
      <p className="border-b border-line bg-inset px-4 py-2 text-secondary font-medium">{artifact.title}</p>
      <div className="overflow-x-auto">
        <table className="w-full text-secondary">
          <thead>
            <tr className="border-b border-line text-left text-faint">
              {artifact.columns.map((column) => (
                <th key={column} scope="col" className="px-4 py-2 font-normal whitespace-nowrap">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {artifact.rows.map((row, index) => (
              <tr key={index} className={cn('border-b border-line last:border-b-0', row.change && 'bg-(--wait-soft)')}>
                {row.cells.map((cell, column) => (
                  <td key={column} className={cn('px-4 py-2 whitespace-nowrap', column > 0 && 'tabular')}>
                    {column === row.cells.length - 1 && row.change === 'up' ? <ArrowUp aria-label="went up" className="mr-1 inline size-3 text-bad" /> : null}
                    {column === row.cells.length - 1 && row.change === 'down' ? <ArrowDown aria-label="went down" className="mr-1 inline size-3 text-ok" /> : null}
                    {cell}
                    {column === 0 && row.change === 'new' ? <span className="ml-2 text-caption text-faint">new</span> : null}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Frame>
  );
}

function Order({ artifact }: { artifact: Extract<StoryArtifact, { type: 'order' }> }) {
  return (
    <Frame label={artifact.refund ? 'The refund' : 'The order'}>
      <p className="flex items-center justify-between gap-3 border-b border-line bg-inset px-4 py-2 text-secondary">
        <span className="font-medium">
          {artifact.refund ? 'Refund for order' : 'Order'} <span className="mono">{artifact.number}</span>
        </span>
        {artifact.customer ? <span className="truncate text-muted">{artifact.customer}</span> : null}
      </p>
      <ul className="divide-y divide-line">
        {artifact.items.map((item) => (
          <li key={item.name} className="flex items-center gap-3 px-4 py-2 text-secondary">
            <span className="tabular w-8 shrink-0 text-faint">{item.quantity}×</span>
            <span className="min-w-0 flex-1 truncate">{item.name}</span>
            <span className="tabular shrink-0">{item.price}</span>
          </li>
        ))}
      </ul>
      <p className="flex items-center justify-between border-t border-line px-4 py-2 text-body font-semibold">
        <span>{artifact.refund ? 'Refunded' : 'Total'}</span>
        <span className="tabular">{artifact.total}</span>
      </p>
      {artifact.note ? <p className="border-t border-line px-4 py-2 text-secondary text-muted">{artifact.note}</p> : null}
    </Frame>
  );
}

function Thread({ artifact }: { artifact: Extract<StoryArtifact, { type: 'message' }> }) {
  return (
    <Frame label="The reply in its thread" className="bg-inset">
      <ol className="grid gap-2 p-4">
        {artifact.thread.map((message, index) => (
          <li key={index} className={cn('max-w-[85%] rounded-xl px-3 py-2 text-secondary', message.ours ? 'ml-auto border border-line bg-card' : 'bg-hover')}>
            <p className="text-caption font-medium text-muted">
              {message.from}
              {index === 0 && artifact.rating ? <span className="ml-2 text-warn">{'★'.repeat(artifact.rating)}</span> : null}
            </p>
            <p className="mt-1 whitespace-pre-line">{message.text}</p>
          </li>
        ))}
      </ol>
    </Frame>
  );
}

function Command({ artifact }: { artifact: Extract<StoryArtifact, { type: 'command' }> }) {
  return (
    <Frame label="The command and what it printed">
      {artifact.computer ? <p className="border-b border-line bg-inset px-4 py-2 text-secondary text-muted">On {artifact.computer}</p> : null}
      <pre className="mono max-h-60 overflow-auto px-4 py-3 text-caption whitespace-pre-wrap">
        <span className="text-faint">$ </span>
        {artifact.command}
        {artifact.output ? `\n${artifact.output}` : ''}
      </pre>
    </Frame>
  );
}

export function ArtifactView({ artifact }: { artifact: StoryArtifact }) {
  switch (artifact.type) {
    case 'email':
      return <Email artifact={artifact} />;
    case 'files':
      return <Files artifact={artifact} />;
    case 'payment':
      return <Payment artifact={artifact} />;
    case 'form':
      return <Form artifact={artifact} />;
    case 'table':
      return <ResultTable artifact={artifact} />;
    case 'order':
      return <Order artifact={artifact} />;
    case 'message':
      return <Thread artifact={artifact} />;
    case 'command':
      return <Command artifact={artifact} />;
  }
}

/** The small picture in the list: the amount, how many files, or the kind's own glyph. */
export function ArtifactGlyph({ artifact }: { artifact: StoryArtifact }) {
  if (artifact.type === 'payment' || (artifact.type === 'order' && artifact.refund)) {
    const amount = artifact.type === 'payment' ? artifact.amount : artifact.total;
    return <span className="tabular max-w-full truncate px-1 text-secondary font-semibold">{amount.replace(/[.,]\d{2}$/, '')}</span>;
  }
  if (artifact.type === 'files') {
    return (
      <span className="flex flex-col items-center leading-none">
        <FileText aria-hidden="true" className="size-5 text-muted" />
        <span className="tabular mt-1 text-micro text-faint font-normal">{artifact.files.length}</span>
      </span>
    );
  }
  const Icon = { email: Mail, form: ClipboardList, table: Table2, order: ShoppingBag, message: MessageSquare, command: SquareTerminal }[artifact.type];
  return <Icon aria-hidden="true" className="size-6 text-muted" />;
}
