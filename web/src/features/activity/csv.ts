import type { TraceEvent } from '@/api/types';
import { principalId } from '@/lib/principal';

/** A spreadsheet runs a cell that starts with one of these, and summaries come from nodes. */
const FORMULA_START = /^[=+\-@\t\r]/;

/**
 * One CSV cell, quoted, with anything a spreadsheet would run as a formula made plain text by a
 * leading apostrophe (security review 11). The server's export does the same.
 */
export function csvCell(value: unknown): string {
  const text = value === null || value === undefined ? '' : String(value);
  const plain = FORMULA_START.test(text) ? `'${text}` : text;
  return `"${plain.replaceAll('"', '""')}"`;
}

export function activityCsv(rows: TraceEvent[]): string {
  const cells = (row: TraceEvent) => [row.at, row.status, row.lane, principalId(row.principal), row.effect, row.cost.usd, row.summary].map(csvCell).join(',');
  return ['at,status,lane,started_by,effect,usd,summary', ...rows.map(cells)].join('\n');
}
