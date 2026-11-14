/** Just enough cron for the mock backend to answer with real upcoming times. */

function field(text: string, min: number, max: number): Set<number> {
  const values = new Set<number>();
  for (const part of text.split(',')) {
    const [range = '', step = '1'] = part.split('/');
    const [from, to] = range === '*' ? [min, max] : range.includes('-') ? range.split('-').map(Number) : [Number(range), Number(range)];
    for (let value = from ?? min; value <= (to ?? max); value += Number(step)) values.add(value);
  }
  return values;
}

/** The next fires of a five-field cron line, in the browser's clock. Plenty for a demo. */
export function nextFires(line: string, from: number, count: number, horizonDays = 14): number[] {
  const parts = line.trim().split(/\s+/);
  if (parts.length !== 5) return [];
  const [minutes, hours, , , days] = [
    field(parts[0] ?? '*', 0, 59),
    field(parts[1] ?? '*', 0, 23),
    null,
    null,
    field(parts[4] ?? '*', 0, 7),
  ];
  const fires: number[] = [];
  const cursor = new Date(from);
  cursor.setSeconds(0, 0);
  cursor.setMinutes(cursor.getMinutes() + 1);
  for (let step = 0; step < horizonDays * 1440 && fires.length < count; step += 1) {
    const day = cursor.getDay();
    if (minutes.has(cursor.getMinutes()) && hours.has(cursor.getHours()) && (days.has(day) || (day === 0 && days.has(7)))) {
      fires.push(cursor.getTime());
    }
    cursor.setMinutes(cursor.getMinutes() + 1);
  }
  return fires;
}

/** Past fires, newest first. */
export function lastFires(line: string, before: number, count: number): number[] {
  const fires = nextFires(line, before - 14 * 86_400_000, 4_000, 14).filter((at) => at <= before);
  return fires.slice(-count).reverse();
}
