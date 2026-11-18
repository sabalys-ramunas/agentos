// The price a customer pays, in cents.

export function lineTotal(unitCents, quantity) {
  return unitCents * quantity;
}

// A discount in percent, taken off the line total and rounded to whole cents.
export function discounted(totalCents, percent) {
  return Math.round(totalCents * percent / 100);
}

export function cartTotal(lines, percent = 0) {
  const total = lines.reduce((sum, l) => sum + lineTotal(l.unitCents, l.quantity), 0);
  return discounted(total, percent);
}
