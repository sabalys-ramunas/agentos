/** The list half of Needs you: what to decide, grouped by what is at stake, then brand changes, codes and questions. */
import { Check, ListChecks, X } from 'lucide-react';
import type { Approval } from '@/api/types';
import { Count } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { Kbd } from '@/components/ui/misc';
import type { usePrincipalName } from '@/lib/use-principal';
import { CodeRow } from './code-card';
import { BrandRow } from './brand-proposal';
import { waitedShort } from './queue-words';
import { GroupHeader, QuestionRow, Row, batchWords, batchable } from './queue-rows';
import type { Queue } from './use-queue';

/** Which row is open, by kind: only one of them is set at a time. */
export interface OpenIds {
  approval: string | undefined;
  brand: string | undefined;
  code: string | undefined;
  question: string | undefined;
}

interface QueueCardProps {
  queue: Queue;
  open: OpenIds;
  whoOf: ReturnType<typeof usePrincipalName>;
  canDecide: boolean;
  changed: ReadonlySet<string>;
  selecting: boolean;
  selected: ReadonlySet<string>;
  onHover: (hovering: boolean) => void;
  onChooseSeveral: () => void;
  onOpen: (id: string) => void;
  onSelect: (id: string, checked: boolean) => void;
}

export function QueueCard({ queue, open, whoOf, canDecide, changed, selecting, selected, onHover, onChooseSeveral, onOpen, onSelect }: QueueCardProps) {
  const { groups, brands, codeRows, questionRows, openCodes, questions, businessOf, colorOf, oldest, waiting, toDecide } = queue;
  return (
    <Card
      className="relative min-w-0 overflow-hidden lg:sticky lg:top-[calc(var(--topbar-height)+16px)]"
      onPointerEnter={(event) => event.pointerType === 'mouse' && onHover(true)}
      onPointerLeave={() => onHover(false)}
    >
      <CardHeader
        title={
          <>
            To decide
            <Count value={toDecide} tone="warn" />
          </>
        }
        meta={oldest ? <span className="hidden sm:inline">the oldest for {waitedShort(oldest)}</span> : null}
        action={
          <span className="hidden items-center gap-1 text-caption text-faint lg:inline-flex" aria-hidden="true">
            <Kbd>J</Kbd>
            <Kbd>K</Kbd>
            move
          </span>
        }
      />
      {waiting > 0 ? (
        <div className="pointer-events-none sticky top-2 z-10 -mb-6 flex h-0 justify-center">
          <span className="tabular h-6 animate-pop-in rounded-full border border-line bg-raised px-2 text-caption leading-6 text-muted shadow-popover">
            {waiting} new when you move away
          </span>
        </div>
      ) : null}
      <div className="divide-y divide-line pb-2">
        {groups.map((group) => {
          const choosable = canDecide && group.items.some(batchable);
          return (
            <section key={group.name} aria-label={group.name}>
              <GroupHeader
                name={group.name}
                why={group.why}
                count={group.items.length}
                action={
                  choosable && group.items.filter(batchable).length > 1 ? (
                    <Button size="sm" variant="ghost" icon={<ListChecks className="size-4" />} aria-pressed={selecting} onClick={onChooseSeveral}>
                      {selecting ? 'Done choosing' : 'Choose several'}
                    </Button>
                  ) : null
                }
              />
              <ul>
                {group.items.map((item) => (
                  <li key={item.id} className="row-motion" data-motion={item.leaving ? 'out' : item.entering ? 'in' : undefined}>
                    <div>
                      <Row
                        approval={item}
                        business={businessOf(item.lane)}
                        color={colorOf(item.lane)}
                        focused={item.id === open.approval}
                        changed={changed.has(item.id)}
                        selecting={selecting}
                        selected={selected.has(item.id)}
                        onFocus={() => onOpen(item.id)}
                        onSelect={(checked) => onSelect(item.id, checked)}
                      />
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          );
        })}
        {brands.length ? (
          <section aria-label="Brand changes">
            <GroupHeader name="Brand changes" why="An agent proposes a change to a brand. Only you decide it." count={brands.length} />
            <ul>
              {brands.map((item) => (
                <li key={item.id}>
                  <BrandRow proposal={item} focused={item.id === open.brand} onFocus={() => onOpen(item.id)} />
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        {codeRows.length ? (
          <section aria-label="Login codes">
            <GroupHeader name="Login codes" why="A site asked for a code. Only you can type it." count={openCodes.length} />
            <ul>
              {codeRows.map((item) => (
                <li key={item.id} className="row-motion" data-motion={item.leaving ? 'out' : item.entering ? 'in' : undefined}>
                  <div>
                    <CodeRow card={item} business={businessOf(item.lane)} focused={item.id === open.code} onFocus={() => onOpen(item.id)} />
                  </div>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        {questionRows.length ? (
          <section aria-label="Questions">
            <GroupHeader name="Questions" why="The agent waits for your answer." count={questions.length} />
            <ul>
              {questionRows.map((item) => (
                <li key={item.id} className="row-motion" data-motion={item.leaving ? 'out' : item.entering ? 'in' : undefined}>
                  <div>
                    <QuestionRow
                      question={item}
                      business={item.lane ? businessOf(item.lane) : ''}
                      askedBy={whoOf(item.asked_by)}
                      focused={item.id === open.question}
                      onFocus={() => onOpen(item.id)}
                    />
                  </div>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </div>
    </Card>
  );
}

/** The bar that rises while several are chosen: clear them, or say no or yes to all of them at once. */
export function BatchBar({ chosen, onClear, onNo, onYes }: { chosen: Approval[]; onClear: () => void; onNo: () => void; onYes: () => void }) {
  if (chosen.length === 0) return null;
  const words = batchWords(chosen);
  return (
    <div className="sticky bottom-[calc(var(--tabbar-height)+env(safe-area-inset-bottom)+16px)] z-20 mt-6 flex animate-pop-in items-center gap-2 rounded-xl bg-inverse p-2 pl-4 text-on-inverse shadow-popover md:bottom-6">
      <span className="tabular min-w-0 flex-1 text-body">{words.chosen}</span>
      <Button size="md" variant="inverse" onClick={onClear}>
        Clear
      </Button>
      <Button size="md" variant="inverse" icon={<X className="size-4" />} onClick={onNo}>
        {words.no}
      </Button>
      <Button size="md" variant="inverse" icon={<Check className="size-4" />} onClick={onYes}>
        {words.yes}
      </Button>
    </div>
  );
}
