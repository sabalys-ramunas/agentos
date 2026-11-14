import { Link } from '@tanstack/react-router';
import { useEngageProgram } from '@/api/hooks';
import { Card, Section } from '@/components/ui/card';
import { Status } from '@/components/ui/status';
import { planStatus } from '@/lib/labels';
import { ENGAGEMENT } from '@/lib/vocabulary';

/**
 * One quiet strip on an account's page, only when the account has a plan. Its sentence is the plan's
 * own, from the node. An account with no plan shows nothing here, not an empty card.
 */
export function EngageOnAccount({ org, ws, name }: { org: string; ws: string; name: string }) {
  const program = useEngageProgram(org, ws, name);
  const data = program.data;
  if (!data?.available || data.state === null || data.program === null) return null;
  return (
    <Section
      title={ENGAGEMENT.place.word}
      action={
        <Link to="/$org/lanes/$ws/$name/engagement" params={{ org, ws, name }} className="inline-flex min-h-8 items-center text-13 text-muted hover:text-fg md:min-h-0">
          Open {ENGAGEMENT.place.word.toLowerCase()}
        </Link>
      }
    >
      <Card className="flex min-w-0 items-center gap-3 px-4 py-3">
        <Status status={planStatus[data.state]} className="shrink-0" />
        <p className="min-w-0 truncate text-14 text-muted">{data.program.summary}</p>
      </Card>
    </Section>
  );
}
