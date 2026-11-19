/** The settings of one step, or of how the routine starts, in the words a person uses. */
import { Check, Lock, ShieldAlert } from 'lucide-react';
import type { ReactNode } from 'react';
import type { Flow, RoutineCatalog, RoutineCondition, RoutineDefinition, RoutineSendLimit, RoutineStep, RoutineStepRun, RoutineTrigger } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { Field, Input } from '@/components/ui/field';
import { CopyButton, Meter } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { cn } from '@/lib/cn';
import { describeCron } from '@/lib/cron';
import { plural } from '@/lib/format';
import { BUILT_TRIGGERS, COMPARE_WORDS, dataBefore, isSendStep, operationFor, refOf, STEP_WORDS, TRIGGER_TARGET, TRIGGER_WORDS } from '@/lib/routines';
import type { DataSource } from '@/lib/routines';
import { useTechnical } from '@/lib/technical';
import { EFFECT_WORDS } from '@/lib/vocabulary';
import { DataField, withoutDeadRefs } from './data-field';
import { TRIGGER_ICON } from './flow-map';

const RHYTHMS = [
  { value: '1-5', label: 'Every weekday' },
  { value: '*', label: 'Every day' },
  { value: '1', label: 'Every Monday' },
  { value: '2', label: 'Every Tuesday' },
  { value: '3', label: 'Every Wednesday' },
  { value: '4', label: 'Every Thursday' },
  { value: '5', label: 'Every Friday' },
];

/** A cron line as the two things a person would choose. Anything else stays for the technical. */
function scheduleParts(cron: string | null | undefined): { days: string; time: string } | null {
  const match = /^(\d+) (\d+) \* \* (\S+)$/.exec(cron?.trim() ?? '');
  if (!match) return null;
  return { days: match[3] ?? '*', time: `${(match[2] ?? '0').padStart(2, '0')}:${(match[1] ?? '0').padStart(2, '0')}` };
}

function Trigger({ trigger, onChange, catalog, workspace }: { trigger: RoutineTrigger; onChange: (next: RoutineTrigger) => void; catalog: RoutineCatalog | undefined; workspace: string | null }) {
  const [technical] = useTechnical();
  const parts = scheduleParts(trigger.cron) ?? { days: '1-5', time: '09:00' };
  const setSchedule = (days: string, time: string) => {
    const [hour = '9', minute = '0'] = time.split(':');
    const cron = `${Number(minute)} ${Number(hour)} * * ${days}`;
    onChange({ ...trigger, cron, label: null });
  };
  const mail = [...new Set((catalog?.operations ?? []).filter((item) => item.workspace === workspace && item.tags.some((tag) => tag.startsWith('mail'))).map((item) => item.lane))];
  return (
    <div className="grid gap-4">
      <div role="radiogroup" aria-label="How it starts" className="grid gap-1">
        {/* A computer offers only the ways it can run a routine, and keeps the one a saved routine already has. */}
        {BUILT_TRIGGERS.filter((kind) => !catalog?.triggers || catalog.triggers.includes(kind) || kind === trigger.kind).map((kind) => {
          const Icon = TRIGGER_ICON[kind];
          const chosen = trigger.kind === kind;
          return (
            <button
              key={kind}
              type="button"
              role="radio"
              aria-checked={chosen}
              onClick={() => !chosen && onChange({ kind, label: null, cron: kind === 'schedule' ? '0 9 * * 1-5' : null })}
              className={cn('flex items-start gap-3 rounded-lg border p-2 text-left transition-colors duration-(--duration-fast) ease-standard', chosen ? 'border-fg' : 'border-transparent hover:bg-hover')}
            >
              <Icon aria-hidden="true" className="translate-y-0.5 size-4 shrink-0 text-muted" />
              <span className="min-w-0 flex-1">
                <span className="block text-label">{TRIGGER_WORDS[kind].word}</span>
                {chosen ? <span className="block text-secondary text-pretty text-muted">{TRIGGER_WORDS[kind].hint}</span> : null}
              </span>
              {chosen ? <Check aria-hidden="true" className="translate-y-0.5 size-4 shrink-0" /> : null}
            </button>
          );
        })}
      </div>
      {trigger.kind === 'schedule' ? (
        <div className="grid grid-cols-[minmax(0,1fr)_7rem] gap-2">
          <Field label="When">
            <Select value={parts.days} onValueChange={(days) => setSchedule(days, parts.time)} options={RHYTHMS.some((item) => item.value === parts.days) ? RHYTHMS : [...RHYTHMS, { value: parts.days, label: describeCron(trigger.cron ?? '') }]} />
          </Field>
          <Field label="At">
            <Input type="time" value={parts.time} onChange={(event) => setSchedule(parts.days, event.target.value)} />
          </Field>
        </div>
      ) : null}
      {trigger.kind === 'schedule' && technical ? (
        <Field label="Cron line">
          <Input mono value={trigger.cron ?? ''} onChange={(event) => onChange({ ...trigger, cron: event.target.value, label: null })} />
        </Field>
      ) : null}
      {trigger.kind === 'webhook' ? (
        <div className="grid gap-2 rounded-lg border border-line p-3">
          <p className="text-secondary font-medium">The private address</p>
          {trigger.hook ? (
            <p className="flex items-center gap-2">
              <span className="mono min-w-0 flex-1 truncate text-caption">{trigger.hook.url}</span>
              <CopyButton text={trigger.hook.url} label="Copy the address" />
            </p>
          ) : (
            <p className="text-secondary text-muted">You get it once this is saved, with a signing secret shown one time.</p>
          )}
          <p className="flex items-start gap-2 text-caption text-pretty text-faint">
            <Lock aria-hidden="true" className="size-3 shrink-0 translate-y-0.5" />
            Only signed requests get through, and a replay is ignored. Your computer stays closed to the internet.
          </p>
        </div>
      ) : null}
      {trigger.kind === 'email' ? (
        <>
          <Field label="Which mail account">
            <Select placeholder="Choose an account" value={trigger.lane ?? ''} onValueChange={(lane) => onChange({ ...trigger, lane })} options={mail.map((lane) => ({ value: lane, label: lane.split('/')[1] ?? lane }))} />
          </Field>
          <Field label="Only from" hint="Leave empty for every email.">
            <Input value={trigger.from ?? ''} onChange={(event) => onChange({ ...trigger, from: event.target.value })} placeholder="orders@yourshop.example" />
          </Field>
        </>
      ) : null}
      {trigger.kind === 'page_change' ? (
        <Field label="Which page">
          <Input value={trigger.url ?? ''} onChange={(event) => onChange({ ...trigger, url: event.target.value })} placeholder="yourshop.example/prices" />
        </Field>
      ) : null}
      {trigger.kind === 'email' || trigger.kind === 'webhook' || trigger.kind === 'page_change' ? (
        <p className="flex items-start gap-2 text-secondary text-pretty text-muted">
          <ShieldAlert aria-hidden="true" className="size-4 shrink-0 translate-y-0.5 text-faint" />
          What comes in here is written by someone outside. It can fill in words, and never decides what this routine does.
        </p>
      ) : null}
    </div>
  );
}

