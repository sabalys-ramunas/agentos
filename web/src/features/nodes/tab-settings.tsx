import { useNodeSettings } from '@/api/hooks';
import type { SettingRow } from '@/api/types';
import { Card } from '@/components/ui/card';
import { ErrorState, Skeleton } from '@/components/ui/states';
import { useTechnical } from '@/lib/technical';
import { NotReported, Panel } from './parts';

function shown(value: SettingRow['value']): string {
  if (value === null) return '–';
  return Array.isArray(value) ? value.join(', ') : String(value);
}

/**
 * This machine's settings, read-only: what it runs with, and where each value came from. Changing one
 * means editing its node.toml on the machine and restarting it. A secret is only ever shown as set or not.
 */
export function SettingsTab({ org, node }: { org: string; node: string }) {
  const settings = useNodeSettings(org, node);
  const [technical] = useTechnical();
  if (settings.isPending) return <Skeleton className="h-96 rounded-xl" />;
  if (settings.isError) return <ErrorState error={settings.error} onRetry={() => void settings.refetch()} />;
  if (settings.data === null) return <NotReported node={node} what="its settings" />;
  const rows = settings.data;
  return (
    <Panel title="Settings" description="Read-only. Change one in the settings file on this machine, then restart agentos there.">
      <p className="text-14">
        {rows.length} settings, read from the file on the machine and its defaults. Secret values are never shown.
      </p>
      <details open={technical} className="group">
        <summary className="flex cursor-pointer list-none items-center gap-2 py-2 text-13 text-muted hover:text-fg [&::-webkit-details-marker]:hidden">
          Technical details
        </summary>
        <Card className="mt-2 overflow-hidden">
          <dl className="divide-y divide-line">
            {rows.map((row) => (
              <div key={row.name} className="grid gap-1 px-4 py-3 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)] sm:gap-4 sm:px-6">
                <dt className="mono text-13">{row.name}</dt>
                <dd className="min-w-0">
                  <p className="mono truncate text-13 text-fg">{shown(row.value)}</p>
                  <p className="mt-1 text-12 text-muted">
                    {row.description} <span className="text-faint">From {row.source}.</span>
                  </p>
                </dd>
              </div>
            ))}
          </dl>
        </Card>
      </details>
    </Panel>
  );
}
