/**
 * Closed environments as a table: what each was, how it ended, which agent, how long it ran, what it cost and
 * when it closed. A row opens its page, and so does Details. Titles cut with their full text as a title; on a
 * phone the rows stack.
 */
import { Link, useNavigate } from '@tanstack/react-router';
import type { EnvSummary } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { CellTitle, NoValue, Table, TableScroll, Td, Th, Tr } from '@/components/ui/table';
import { dateTime, timeAgo } from '@/lib/format';
import { EnvStatusPill } from './status-pill';
import { ENV_STATE_SENTENCE, ENV_STATUS, ageOf, agentWords, euro, hourly, repoName, span } from './words';

export function ClosedTable({ org, rows, now }: { org: string; rows: EnvSummary[]; now: number }) {
  const navigate = useNavigate();
  return (
    <Card className="overflow-hidden">
      <TableScroll>
        <Table stack aria-label="Closed environments">
          <thead>
            <tr>
              <Th>Environment</Th>
              <Th>Result</Th>
              <Th phone="hide">Agent</Th>
              <Th align="end">Ran</Th>
              <Th align="end">Cost</Th>
              <Th>Closed</Th>
              <Th className="w-1" phone="hide">
                <span className="sr-only">Details</span>
              </Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((env) => (
              <Tr
                key={env.id}
                onActivate={() => void navigate({ to: '/$org/environments/$id', params: { org, id: env.id } })}
                aria-label={`${env.title}, ${ENV_STATUS[env.state].label}`}
              >
                <Td main>
                  <CellTitle
                    title={
                      <span className="block truncate" title={env.title}>
                        {env.title}
                      </span>
                    }
                    second={
                      env.repo ? (
                        <span className="font-mono text-code-caption" title={env.repo}>
                          {repoName(env.repo)}
                        </span>
                      ) : (
                        'No repository'
                      )
                    }
                  />
                </Td>
                <Td>
                  <span title={ENV_STATE_SENTENCE[env.state]}>
                    <EnvStatusPill state={env.state} />
                  </span>
                </Td>
                <Td phone="hide">
                  <span className="block max-w-44 truncate text-fg" title={agentWords(env.agent)}>
                    {env.agent.name}
                  </span>
                </Td>
                <Td numeric label="Ran">
                  <span title={`Opened ${dateTime(env.created_at)}`}>{span(ageOf(env, now))}</span>
                </Td>
                <Td money label="Cost">
                  <span title={hourly(env.price_hourly_eur)}>{euro(env.cost_eur)}</span>
                </Td>
                <Td label="Closed">
                  {env.closed_at ? (
                    <time dateTime={env.closed_at} title={dateTime(env.closed_at)} className="whitespace-nowrap">
                      {timeAgo(env.closed_at, now)}
                    </time>
                  ) : (
                    <NoValue />
                  )}
                </Td>
                <Td className="w-1 text-right" phone="hide" onClick={(event) => event.stopPropagation()}>
                  <Button asChild variant="ghost" size="sm">
                    <Link to="/$org/environments/$id" params={{ org, id: env.id }} aria-label={`Details of ${env.title}`}>
                      Details
                    </Link>
                  </Button>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </TableScroll>
    </Card>
  );
}
