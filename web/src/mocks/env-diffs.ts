/**
 * Checkpoint diffs for the mock, as the node serves them: one checkpoint against the work tree now, per
 * repository its files with git's status and line counts, and a unified patch. The cart discounts story
 * (c5kp) and the offer page (e7k2).
 */
import type { EnvCheckpointDiff } from '@/api/types';

type Status = 'A' | 'M' | 'D' | 'T';

interface MockFile {
  path: string;
  status: Status;
  body: string;
  /** Null for a binary file, as git counts it. */
  added?: number | null;
  removed?: number | null;
}

const f = (path: string, status: Status, body: string, added?: number | null, removed?: number | null): MockFile => ({ path, status, body, added, removed });

const count = (body: string, mark: '+' | '-') => body.split('\n').filter((line) => line.startsWith(mark)).length;

/** git's header for one file, then its hunks. */
function patchOf(file: MockFile): string {
  const a = file.status === 'A' ? '/dev/null' : `a/${file.path}`;
  const b = file.status === 'D' ? '/dev/null' : `b/${file.path}`;
  const mode = file.status === 'A' ? 'new file mode 100644\n' : file.status === 'D' ? 'deleted file mode 100644\n' : '';
  return `diff --git a/${file.path} b/${file.path}\n${mode}index 3c1e2a0..9b7f4d1 100644\n--- ${a}\n+++ ${b}\n${file.body}`;
}

function repoDiff(dir: string, files: MockFile[]): EnvCheckpointDiff['repos'][number] {
  const patch = files.map(patchOf).join('\n');
  return {
    dir: `/work/${dir}`,
    files_total: files.length,
    files: files.map((file) => ({ path: file.path, status: file.status, lines_added: file.added === undefined ? count(file.body, '+') : file.added, lines_removed: file.removed === undefined ? count(file.body, '-') : file.removed })),
    patch,
    patch_bytes: patch.length,
    truncated: false,
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

const SHIPPING_TEST = "@@ -18,3 +18,7 @@ describe('totals', () => {\n   });\n+\n+  it('still ships a cart a fixed discount emptied', () => {\n+    expect(totals(cart(12), { kind: 'fixed', amount: 20 }).shipping).toBe(4.9);\n+  });\n });";
const SHIPPING_ROW = "@@ -18,1 +18,1 @@\n-      <Row label=\"Shipping\" value={t.shipping} free={t.shipping === 0} />\n+      <Row label=\"Shipping\" value={t.shipping} free={t.shipping === 0} hint={t.shipping ? 'Free from 100' : undefined} />";
const REFUND = '@@ -1,4 +0,0 @@\n-export function refundLine(order: Order, lineId: string) {\n-  const line = order.lines.find((entry) => entry.id === lineId);\n-  return line ? line.price * line.qty - line.discount : 0;\n-}';
const LOGO = 'Binary files a/public/offer-hero.webp and b/public/offer-hero.webp differ';

/** Each checkpoint of a story, from it to the work tree now, by repository folder. */
const DIFFS: Record<string, Record<number, Record<string, MockFile[]>>> = {
  c5kp: {
    1: {
      shop: [f('src/cart/totals.ts', 'M', TOTALS_PERCENT), f('src/cart/totals.test.ts', 'A', TOTALS_TEST), f('src/cart/summary.tsx', 'M', SUMMARY), f('src/cart/code-field.tsx', 'A', "@@ -0,0 +1,3 @@\n+export function CodeField({ onApply }: { onApply: (code: string) => void }) {\n+  return <form onSubmit={(event) => { event.preventDefault(); onApply(new FormData(event.currentTarget).get('code') as string); }}><input name=\"code\" /></form>;\n+}")],
      'ui-kit': [f('src/price-badge.tsx', 'M', BADGE)],
    },
    2: { shop: [f('src/cart/totals.ts', 'M', NOW_AFTER_3), f('src/cart/totals.test.ts', 'A', TOTALS_TEST), f('src/cart/summary.tsx', 'M', SUMMARY)] },
    3: { shop: [f('src/cart/totals.ts', 'M', NOW_AFTER_3), f('src/cart/totals.test.ts', 'M', SHIPPING_TEST), f('src/cart/summary.tsx', 'M', SHIPPING_ROW)], 'ui-kit': [] },
    4: { shop: [f('src/cart/totals.ts', 'M', LINE_ITEMS.replace(/^\+/gm, '-'), 3, 214), f('src/cart/refund.ts', 'D', REFUND, 0, 96)], 'ui-kit': [f('src/price-badge.tsx', 'M', BADGE, 9, 31)] },
    5: { shop: [f('src/cart/totals.ts', 'M', LINE_ITEMS.replace(/^\+/gm, '-'), 3, 214), f('src/cart/refund.ts', 'D', REFUND, 0, 96)], 'ui-kit': [f('src/price-badge.tsx', 'M', BADGE, 9, 31)] },
    6: { shop: [f('src/cart/totals.ts', 'M', NOW_AFTER_3.split('\n').slice(0, 5).join('\n'), 2, 1), f('src/cart/totals.test.ts', 'M', SHIPPING_TEST)], 'ui-kit': [] },
  },
  e7k2: {
    1: { shop: [f('src/pricing/plan-card.tsx', 'M', '@@ -9,2 +9,4 @@\n-      <Price value={plan.price} />\n+      <s className="was">{money(plan.price)}</s>\n+      <Price value={plan.price * 0.7} />', 26, 15), f('public/offer-hero.webp', 'M', LOGO, null, null)] },
    2: { shop: [] },
  },
};

export function checkpointDiff(env: string, checkpoint: number): EnvCheckpointDiff | null {
  const found = DIFFS[env]?.[checkpoint];
  if (!found) return null;
  return { checkpoint, max_bytes: 200_000, max_files: 500, repos: Object.entries(found).map(([dir, files]) => repoDiff(dir, files)) };
}
