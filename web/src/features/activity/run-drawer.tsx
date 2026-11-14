import { useRun } from '@/api/hooks';
import type { HistoryStory } from '@/api/types';
import { Drawer } from '@/components/ui/dialog';
import { ErrorState, Skeleton } from '@/components/ui/states';
import { StoryDetail, useRunView } from './run-story';
import { fromHistory } from './story-view';

export function StorySkeleton() {
  return (
    <div className="grid gap-4" role="status" aria-label="Loading">
      <Skeleton className="w-40" />
      <Skeleton className="h-6 w-3/4" />
      <Skeleton className="w-2/3" />
      <Skeleton className="h-48 rounded-xl" />
    </div>
  );
}

/** The chosen story: from History's loaded days when it is there, else its run read on its own. */
export function Chosen({ org, id, stories }: { org: string; id: string; stories?: Map<string, HistoryStory> }) {
  const known = stories?.get(id);
  // A story still working is read as its run too, so it can be stopped and its end seen at once.
  const live = known?.verdict === 'running';
  const run = useRun(org, known && !live ? null : id);
  const told = useRunView(org, run.data);
  const view = known && !(live && told) ? fromHistory(known) : told;
  if (!view) return run.isError ? <ErrorState error={run.error} onRetry={() => void run.refetch()} /> : <StorySkeleton />;
  // One StoryDetail in one place whichever way the story came, so an open fold stays open.
  return <StoryDetail org={org} view={view} run={run.data} />;
}

interface RunDrawerProps {
  org: string;
  id: string | null;
  onClose: () => void;
  /** History's loaded stories, so a story it already has opens without a second request. */
  stories?: Map<string, HistoryStory>;
}

/** One thing that happened, as a story, in a drawer: History on a narrow screen, and a routine's runs. */
export function RunDrawer({ org, id, onClose, stories }: RunDrawerProps) {
  return (
    <Drawer open={id !== null} onOpenChange={(open) => !open && onClose()} title="What happened">
      {id ? <Chosen org={org} id={id} stories={stories} /> : null}
    </Drawer>
  );
}