/** What a send step can and cannot do. Never a setting, so it never has a control. */
function SendRules({ limit, listSuppressed }: { limit: RoutineSendLimit | undefined; listSuppressed: number | null }) {
  const daily = limit?.daily_limit ?? 200;
  const sent = limit?.sent_today ?? 0;
  return (
    <div className="grid gap-2 rounded-lg bg-inset p-3">
      <p className="flex items-center gap-2 text-secondary font-medium">
        <Lock aria-hidden="true" className="size-3 text-faint" />
        Always true for this step
      </p>
      <div className="flex items-center gap-2">
        <Meter value={sent} max={daily} label="Sent today" className="w-24 shrink-0" />
        <span className="tabular text-caption text-muted">
          {sent} of {daily} today
        </span>
      </div>
      <ul className="grid gap-1 text-secondary text-muted">
        <li>Stops at {daily} a day</li>
        <li>Skips {listSuppressed === null ? 'anyone' : plural(listSuppressed, 'person', 'people')} who asked not to hear from you</li>
        <li>Every email carries an unsubscribe link</li>
      </ul>
    </div>
  );
}

function Condition({ condition, onChange, definition, id, catalog }: { condition: RoutineCondition; onChange: (next: RoutineCondition) => void; definition: RoutineDefinition; id: string; catalog: RoutineCatalog | undefined }) {
  const sources = dataBefore(definition, id, catalog);
  const options = sources.flatMap((source) => source.fields.filter((field) => !field.untrusted).map((field) => ({ value: refOf(source.step, field.key), label: field.label, hint: source.label })));
  const blocked = sources.flatMap((source) => source.fields.filter((field) => field.untrusted));
  const needsValue = condition.op !== 'is_empty' && condition.op !== 'not_empty';
  return (
    <div className="grid gap-3">
      <Field label="Look at">
        <Select placeholder="Choose what to check" value={condition.left ?? ''} onValueChange={(left) => onChange({ ...condition, left })} options={options} />
      </Field>
      <div className={cn('grid gap-2', needsValue && 'grid-cols-2')}>
        <Field label="It">
          <Select value={condition.op} onValueChange={(op) => onChange({ ...condition, op: op as RoutineCondition['op'] })} options={Object.entries(COMPARE_WORDS).map(([value, label]) => ({ value, label }))} />
        </Field>
        {needsValue ? (
          <Field label="This">
            <Input value={condition.right} onChange={(event) => onChange({ ...condition, right: event.target.value })} />
          </Field>
        ) : null}
      </div>
      {blocked.length ? <p className="text-secondary text-pretty text-faint">{blocked.map((field) => field.label).join(', ')} came from outside, so it cannot decide this.</p> : null}
    </div>
  );
}

