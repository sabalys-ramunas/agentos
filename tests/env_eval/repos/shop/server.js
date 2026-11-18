// The shop's dev server: the home page on port 3000 (PORT to change it).
const http = require("node:http");

const items = [
  { name: "Wool scarf", euros: 24 },
  { name: "Rain jacket", euros: 89 },
  { name: "Thermos", euros: 19 },
];

function page() {
  const rows = items.map((i) => `<li>${i.name} <b>€${i.euros}</b></li>`).join("");
  return `<!doctype html><html><head><meta charset="utf-8"><title>Autumn sale · Shop</title>
<style>body{font:18px system-ui;margin:40px}h1{color:#a33}</style></head>
<body><h1>Autumn sale</h1><p>Twenty percent off until Sunday.</p><ul>${rows}</ul></body></html>`;
}

const port = Number(process.env.PORT || 3000);
http.createServer((req, res) => {
  res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
  res.end(page());
}).listen(port, "0.0.0.0", () => console.log(`shop on http://localhost:${port}`));
