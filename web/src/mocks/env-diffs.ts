/**
 * Checkpoint diffs for the mock (concept): what a checkpoint changed against the one before it, and what
 * changed from it to the working copy now. The cart discounts story (c5kp) and the offer page (e7k2).
 */
import type { EnvCheckpointDiff, EnvDiffFile, EnvDiffLine } from '@/api/types';

/** A unified diff body as lines: "+" added, "-" removed, "@@" a hunk head, anything else context. */
function lines(body: string): EnvDiffLine[] {
  return body.split('\n').map((line) => {
    if (line.startsWith('@@')) return { kind: 'hunk', text: line };
    if (line.startsWith('+')) return { kind: 'add', text: line.slice(1) };
    if (line.startsWith('-')) return { kind: 'del', text: line.slice(1) };
    return { kind: 'ctx', text: line.startsWith(' ') ? line.slice(1) : line };
  });
}

function file(repo: string, path: string, status: EnvDiffFile['status'], body: string, added?: number, removed?: number): EnvDiffFile {
  const parsed = lines(body);
  return {
    repo,
    path,
    status,
    added: added ?? parsed.filter((line) => line.kind === 'add').length,
    removed: removed ?? parsed.filter((line) => line.kind === 'del').length,
    lines: parsed,
  };
}

const TOTALS_PERCENT = `@@ -1,18 +1,41 @@
 import type { Cart, Discount } from './types';
+import { roundCents } from '../money';

-export function totals(cart: Cart) {
+/** A percent discount takes its share of the subtotal; a fixed one its amount, never past zero. */
+export function discountOf(subtotal: number, discount: Discount | null): number {
+  if (!discount) return 0;
+  if (discount.kind === 'percent') return roundCents((subtotal * discount.percent) / 100);
+  return Math.min(subtotal, discount.amount);
+}
+
+export function totals(cart: Cart, discount: Discount | null = cart.discount) {
   const subtotal = cart.lines.reduce((sum, line) => sum + line.price * line.qty, 0);
-  const discount = cart.code ? subtotal * 0.1 : 0;
-  const shipping = subtotal > 100 ? 0 : 4.9;
-  return { subtotal, discount, shipping, total: subtotal - discount + shipping };
+  const off = discountOf(subtotal, discount);
+  const shipping = subtotal - off >= 100 ? 0 : 4.9;
+  return { subtotal, discount: off, shipping, total: roundCents(subtotal - off + shipping) };
 }`;

const TOTALS_TEST = `@@ -0,0 +1,24 @@
+import { describe, expect, it } from 'vitest';
+import { totals } from './totals';
+
+const cart = (price: number, discount = null) => ({ lines: [{ price, qty: 1 }], discount });
+
+describe('totals', () => {
+  it('takes 10% off the subtotal', () => {
+    expect(totals(cart(80), { kind: 'percent', percent: 10 }).total).toBe(76.9);
+  });
+
+  it('never takes a fixed discount past zero', () => {
+    expect(totals(cart(12), { kind: 'fixed', amount: 20 }).discount).toBe(12);
+  });
+
+  it('ships free from 100 after the discount, not before', () => {
+    expect(totals(cart(105), { kind: 'percent', percent: 10 }).shipping).toBe(4.9);
+  });
+});`;

const SUMMARY = `@@ -14,9 +14,14 @@ export function CartSummary({ cart }: { cart: Cart }) {
   const t = totals(cart);
   return (
     <dl className="summary">
       <Row label="Subtotal" value={t.subtotal} />
-      {cart.code ? <Row label="Discount" value={-t.discount} /> : null}
+      {t.discount ? <Row label={discountLabel(cart.discount)} value={-t.discount} /> : null}
       <Row label="Shipping" value={t.shipping} free={t.shipping === 0} />
       <Row label="Total" value={t.total} strong />
     </dl>`;

const BADGE = `@@ -3,6 +3,9 @@ export function PriceBadge({ children, tone = 'neutral' }: Props) {
-  return <span className={styles.badge}>{children}</span>;
+  return (
+    <span className={styles.badge} data-tone={tone}>
+      {children}
+    </span>
+  );
 }`;

const LINE_ITEMS = `@@ -1,12 +1,58 @@
-export function totals(cart: Cart, discount: Discount | null = cart.discount) {
+/** Try: each line carries its own share of the discount, so a refund of one line gives back its part. */
+export function lineShares(cart: Cart, discount: Discount | null) {
+  const subtotal = cart.lines.reduce((sum, line) => sum + line.price * line.qty, 0);
+  return cart.lines.map((line) => ({
+    ...line,
+    discount: roundCents((discountOf(subtotal, discount) * line.price * line.qty) / subtotal),
+  }));
+}`;

const NOW_AFTER_3 = `@@ -22,7 +22,9 @@ export function totals(cart: Cart, discount: Discount | null = cart.discount) {
   const subtotal = cart.lines.reduce((sum, line) => sum + line.price * line.qty, 0);
   const off = discountOf(subtotal, discount);
-  const shipping = subtotal - off >= 100 ? 0 : 4.9;
+  // A fixed discount that empties the cart still ships: free shipping is for what was paid.
+  const paid = Math.max(0, subtotal - off);
+  const shipping = paid >= 100 ? 0 : 4.9;
   return { subtotal, discount: off, shipping, total: roundCents(subtotal - off + shipping) };`;

