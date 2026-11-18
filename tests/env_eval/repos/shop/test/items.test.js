const { test } = require("node:test");
const assert = require("node:assert/strict");

test("the server file exists", () => {
  assert.ok(require("node:fs").existsSync(require("node:path").join(__dirname, "..", "server.js")));
});
