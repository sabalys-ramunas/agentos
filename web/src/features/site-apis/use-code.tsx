/**
 * The door for a developer: a call to try with a form built from the endpoint's input schema, the
 * real answer with how it was served and how long it took, then the same call as a snippet and the
 * OpenAPI document. A try from here runs as the signed-in person, so no key is needed, and a try that
 * fails shows the same answer a caller would get.
 */
import { ArrowRight, FileJson } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useSiteTry } from '@/api/hooks';
import type { SiteApiDetail, SiteEndpoint, SiteInputField, SiteTryResult } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Drawer } from '@/components/ui/dialog';
import { Field, Input } from '@/components/ui/field';
import { CopyButton } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { Tab, TabList, TabPanel, Tabs } from '@/components/ui/tabs';
import { Checkbox } from '@/components/ui/toggle';
import { Tooltip } from '@/components/ui/menu';
import { cn } from '@/lib/cn';
import { callUrl, defaultsOf, EFFECT_WORD, openApiOf, snippet, speed } from './words';
import type { SnippetKind } from './words';

const SNIPPETS: [SnippetKind, string][] = [
  ['curl', 'curl'],
  ['javascript', 'JavaScript'],
  ['python', 'Python'],
  ['n8n', 'n8n'],
];

function InputField({ field, value, onChange }: { field: SiteInputField; value: string; onChange: (value: string) => void }) {
  return (
    <Field label={field.required ? field.label : `${field.label} (optional)`} hint={field.hint}>
      {field.type === 'choice' ? (
        <Select value={value} onValueChange={onChange} options={field.options ?? []} />
      ) : (
        <Input type={field.type === 'number' ? 'number' : field.type === 'date' ? 'date' : 'text'} value={value} onChange={(event) => onChange(event.target.value)} />
      )}
    </Field>
  );
}

/** JSON with its keys quiet. Each line is plain text: values from a page are never markup. */
function Json({ value }: { value: unknown }) {
  const lines = useMemo(() => JSON.stringify(value, null, 2).split('\n'), [value]);
  return (
    <pre className="mono max-h-96 overflow-auto p-4 text-caption leading-5">
      {lines.map((line, index) => {
        const match = /^(\s*)("[^"]+")(:\s*)(.*)$/.exec(line);
        return (
          <span key={index} className="block">
            {match ? (
              <>
                {match[1]}
                <span className="text-faint">{match[2]}</span>
                <span className="text-faint">{match[3]}</span>
                {match[4]}
              </>
            ) : (
              <span className="text-faint">{line}</span>
            )}
          </span>
        );
      })}
    </pre>
  );
}

function Timing({ result, endpoint }: { result: SiteTryResult; endpoint: SiteEndpoint }) {
  const body = result.body as { cache?: { hit: boolean; age_s: number } };
  const hit = body.cache?.hit ?? false;
  const longest = Math.max(result.ms, endpoint.p50_ms ?? 0);
  const bar = (ms: number, on: boolean) => (
    <span aria-hidden="true" className="h-1 flex-1 overflow-hidden rounded-full bg-pressed">
      <span className={cn('block h-full rounded-full', on ? 'bg-fg' : 'bg-line-strong')} style={{ width: `${Math.max(1.5, (ms / longest) * 100)}%` }} />
    </span>
  );
  return (
    <div className="grid gap-1 border-b border-line px-4 py-3">
      <div className="flex items-center gap-3 text-caption">
        <span className="w-24 shrink-0 text-muted">This call</span>
        {bar(result.ms, true)}
        <span className="tabular w-16 shrink-0 text-right font-medium">{speed(result.ms)}</span>
      </div>
      {hit && endpoint.p50_ms !== null ? (
        <div className="flex items-center gap-3 text-caption text-faint">
          <span className="w-24 shrink-0">A fresh read</span>
          {bar(endpoint.p50_ms, false)}
          <span className="tabular w-16 shrink-0 text-right">{speed(endpoint.p50_ms)}</span>
        </div>
      ) : null}
    </div>
  );
}

function Answer({ result, endpoint }: { result: SiteTryResult; endpoint: SiteEndpoint }) {
  const body = result.body as { cache?: { hit: boolean; age_s: number }; detail?: string; code?: string };
  const ok = result.status < 400;
  return (
    <div className="min-w-0">
      <div className="flex h-11 items-center gap-2 border-b border-line px-4">
        <span className="mono text-secondary font-medium">{result.status}</span>
        <span className="min-w-0 flex-1 truncate text-secondary text-muted">
          {ok ? (body.cache?.hit ? `From a saved answer, ${body.cache.age_s} s old. It did not touch the site.` : 'Read fresh from the site. It used 1 read of this account\'s pace.') : body.detail}
        </span>
        <CopyButton text={JSON.stringify(result.body, null, 2)} label="Copy the answer" />
      </div>
      {ok ? <Timing result={result} endpoint={endpoint} /> : null}
      <Json value={result.body} />
      <details className="group border-t border-line">
        <summary className="cursor-pointer px-4 py-2 text-caption text-faint transition-colors duration-(--duration-fast) ease-standard hover:text-fg">Headers</summary>
        <dl className="mono grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 px-4 pb-3 text-caption">
          {Object.entries(result.headers).map(([name, value]) => (
            <div key={name} className="contents">
              <dt className="text-faint">{name}</dt>
              <dd className="truncate">{value}</dd>
            </div>
          ))}
        </dl>
      </details>
    </div>
  );
}