/**
 * A new action keeps only the values its inputs still take, and data that no longer points at anything a step
 * can read is taken out. A send list stays only for an action that sends.
 */
export function changeAction(current: RoutineStep, id: string, catalog: RoutineCatalog | undefined, sources: DataSource[]): RoutineStep {
  if (current.kind !== 'action') return current;
  const next = catalog?.operations.find((item) => item.id === id);
  const takes = new Set(Object.keys(next?.params ?? {}));
  const inputs = Object.fromEntries(
    Object.entries(current.inputs)
      .filter(([key]) => takes.has(key))
      .map(([key, value]) => [key, withoutDeadRefs(value, sources)]),
  );
  const { send, ...rest } = current;
  return { ...rest, operation: id, inputs, ...(next?.sends && send ? { send } : {}) };
}

const pretty = (value: unknown) => (Array.isArray(value) ? value.join(', ') : String(value));

/** What this step really took in and gave out last time. */
function LastRun({ ran }: { ran: RoutineStepRun | undefined }) {
  const [technical] = useTechnical();
  if (!ran || (!ran.input && !ran.output)) return <p className="text-secondary text-muted">Its data shows here after the first run.</p>;
  const block = (title: string, values: Record<string, unknown> | null) =>
    values ? (
      <div className="min-w-0">
        <p className="silk mb-1">{title}</p>
        {technical ? (
          <pre className="mono overflow-x-auto rounded-md bg-inset p-2 text-caption">{JSON.stringify(values, null, 2)}</pre>
        ) : (
          <dl className="grid gap-1">
            {Object.entries(values)
              .filter(([, value]) => value !== null)
              .map(([key, value]) => (
                <div key={key} className="flex justify-between gap-4 text-secondary">
                  <dt className="shrink-0 text-faint">{key.replaceAll('_', ' ')}</dt>
                  <dd className="min-w-0 truncate">{pretty(value)}</dd>
                </div>
              ))}
          </dl>
        )}
      </div>
    ) : null;
  return (
    <div className="grid gap-3">
      {block('Went in', ran.input)}
      {block('Came out', ran.output)}
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="grid gap-3 border-t border-line pt-4">
      <h3 className="text-secondary font-medium text-muted">{title}</h3>
      {children}
    </section>
  );
}

interface InspectorProps {
  target: string;
  step: RoutineStep | null;
  definition: RoutineDefinition;
  flow: Flow | null;
  catalog: RoutineCatalog | undefined;
  ran: RoutineStepRun | undefined;
  onStep: (patch: (step: RoutineStep) => RoutineStep, key?: string) => void;
  onTrigger: (trigger: RoutineTrigger) => void;
}