const DIFFS: Record<string, Record<'previous' | 'now', EnvDiffFile[]>> = {
  ck1: { previous: [], now: [file('shop', 'src/cart/totals.ts', 'modified', TOTALS_PERCENT), file('shop', 'src/cart/totals.test.ts', 'added', TOTALS_TEST), file('shop', 'src/cart/summary.tsx', 'modified', SUMMARY), file('ui-kit', 'src/price-badge.tsx', 'modified', BADGE)] },
  ck2: {
    previous: [file('shop', 'src/cart/totals.ts', 'modified', TOTALS_PERCENT.split('\n').slice(0, 12).join('\n'), 52, 9), file('shop', 'src/cart/summary.tsx', 'modified', SUMMARY), file('shop', 'src/cart/code-field.tsx', 'added', "@@ -0,0 +1,3 @@\n+export function CodeField({ onApply }: { onApply: (code: string) => void }) {\n+  return <form onSubmit={(event) => { event.preventDefault(); onApply(new FormData(event.currentTarget).get('code') as string); }}><input name=\"code\" /></form>;\n+}", 27, 0)],
    now: [file('shop', 'src/cart/totals.ts', 'modified', NOW_AFTER_3), file('shop', 'src/cart/totals.test.ts', 'added', TOTALS_TEST), file('ui-kit', 'src/price-badge.tsx', 'modified', BADGE)],
  },
  ck3: {
    previous: [file('shop', 'src/cart/totals.ts', 'modified', TOTALS_PERCENT), file('shop', 'src/cart/totals.test.ts', 'added', TOTALS_TEST), file('ui-kit', 'src/price-badge.tsx', 'modified', BADGE)],
    now: [file('shop', 'src/cart/totals.ts', 'modified', NOW_AFTER_3), file('shop', 'src/cart/totals.test.ts', 'modified', "@@ -18,3 +18,7 @@ describe('totals', () => {\n   });\n+\n+  it('still ships a cart a fixed discount emptied', () => {\n+    expect(totals(cart(12), { kind: 'fixed', amount: 20 }).shipping).toBe(4.9);\n+  });\n });"), file('shop', 'src/cart/summary.tsx', 'modified', "@@ -18,1 +18,1 @@\n-      <Row label=\"Shipping\" value={t.shipping} free={t.shipping === 0} />\n+      <Row label=\"Shipping\" value={t.shipping} free={t.shipping === 0} hint={t.shipping ? 'Free from €100' : undefined} />")],
  },
  ck4: {
    previous: [file('shop', 'src/cart/totals.ts', 'modified', LINE_ITEMS, 214, 41), file('shop', 'src/cart/refund.ts', 'added', '@@ -0,0 +1,4 @@\n+export function refundLine(order: Order, lineId: string) {\n+  const line = order.lines.find((entry) => entry.id === lineId);\n+  return line ? line.price * line.qty - line.discount : 0;\n+}', 96, 0), file('ui-kit', 'src/price-badge.tsx', 'modified', BADGE, 31, 9)],
    now: [file('shop', 'src/cart/totals.ts', 'modified', LINE_ITEMS.replace(/^\+/gm, '-'), 3, 214), file('shop', 'src/cart/refund.ts', 'deleted', '@@ -1,4 +0,0 @@\n-export function refundLine(order: Order, lineId: string) {\n-  const line = order.lines.find((entry) => entry.id === lineId);\n-  return line ? line.price * line.qty - line.discount : 0;\n-}', 0, 96)],
  },
  ck5: {
    previous: [file('shop', 'src/cart/totals.test.ts', 'modified', "@@ -30,2 +30,6 @@\n+  it('splits a fixed discount across lines', () => {\n+    expect(lineShares(cart(10, 30), fixed(4)).map((line) => line.discount)).toEqual([1, 3]);\n+  });")],
    now: [file('shop', 'src/cart/totals.ts', 'modified', LINE_ITEMS.replace(/^\+/gm, '-'), 3, 214), file('shop', 'src/cart/refund.ts', 'deleted', '@@ -1,4 +0,0 @@\n-export function refundLine(order: Order, lineId: string) {', 0, 96)],
  },
  ck_e7k2_1: { previous: [file('shop', 'src/offer/banner.tsx', 'added', '@@ -0,0 +1,3 @@\n+export function OfferBanner() {\n+  return <section className="offer">Black Friday: 30% off everything</section>;\n+}', 64, 4)], now: [file('shop', 'src/pricing/plan-card.tsx', 'modified', '@@ -9,2 +9,4 @@\n-      <Price value={plan.price} />\n+      <s className="was">{money(plan.price)}</s>\n+      <Price value={plan.price * 0.7} />', 26, 15)] },
  ck_e7k2_2: { previous: [file('shop', 'src/pricing/plan-card.tsx', 'modified', '@@ -9,2 +9,4 @@\n-      <Price value={plan.price} />\n+      <s className="was">{money(plan.price)}</s>\n+      <Price value={plan.price * 0.7} />', 26, 15)], now: [] },
};

export function checkpointDiff(_env: string, checkpoint: string, against: 'previous' | 'now'): EnvCheckpointDiff | null {
  const found = DIFFS[checkpoint];
  return found ? { checkpoint, against, files: found[against] } : null;
}
