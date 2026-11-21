/**
 * The design system's catalog: every component in every state, by group, in the app's own tokens and
 * shell, with openajar's composites drawn from the mock. Dev only (/$org/design, app/routes/catalog.tsx):
 * a live build holds none of it (build/live-build.test.ts). How to use it: web/DESIGN-SYSTEM.md.
 */
import { Fragment } from 'react';
import { Card } from '@/components/ui/card';
import { PageHeader } from '@/components/ui/misc';
import { cn } from '@/lib/cn';
import { GROUPS } from './registry';
import type { Entry, Group } from './registry';

/** A marker only this page holds, so the live-build test can tell that none of it shipped. */
export const CATALOG_MARKER = 'openajar-design-catalog';

function EntryView({ entry }: { entry: Entry }) {
  const Demo = entry.demo!;
  return (
    <div data-catalog-entry={entry.file} className="grid min-w-0 gap-3">
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-card-title">{entry.names}</h3>
        <span className="min-w-0 truncate font-mono text-code-caption text-faint" title={entry.file}>
          {entry.file.replace(/^src\//, '')}
        </span>
      </div>
      <Demo />
    </div>
  );
}

function Shown({ entries }: { entries: Entry[] }) {
  if (!entries.length) return null;
  return (
    <Card>
      <dl className="grid divide-y divide-line px-5 py-1">
        {entries.map((entry) => (
          <div key={`${entry.file}-${entry.names}`} data-catalog-entry={entry.file} className="grid gap-1 py-2 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] md:gap-4">
            <dt className="min-w-0 text-secondary">
              <span className="text-fg">{entry.names}</span>
              <span className="block truncate font-mono text-code-caption text-faint" title={entry.file}>
                {entry.file.replace(/^src\//, '')}
              </span>
            </dt>
            <dd className="min-w-0 text-secondary text-muted">Seen in {entry.shownBy}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

function GroupView({ group }: { group: Group }) {
  const drawn = group.entries.filter((entry) => entry.demo);
  return (
    <section id={group.id} aria-labelledby={`${group.id}-h`} className="grid scroll-mt-20 gap-6 border-t border-line pt-8">
      <div className="grid gap-1">
        <h2 id={`${group.id}-h`} className="text-section-title">
          {group.title}
        </h2>
        <p className="max-w-3xl text-secondary text-muted">
          {group.purpose} <span className="font-mono text-code-caption text-faint">web/design-system/{group.id}.md</span>
        </p>
      </div>
      {drawn.map((entry) => (
        <Fragment key={`${entry.file}-${entry.names}`}>
          <EntryView entry={entry} />
        </Fragment>
      ))}
      <Shown entries={group.entries.filter((entry) => entry.shownBy)} />
    </section>
  );
}

export function CatalogPage() {
  return (
    <div data-catalog={CATALOG_MARKER} className="grid gap-10 pb-16">
      <PageHeader
        title="The design system"
        description="Every component in every state, by group, in the app’s own tokens. Open it in both themes at 390 and 1440 before you change one. web/DESIGN-SYSTEM.md says how to build a screen with it."
      />
      <nav aria-label="Groups" className="-mt-4 flex flex-wrap gap-2">
        {GROUPS.map((group) => (
          <a key={group.id} href={`#${group.id}`} className={cn('rounded-full border border-line px-3 py-1 text-secondary text-muted hover:border-line-strong hover:text-fg')}>
            {group.title}
          </a>
        ))}
      </nav>
      {GROUPS.map((group) => (
        <GroupView key={group.id} group={group} />
      ))}
    </div>
  );
}
