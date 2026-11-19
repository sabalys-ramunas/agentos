import { Link } from '@tanstack/react-router';
import { CalendarClock } from 'lucide-react';
import { useState } from 'react';
import type { NodeSchedule, ScheduleFire } from '@/api/types';
import { Card } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/states';
import { Table, Td, Th, Tr, mainCell } from '@/components/ui/table';
import { ScheduleTimeline } from '@/features/flows/schedule-timeline';
import { NextFire, PauseSwitch, triggerOf } from '@/features/flows/schedules';
import { cn } from '@/lib/cn';
import { timeAgo } from '@/lib/format';
import { runStatus } from '@/lib/labels';
import { NotReported, Panel } from './parts';

const FIRE_COLORS: Partial<Record<ScheduleFire['status'], string>> = {
  succeeded_verified: 'var(--mark-verified)',
  succeeded_unverified: 'var(--mark-unverified)',
  partial: 'var(--mark-partial)',
  failed: 'var(--mark-failed)',
};

const fireWord = (fire: ScheduleFire) => (fire.status === 'missed' ? 'Missed' : runStatus[fire.status].label);

/**
 * The last fires, oldest to newest. A missed fire is an empty red frame: the schedule was due
 * and nothing ran, which is the thing a dead-man alert exists for.
 */
function FireStrip({ fires }: { fires: ScheduleFire[] }) {
  const [hover, setHover] = useState<ScheduleFire | null>(null);
  const ordered = [...fires].reverse();
  const missed = fires.filter((fire) => fire.status === 'missed').length;
  const failed = fires.filter((fire) => fire.status === 'failed').length;
  const summary = `Last ${fires.length} fires: ${[missed && `${missed} missed`, failed && `${failed} failed`].filter(Boolean).join(', ') || 'all ran'}`;
  if (fires.length === 0) return <span className="text-secondary text-faint">Not fired yet</span>;
  return (
    <span className="inline-flex items-center gap-3">
      <span role="img" aria-label={summary} className="inline-flex h-4 items-center gap-px" onPointerLeave={() => setHover(null)}>
        {ordered.map((fire) => (
          <span
            key={fire.at}
            onPointerEnter={() => setHover(fire)}
            className={cn(
              'block h-4 w-2 rounded-[2px] transition-opacity duration-(--duration-fast) ease-standard',
              fire.status === 'missed' && 'border border-bad',
            )}
            style={{ backgroundColor: FIRE_COLORS[fire.status] ?? 'transparent', opacity: hover && hover !== fire ? 0.35 : 1 }}
          />
        ))}
      </span>
      {/* Fixed width, so the row never moves while the pointer reads the strip. */}
      <span aria-hidden="true" className="tabular hidden w-36 truncate text-caption text-faint xl:inline">
        {hover ? `${fireWord(hover)} · ${timeAgo(hover.at)}` : missed ? `${missed} missed` : failed ? `${failed} failed` : 'All ran'}
      </span>
    </span>
  );
}

export function SchedulesTab({ org, node, zone, schedules, canOperate }: { org: string; node: string; zone: string; schedules: NodeSchedule[] | null; canOperate: boolean }) {
  const [active, setActive] = useState<string | null>(null);
  if (schedules === null) return <NotReported node={node} what="its schedules" />;
  if (schedules.length === 0) {
    return (
      <Card>
        <EmptyState icon={<CalendarClock />} title="Nothing scheduled here" description="Schedules for routines on this machine's accounts show here, on the machine's own clock." />
      </Card>
    );
  }
  const missed = schedules.filter((item) => item.recent_fires.some((fire) => fire.status === 'missed'));
  return (
    <div className="grid gap-10">
      <ScheduleTimeline org={org} schedules={schedules} zoneOf={() => zone} fallback={zone} active={active} onActive={setActive} />
      <Panel
        title="Every schedule"
        description={missed.length ? `${missed.length} missed a fire recently. A missed fire means it was due and nothing ran.` : 'Each one with its recent fires, oldest on the left.'}
      >
        <Card className="overflow-hidden">
          <Table>
            <thead className="hidden md:table-header-group">
              <tr>
                <Th>Flow</Th>
                <Th className="hidden lg:table-cell">When</Th>
                <Th>Recent fires</Th>
                <Th className="w-44">Next</Th>
                <Th className="hidden xl:table-cell">Alert if quiet</Th>
                <Th className="w-16">
                  <span className="sr-only">Runs on schedule</span>
                </Th>
              </tr>
            </thead>
            <tbody>
              {schedules.map((item) => (
                <Tr
                  key={item.id}
                  className={cn('hover:bg-hover', active === item.id && 'bg-hover')}
                  onPointerEnter={() => setActive(item.id)}
                  onPointerLeave={() => setActive(null)}
                >
                  <Td className={cn(mainCell, 'min-w-48')}>
                    <Link to="/$org/flows/$flow" params={{ org, flow: item.flow }} className="mono block truncate text-secondary hover:underline">
                      {item.flow}
                    </Link>
                    <p className="truncate text-caption text-faint lg:hidden">{triggerOf(item)}</p>
                    <div className="mt-1 md:hidden">
                      <FireStrip fires={item.recent_fires} />
                    </div>
                  </Td>
                  <Td className="hidden text-secondary text-muted lg:table-cell">
                    <span title={item.cron ?? undefined}>{triggerOf(item)}</span>
                  </Td>
                  <Td className="hidden md:table-cell">
                    <FireStrip fires={item.recent_fires} />
                  </Td>
                  <Td className="hidden text-secondary text-muted md:table-cell">
                    <NextFire schedule={item} zone={zone} />
                  </Td>
                  <Td className="tabular hidden text-secondary text-muted xl:table-cell">{item.deadman_minutes ? `After ${item.deadman_minutes} min` : '–'}</Td>
                  <Td>{canOperate ? <PauseSwitch org={org} schedule={item} /> : null}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </Card>
      </Panel>
    </div>
  );
}