export function Inspector({ target, step, definition, flow, catalog, ran, onStep, onTrigger }: InspectorProps) {
  const [technical] = useTechnical();
  if (target === TRIGGER_TARGET) {
    return (
      <div className="grid gap-4">
        <h2 className="text-section-title">How it starts</h2>
        <Trigger trigger={definition.trigger} onChange={onTrigger} catalog={catalog} workspace={definition.workspace} />
      </div>
    );
  }
  if (!step) return null;
  const sources = dataBefore(definition, step.id, catalog);
  const operation = operationFor(catalog, step);
  const operations = (catalog?.operations ?? []).filter((item) => item.workspace === definition.workspace);
  const sends = step.kind === 'action' && isSendStep(step, catalog);
  const list = step.kind === 'action' ? catalog?.lists.find((item) => item.id === step.send?.list) : undefined;
  return (
    <div className="grid gap-4">
      <div>
        <p className="silk">{STEP_WORDS[step.kind].silk}</p>
        <h2 className="mt-1 text-section-title">{step.label.charAt(0).toUpperCase() + step.label.slice(1)}</h2>
      </div>
      {step.kind === 'action' ? (
        <>
          <Field label="What it does">
            <Select
              placeholder="Choose what it does"
              value={step.operation ?? ''}
              onValueChange={(id) => onStep((current) => changeAction(current, id, catalog, sources))}
              options={operations.map((item) => ({ value: item.id, label: item.name, hint: `${item.lane.split('/')[1] ?? ''} · ${EFFECT_WORDS[item.effect].word}` }))}
            />
          </Field>
          {operation ? (
            <p className="flex items-center gap-2 text-secondary text-muted">
              <SiteMark site={operation.lane} size={16} label={false} />
              {EFFECT_WORDS[operation.effect].word}
              {operation.effect === 'pay' || operation.effect === 'irreversible' ? (
                <span className="inline-flex items-center gap-1 text-fg">
                  <Lock aria-hidden="true" className="size-3" />
                  Always asks you
                </span>
              ) : null}
            </p>
          ) : null}
          {sends ? (
            <>
              <Field label="Who it goes to" hint="Only lists you gave agentos. It can't find or buy addresses.">
                <Select
                  placeholder="Choose a list"
                  value={step.send?.list ?? ''}
                  onValueChange={(id) => onStep((current) => ({ ...current, send: { list: id } } as RoutineStep))}
                  options={(catalog?.lists ?? []).filter((item) => item.workspace === definition.workspace).map((item) => ({ value: item.id, label: item.name, hint: `${item.size.toLocaleString('en')} people` }))}
                />
              </Field>
              <SendRules limit={flow?.send_limits?.find((item) => item.step === step.id)} listSuppressed={list?.suppressed ?? null} />
            </>
          ) : null}
          {Object.entries(operation?.params ?? {}).map(([key, param]) => (
            <DataField
              key={`${step.id}.${key}`}
              label={param.label ?? key}
              multiline={key === 'body' || key === 'caption'}
              sources={sources}
              value={step.inputs[key] ?? ''}
              onChange={(value) => onStep((current) => ({ ...current, inputs: { ...(current as typeof step).inputs, [key]: value } } as RoutineStep), `${step.id}.${key}`)}
            />
          ))}
        </>
      ) : null}
      {step.kind === 'branch' ? <Condition condition={step.when} definition={definition} id={step.id} catalog={catalog} onChange={(when) => onStep((current) => ({ ...current, when } as RoutineStep))} /> : null}
      {step.kind === 'wait' ? (
        <div className="grid grid-cols-2 gap-2">
          <Field label="For">
            <Input type="number" min={1} value={step.amount} onChange={(event) => onStep((current) => ({ ...current, amount: Number(event.target.value) } as RoutineStep), `${step.id}.amount`)} />
          </Field>
          <Field label="Unit">
            <Select value={step.unit} onValueChange={(unit) => onStep((current) => ({ ...current, unit } as RoutineStep))} options={[{ value: 'minutes', label: 'Minutes' }, { value: 'hours', label: 'Hours' }, { value: 'days', label: 'Days' }]} />
          </Field>
        </div>
      ) : null}
      {step.kind === 'loop' ? (
        <>
          <Field label="Repeat for each thing in">
            <Select
              placeholder="Choose a list"
              value={step.over ?? ''}
              onValueChange={(over) => onStep((current) => ({ ...current, over } as RoutineStep))}
              options={sources.flatMap((source) => source.fields.filter((field) => field.type === 'list').map((field) => ({ value: refOf(source.step, field.key), label: field.label })))}
            />
          </Field>
          <Field label="One of them is called">
            <Input value={step.item} onChange={(event) => onStep((current) => ({ ...current, item: event.target.value } as RoutineStep), `${step.id}.item`)} />
          </Field>
        </>
      ) : null}
      {step.kind === 'ask' ? (
        <>
          <DataField label="What to ask" multiline sources={sources} value={step.question} onChange={(question) => onStep((current) => ({ ...current, question } as RoutineStep), `${step.id}.question`)} />
          <p className="flex items-start gap-2 text-secondary text-pretty text-muted">
            <Lock aria-hidden="true" className="size-4 shrink-0 translate-y-0.5 text-faint" />
            The routine stops here until someone says yes in Needs you.
          </p>
        </>
      ) : null}
      {step.kind === 'notify' ? <DataField label="What to say" multiline sources={sources} value={step.message} onChange={(message) => onStep((current) => ({ ...current, message } as RoutineStep), `${step.id}.message`)} /> : null}
      <Field label="How it reads in the sentence">
        <Input value={step.label} onChange={(event) => onStep((current) => ({ ...current, label: event.target.value }), `${step.id}.label`)} />
      </Field>
      <Section title="Last time">
        <LastRun ran={ran} />
      </Section>
      {technical ? (
        <p className="mono text-caption break-all text-faint">
          {step.id}
          {step.kind === 'action' && step.operation ? ` · ${step.operation}` : ''}
        </p>
      ) : null}
    </div>
  );
}

