import type { ReactNode } from 'react';

/**
 * The head of one settings section (web/DESIGN.md, Forms): its title (26/32), one line under it, and
 * its main action at the top right. Under the page's h1 "Settings", so it is an h2.
 */
export function SettingsHeader({ title, description, action }: { title: string; description?: ReactNode; action?: ReactNode }) {
  return (
    <div className="mb-6 grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-4">
      <h2 className="text-page-title-compact [overflow-wrap:anywhere]">{title}</h2>
      {action ? <div className="row-span-2 flex flex-wrap items-center justify-end gap-2">{action}</div> : <span />}
      {description ? <p className="col-start-1 mt-1 max-w-[46em] text-body text-muted">{description}</p> : null}
    </div>
  );
}
