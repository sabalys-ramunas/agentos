/**
 * The words of Site APIs, in one place: each state a person can meet, with what happened, what was
 * and was not passed on, and the one next step. Also the pure pieces both doors share: the address
 * of a call, the snippets, the CSV and the OpenAPI document. Nothing here says lane, grant, surface
 * or scrape, and nothing here offers a way past a site's limit: an API runs on its one account.
 */
import type { SiteApi, SiteApiDetail, SiteApiEffect, SiteApiHealth, SiteEndpoint, SiteField, SiteInputField, SiteRow } from '@/api/types';
import type { PersonState } from '@/lib/states';
import { clock, plural, timeAgo } from '@/lib/format';

export const STATE_OF: Record<SiteApiHealth, PersonState> = {
  good: 'good',
  changed: 'stuck',
  sign_in: 'waiting',
  site_check: 'waiting',
  empty: 'waiting',
  resting: 'sleeping',
  paused: 'sleeping',
  unread: 'waiting',
};

/** A word that fits a card. */
export const HEALTH_WORD: Record<SiteApiHealth, string> = {
  good: 'Answering',
  changed: 'Site changed',
  sign_in: 'Needs you to sign in',
  site_check: 'Needs a person',
  empty: 'Came back short',
  resting: 'Resting',
  paused: 'Paused',
  unread: 'Not read yet',
};

export interface ProblemWords {
  /** What happened, as a title. */
  title: string;
  /** What that means for whoever reads this API. */
  body: string;
  /** What callers get meanwhile. */
  callers: string;
  /** The code a developer sees, for the technical line. */
  code: string;
}

export function problemWords(api: Pick<SiteApi, 'health' | 'problem' | 'site' | 'account'>, field?: SiteField): ProblemWords | null {
  const { problem, site } = api;
  switch (api.health) {
    case 'good':
      return null;
    case 'changed':
      return {
        title: field ? `${site.name} changed its page, so "${field.label}" no longer reads` : `${site.name} changed its page`,
        body: `${field ? `"${field.label}"` : 'It'} no longer matches what this expects${problem?.now_label ? `, and the page now says "${problem.now_label}" there` : ''}. It looked wrong, so nothing was passed on.`,
        callers: 'Anything that calls this gets an error instead of a wrong value.',
        code: '502 output_contract_violation',
      };
    case 'sign_in':
      return {
        title: `${api.account} was signed out of ${site.name}`,
        body: 'Sign in again in the account\'s own window. Nothing will try to sign in for you.',
        callers: 'Anything that calls this is told a person is needed, and should wait.',
        code: '503 needs_human',
      };
    case 'site_check':
      return {
        title: `${site.name} asked a question only you can answer`,
        body: 'The site showed a check for the account holder. Everything on this account stopped so that nothing makes it worse.',
        callers: 'Anything that calls this is told a person is needed, and should wait.',
        code: '503 needs_human',
      };
    case 'empty':
      return {
        title: problem?.found === undefined ? 'It came back shorter than it expects' : `It found ${problem.found} where it expected at least ${problem.expected ?? 1}`,
        body: 'A list this short usually means the page changed, so it said so rather than hand over an empty list.',
        callers: 'Anything that calls this gets an error, not an empty list that looks true.',
        code: '422 empty_result',
      };
    case 'resting':
      return {
        title: problem?.resumes_at ? `Resting until ${clock(problem.resumes_at)}` : 'Resting',
        body: `${site.name} asked this account to slow down, so it is waiting. It starts again by itself.`,
        callers: 'Anything that calls this is told when to come back. Answers already saved are still served.',
        code: '503 rate_limited',
      };
    case 'paused':
      return {
        title: problem?.by ? `Paused by ${problem.by} ${timeAgo(problem.since).toLowerCase()}` : 'Paused',
        body: 'It is not reading the site, and it stays that way until a person resumes it.',
        callers: 'Anything that calls this is told it is paused.',
        code: '409 paused',
      };
    case 'unread':
      return {
        title: 'Not read on this computer yet',
        body: 'No run has finished for it here, so nothing is known yet about whether the site answers.',
        callers: 'A call runs the read itself, and is answered when the site answers.',
        code: 'no run yet',
      };
  }
}

export const EFFECT_WORD: Record<SiteApiEffect, { word: string; note: string }> = {
  read: { word: 'Reads', note: 'Only looks at the page.' },
  draft: { word: 'Fills in', note: 'Types into a box like a search, and never sends anything.' },
  commit: { word: 'Acts', note: 'Changes something on the site, and follows Ask me first.' },
};

export function freshWords(seconds: number): string {
  if (seconds === 0) return 'Always reads the page';
  if (seconds < 3600) return `Saved for ${Math.round(seconds / 60)} min`;
  return `Saved for ${plural(Math.round(seconds / 3600), 'hour')}`;
}

/** The site's own letter on the panel's plain mark. An invented site never gets a logo of its own. */
export const markOf = (site: { name: string }) => `${site.name.replace(/[^a-z0-9]/gi, '').toLowerCase()}.example`;