function Waiting() {
  return (
    <div className="grid h-full min-h-72 place-items-center p-8 text-center">
      <div>
        <p className="text-label">The answer lands here</p>
        <p className="mt-1 text-secondary text-muted">Press Send to see exactly what a caller gets, and how long it takes.</p>
      </div>
    </div>
  );
}

export function UseCode({ org, api, endpoint }: { org: string; api: SiteApiDetail; endpoint: SiteEndpoint }) {
  const [values, setValues] = useState(() => defaultsOf(endpoint.input));
  const [fresh, setFresh] = useState(false);
  const [openApi, setOpenApi] = useState(false);
  const attempt = useSiteTry(org, api.key);
  const url = callUrl(api.key, endpoint.key, values);
  const document = useMemo(() => JSON.stringify(openApiOf(api), null, 2), [api]);
  const missing = endpoint.input.find((field) => field.required && !values[field.key]);

  return (
    <div>
      <div className="grid md:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <form
          className="grid content-start gap-4 border-line p-4 max-md:border-b md:border-r"
          onSubmit={(event) => {
            event.preventDefault();
            if (!missing) attempt.mutate({ endpoint: endpoint.key, params: values, fresh });
          }}
        >
          <div>
            <p className="flex items-center gap-2">
              <span className="mono rounded-sm border border-line px-1 text-caption font-medium">GET</span>
              <span className="mono min-w-0 truncate text-secondary">/{api.key}/{endpoint.key}</span>
            </p>
            <p className="mt-2 text-secondary text-muted">
              {endpoint.description} {EFFECT_WORD[endpoint.effect].note}
            </p>
          </div>
          {endpoint.input.map((field) => (
            <InputField key={field.key} field={field} value={values[field.key] ?? ''} onChange={(value) => setValues((now) => ({ ...now, [field.key]: value }))} />
          ))}
          <div className="flex items-start gap-2 text-secondary">
            <Checkbox id="site-try-fresh" checked={fresh} onCheckedChange={(value) => setFresh(value === true)} className="mt-px" />
            <label htmlFor="site-try-fresh">
              <span className="block font-medium">Read the page fresh</span>
              <span className="block text-faint">Skips the saved answer. Sends max_age=0 and uses pace.</span>
            </label>
          </div>
          <div className="flex items-center justify-between gap-2 border-t border-line pt-4">
            <p className="text-caption text-faint">Tried as you, so no key is needed here.</p>
            <Button type="submit" variant="primary" loading={attempt.isPending} icon={<ArrowRight aria-hidden="true" className="size-4" />} disabled={Boolean(missing)}>
              Send
            </Button>
          </div>
        </form>
        <div aria-live="polite" className="min-w-0 bg-inset">
          {attempt.data ? <Answer result={attempt.data} endpoint={endpoint} /> : <Waiting />}
        </div>
      </div>

      <div className="border-t border-line">
        <Tabs defaultValue="curl">
          <div className="flex items-center justify-between gap-4 px-4">
            <TabList aria-label="The same call in code">
              {SNIPPETS.map(([key, label]) => (
                <Tab key={key} value={key}>
                  {label}
                </Tab>
              ))}
            </TabList>
            <Tooltip label="Generated from what the endpoint promises. It holds no site data, so it can be shared.">
              <Button size="sm" variant="ghost" icon={<FileJson aria-hidden="true" className="size-4" />} onClick={() => setOpenApi(true)}>
                OpenAPI document
              </Button>
            </Tooltip>
          </div>
          {SNIPPETS.map(([key]) => {
            const code = snippet(key, url);
            return (
              <TabPanel key={key} value={key} className="relative bg-inset">
                <div className="absolute top-2 right-2">
                  <CopyButton text={code} label={`Copy the ${key} snippet`} />
                </div>
                <pre className="mono max-h-72 overflow-auto p-4 pr-12 text-caption leading-5">{code}</pre>
              </TabPanel>
            );
          })}
        </Tabs>
      </div>

      <Drawer open={openApi} onOpenChange={setOpenApi} title="OpenAPI document" description="Import this into n8n or any client. It holds no site data, so it can be shared.">
        <div className="grid gap-2">
          <div className="flex justify-end">
            <CopyButton text={document} label="Copy the document" />
          </div>
          <pre className="mono overflow-auto rounded-lg border border-line bg-inset p-4 text-caption leading-5">{document}</pre>
        </div>
      </Drawer>
    </div>
  );
}
