/**
 * The time zone the unit tests run in: one where it is now between 15:00 and 16:00. The mock data is
 * dated relative to now (a run 3 hours ago), and History, Needs you and Machines group it by local day,
 * so a run just after midnight put half of "Today" under "Yesterday" and failed. The clock stays real;
 * only the zone moves. Etc/GMT names have the sign reversed: Etc/GMT-3 is UTC+3.
 */
export function afternoonZone(now = new Date()): string {
  const offset = ((15 - now.getUTCHours() + 36) % 24) - 12; // -12..11 hours from UTC
  return offset === 0 ? 'UTC' : `Etc/GMT${offset > 0 ? '-' : '+'}${Math.abs(offset)}`;
}
