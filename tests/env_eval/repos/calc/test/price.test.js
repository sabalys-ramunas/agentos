import { test } from "node:test";
import assert from "node:assert/strict";
import { cartTotal, discounted, lineTotal } from "../src/price.js";

test("a line is the unit price times the quantity", () => {
  assert.equal(lineTotal(250, 4), 1000);
});

test("a discount takes the percent off", () => {
  assert.equal(discounted(1000, 20), 800);
});

test("no discount is the whole total", () => {
  assert.equal(cartTotal([{ unitCents: 999, quantity: 2 }]), 1998);
});

test("a discount rounds to whole cents", () => {
  assert.equal(discounted(999, 15), 849);
});
