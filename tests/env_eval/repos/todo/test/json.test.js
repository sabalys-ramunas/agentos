import { test } from "node:test";
import assert from "node:assert/strict";
import { toJson } from "../src/export.js";

test("JSON counts the tasks and the done ones", () => {
  const out = JSON.parse(toJson([{ title: "Buy milk", done: false }, { title: "Call Ana", done: true }]));
  assert.deepEqual(out, { count: 2, done: 1, tasks: [{ title: "Buy milk", done: false }, { title: "Call Ana", done: true }] });
});

test("JSON is indented by two spaces", () => {
  assert.ok(toJson([]).includes('\n  "count": 0'));
});
