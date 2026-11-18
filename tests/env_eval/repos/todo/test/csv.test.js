import { test } from "node:test";
import assert from "node:assert/strict";
import { toCsv } from "../src/export.js";

const tasks = [{ title: "Buy milk", done: false }, { title: "Call Ana, today", done: true }];

test("CSV has a header and a row a task", () => {
  assert.equal(toCsv(tasks), 'title,done\nBuy milk,no\n"Call Ana, today",yes\n');
});

test("a quote in a title is doubled", () => {
  assert.equal(toCsv([{ title: 'Say "hi"', done: false }]), 'title,done\n"Say ""hi""",no\n');
});
