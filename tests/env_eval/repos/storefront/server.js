// The storefront: its price list on port 3000, from .cache/prices.json (scripts/setup.sh builds it).
const fs = require("node:fs");
const http = require("node:http");

const items = JSON.parse(fs.readFileSync(".cache/prices.json", "utf8"));
const rows = items.map((i) => `<li>${i.name}: <b>€${i.euros}</b></li>`).join("");
const html = `<!doctype html><html><head><meta charset="utf-8"><title>Storefront</title></head>
<body><h1>Today's prices</h1><ul>${rows}</ul></body></html>`;

http.createServer((req, res) => {
  res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
  res.end(html);
}).listen(3000, "0.0.0.0", () => console.log("storefront on http://localhost:3000"));
