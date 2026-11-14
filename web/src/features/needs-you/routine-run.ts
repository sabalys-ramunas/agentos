import { routineName } from '@/lib/principal';

/** A routine's run to approve: its name, and one "Field: value" line per input it runs with. */
export interface RoutineRun {
  name: string;
  inputs: string[];
}

/** "post_date" reads "Post date". */
const fieldLabel = (field: string): string => {
  const words = field.replace(/[_-]+/g, ' ').trim();
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}`;
};

/** The node holds back a value it reads as a secret (a PIN, say) and writes it as this bare word, which is not JSON. */
const HELD_BACK = /:\s*\[redacted\]/g;

/** An input's value in words. Text as it is, Yes or No, a list with commas. A nested object has no words to show. */
function valueWords(value: unknown): string | null {
  if (value === '[redacted]') return 'Not shown';
  if (typeof value === 'string') return value.trim() ? value : 'Not set';
  if (typeof value === 'number') return String(value);
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (value === null || value === undefined) return 'Not set';
  if (Array.isArray(value)) return value.flatMap((item) => valueWords(item) ?? []).join(', ') || 'Not set';
  return null;
}

/**
 * The node sends a routine's run for approval as JSON text: `{"workflow", "name", "values"}`. Read it here, so the card
 * says the routine's name and its inputs in words, never the JSON. Null for any other preview, which keeps its own card.
 */
export function routineRunOf(text: string | null | undefined): RoutineRun | null {
  if (!text) return null;
  let data: unknown;
  try {
    // A held-back value is put in quotes first, so the rest still reads as JSON.
    data = JSON.parse(text.replace(HELD_BACK, ': "[redacted]"'));
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const { workflow, name, values } = data as { workflow?: unknown; name?: unknown; values?: unknown };
  if (typeof workflow !== 'string' || !workflow) return null;
  const title = typeof name === 'string' && name.trim() ? name.trim() : routineName(workflow);
  const inputs = values && typeof values === 'object' && !Array.isArray(values)
    ? Object.entries(values).flatMap(([field, value]) => {
        const words = valueWords(value);
        return words === null ? [] : [`${fieldLabel(field)}: ${words}`];
      })
    : [];
  return { name: title, inputs };
}