export const speed = (ms: number) => (ms >= 1000 ? `${(ms / 1000).toFixed(2).replace(/0$/, '')} s` : `${ms} ms`);

/** Where a developer calls this, whichever computer it runs on. */
export const BASE = 'https://relay.agentos.example/v1/sites';

export function callUrl(api: string, endpoint: string, params: Record<string, string>): string {
  const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== '')).toString();
  return `${BASE}/${api}/${endpoint}${query ? `?${query}` : ''}`;
}

export function defaultsOf(input: SiteInputField[]): Record<string, string> {
  return Object.fromEntries(input.map((field) => [field.key, field.default ?? '']));
}

export type SnippetKind = 'curl' | 'javascript' | 'python' | 'n8n';

/** A key is shown once and never again, so a snippet reads it from the environment. */
export function snippet(kind: SnippetKind, url: string): string {
  switch (kind) {
    case 'curl':
      return `curl "${url}" \\\n  -H "Authorization: Bearer $AGENTOS_KEY"`;
    case 'javascript':
      return `const response = await fetch("${url}", {\n  headers: { Authorization: \`Bearer \${process.env.AGENTOS_KEY}\` },\n});\n\nif (response.status === 503) {\n  // A person is needed, or the site asked for a rest. Do not retry in a loop.\n  throw new Error((await response.json()).detail);\n}\n\nconst { data, cache, source } = await response.json();`;
    case 'python':
      return `import os\nimport requests\n\nresponse = requests.get(\n    "${url}",\n    headers={"Authorization": f"Bearer {os.environ['AGENTOS_KEY']}"},\n    timeout=70,\n)\nresponse.raise_for_status()\nanswer = response.json()\nprint(answer["data"], answer["cache"])`;
    case 'n8n':
      return JSON.stringify(
        {
          nodes: [
            {
              name: 'Read the Site API',
              type: 'n8n-nodes-base.httpRequest',
              typeVersion: 4,
              parameters: {
                url,
                authentication: 'genericCredentialType',
                genericAuthType: 'httpBearerAuth',
                options: { timeout: 70000 },
              },
            },
          ],
        },
        null,
        2,
      );
  }
}

function typeOf(field: SiteField) {
  return field.kind === 'link' ? { type: 'string', format: 'uri' } : { type: 'string' };
}

/** Generated from what the endpoint promises, and it holds no site data, so it can be shared. */
export function openApiOf(api: Pick<SiteApiDetail, 'key' | 'title' | 'version' | 'endpoints'>) {
  return {
    openapi: '3.1.0',
    info: { title: api.title, version: api.version },
    servers: [{ url: BASE }],
    security: [{ key: [] }],
    components: { securitySchemes: { key: { type: 'http', scheme: 'bearer' } } },
    paths: Object.fromEntries(
      api.endpoints.map((endpoint) => [
        `/${api.key}/${endpoint.key}`,
        {
          get: {
            operationId: `${api.key.replaceAll('-', '_')}_${endpoint.key}`,
            summary: endpoint.title,
            description: endpoint.description,
            'x-agentos-effect': endpoint.effect,
            'x-agentos-cache-s': endpoint.cache_s,
            parameters: endpoint.input.map((input) => ({
              name: input.key,
              in: 'query',
              required: Boolean(input.required),
              schema: {
                type: input.type === 'number' ? 'integer' : 'string',
                ...(input.options ? { enum: input.options.map((option) => option.value) } : {}),
                ...(input.default ? { default: input.default } : {}),
              },
            })),
            responses: {
              200: {
                description: 'The fields this endpoint promises',
                content: {
                  'application/json': {
                    schema: {
                      type: 'object',
                      properties: { data: { type: 'array', items: { type: 'object', properties: Object.fromEntries(endpoint.fields.map((field) => [field.key, typeOf(field)])) } } },
                    },
                  },
                },
              },
            },
          },
        },
      ]),
    ),
  };
}

const quoteCsv = (value: string) => (/[",\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value);
/** A spreadsheet runs a cell that starts like a formula, and these values came from a web page. */
const safeCell = (value: string) => (/^[=+\-@\t\r]/.test(value) ? `'${value}` : value);

export function toCsv(fields: SiteField[], rows: SiteRow[]): string {
  const lines = [fields.map((field) => quoteCsv(field.label)), ...rows.map((row) => fields.map((field) => quoteCsv(safeCell(row[field.key] ?? ''))))];
  return lines.map((line) => line.join(',')).join('\n');
}

export function toTsv(fields: SiteField[], rows: SiteRow[]): string {
  return [fields.map((field) => field.label).join('\t'), ...rows.map((row) => fields.map((field) => (row[field.key] ?? '').replaceAll('\t', ' ')).join('\t'))].join('\n');
}

export function endpointOf(api: SiteApiDetail, key: string | undefined): SiteEndpoint {
  return api.endpoints.find((item) => item.key === key) ?? (api.endpoints[0] as SiteEndpoint);
}
