import { test } from "node:test";
import assert from "node:assert/strict";
import { balance, format } from "../src/ledger.js";

const entries = [{ kind: "in", cents: 1999 }, { kind: "out", cents: 450 }];

test("the balance adds what came in and takes what went out", () => {
  assert.equal(balance(entries), 1549);
});

test("an amount is shown in euros with two decimals", () => {
  assert.equal(format(1549), "€15.49");
  assert.equal(format(-5), "-€0.05");
});
