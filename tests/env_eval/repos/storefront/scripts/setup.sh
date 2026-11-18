#!/bin/sh
# The storefront's install: builds .cache/prices.json from data/prices.csv. The sleep stands in for
# the minutes a real install takes, so a prepared machine has something to save.
set -e
sleep 12
mkdir -p .cache
node -e '
const fs = require("fs");
const rows = fs.readFileSync("data/prices.csv", "utf8").trim().split("\n").slice(1);
const items = rows.map((r) => { const [name, euros] = r.split(","); return { name, euros }; });
fs.writeFileSync(".cache/prices.json", JSON.stringify(items));
'
echo "setup done: $(wc -c < .cache/prices.json) bytes of prices"
