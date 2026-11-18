// The shop's ledger: every amount is in whole cents.

export function balance(entries) {
  return entries.reduce((sum, e) => sum + (e.kind === "in" ? e.cents : -e.cents), 0);
}

export function format(cents) {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  return `${sign}€${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}
