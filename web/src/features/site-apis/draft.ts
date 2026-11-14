/**
 * A Site API being taught, as plain data: which columns of the page are kept and what they are
 * called. Everything Teach it shows is worked out from this, the sheet, the sample values and the
 * JSON definition, so the three can never disagree. A draft holds fields and steps and nothing else:
 * no cookie, no session and no page text beyond what a person chose to keep.
 */
import type { SiteDiscovery, SiteField, SitePageColumn, SiteRow } from '@/api/types';
import { slugify } from '@/lib/format';

export interface Draft {
  /** Kept columns by their label on the page, in page order. */
  kept: string[];
  /** What a person called a column, where they changed it. */
  names: Record<string, string>;
  title: string;
  cache_s: number;
  expect_min: number;
}

export const keyOf = (label: string) => slugify(label).replaceAll('-', '_');

export function firstDraft(found: SiteDiscovery): Draft {
  return {
    kept: found.page.columns.filter((column) => column.field).map((column) => column.label),
    names: {},
    title: 'Open orders',
    cache_s: 300,
    expect_min: 1,
  };
}

function valuesOf(column: SitePageColumn, rows: SiteRow[]): string[] {
  return column.field ? rows.map((row) => row[column.field as string] ?? '') : (column.values ?? []);
}

/** The page's columns, with a field on each one that is kept. */
export function draftColumns(found: SiteDiscovery, draft: Draft): SitePageColumn[] {
  return found.page.columns.map((column) => ({
    label: column.label,
    field: draft.kept.includes(column.label) ? keyOf(column.label) : null,
    values: valuesOf(column, found.rows),
  }));
}

export function draftFields(found: SiteDiscovery, draft: Draft): SiteField[] {
  return found.page.columns
    .filter((column) => draft.kept.includes(column.label))
    .map((column) => ({ key: keyOf(column.label), label: draft.names[column.label]?.trim() || column.label, kind: 'text' }));
}

export function draftRows(found: SiteDiscovery, draft: Draft): SiteRow[] {
  const columns = found.page.columns.filter((column) => draft.kept.includes(column.label));
  return found.rows.map((_, index) => Object.fromEntries(columns.map((column) => [keyOf(column.label), valuesOf(column, found.rows)[index] ?? ''])));
}

/** What the definition file would hold. Steps only ever go to the site's own domain. */
export function definitionOf(found: SiteDiscovery, draft: Draft) {
  const fields = draftFields(found, draft);
  const name = keyOf(draft.title) || 'rows';
  return {
    api: slugify(found.site.name),
    title: found.site.name,
    domain: found.site.domain,
    version: '1.0.0',
    surfaces: [found.lane],
    endpoints: {
      [name]: {
        title: draft.title,
        effect: 'read',
        input: { type: 'object', properties: {} },
        output: {
          type: 'object',
          required: [name],
          properties: { [name]: { type: 'array', items: { type: 'object', properties: Object.fromEntries(fields.map((field) => [field.key, { type: 'string' }])) } } },
        },
        steps: [
          { type: 'navigate', url: found.page.url },
          {
            type: 'extract',
            save_as: name,
            max_items: 100,
            items: { role: 'row', within: { role: 'table', name: found.page.heading } },
            fields: Object.fromEntries(
              found.page.columns.flatMap((column, index) =>
                draft.kept.includes(column.label) ? [[keyOf(column.label), { locator: { role: 'cell', nth: index }, read: 'text', max_chars: 80 }]] : [],
              ),
            ),
          },
        ],
        outputs: { [name]: name },
        cache_s: draft.cache_s,
        expect: { min_items: { [name]: draft.expect_min } },
        pace: { per_hour: 60 },
      },
    },
  };
}
