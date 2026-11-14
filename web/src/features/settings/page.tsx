import type { ReactNode } from 'react';

/** The heading of one settings page. */
export function SettingsHeader({ title, description, action }: { title: string; description?: ReactNode; action?: ReactNode }) {
  return (
    <div className="mb-6 grid grid-cols-[minmax(0,1fr)_auto] items-start gap-x-4">
      <h2 className="text-20 font-semibold">{title}</h2>
      {action ?? <span />}
      {description ? <p className="col-span-2 mt-1 text-14 text-muted">{description}</p> : null}
    </div>
  );
}
