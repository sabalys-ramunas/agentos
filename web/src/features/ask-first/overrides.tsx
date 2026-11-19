/**
 * The safety checks a person turned off for one account, and why. It is technical, so it shows only
 * with Technical details on, and only to the owner: the node refuses anyone else, and the screen
 * never asks them.
 */
import { useAccess, useOverrides } from '@/api/hooks';
import { timeAgo } from '@/lib/format';
import { useTechnical } from '@/lib/technical';

/** Who turned a check off, in words. A person is never shown as an id. */
export function overrideBy(by: string): string {
  if (by.startsWith('owner')) return 'the owner';
  if (by.startsWith('agent')) return 'an agent';
  return 'a person';
}

export function AccountOverrides({ org, laneId }: { org: string; laneId: string }) {
  const [technical] = useTechnical();
  const access = useAccess(org);
  const allowed = technical && access.can('overrides.read');
  const list = useOverrides(org, allowed);
  if (!allowed || !list.data) return null;
  const mine = list.data.filter((item) => item.lane === laneId);
  if (mine.length === 0) return null;
  return (
    <section aria-label="Overrides" className="mt-3 grid gap-2 border-t border-line pt-3">
      <h3 className="text-secondary text-faint font-semibold">Overrides</h3>
      <ul className="grid gap-2">
        {mine.map((item) => (
          <li key={item.id} className="text-secondary text-muted">
            <span className="text-fg">{item.rules.length ? item.rules.join(', ') : 'A proxy check'}</span> was turned off
            {item.proxy ? ` on ${item.proxy}` : ''} {timeAgo(item.at)} by {overrideBy(item.by)}.{' '}
            {item.reason ? <span>Reason: {item.reason}</span> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
