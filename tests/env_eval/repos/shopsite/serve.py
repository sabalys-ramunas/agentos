#!/usr/bin/env python3
"""A small shop, served on the machine itself: the page kinds an agent meets on the real web, with
nothing from the real web in it.

    python3 serve.py --seed 3        # http://localhost:8800

A consent overlay on the first visit, a catalogue with pages and a search box, a basket, a checkout
form with every kind of field and a validation error, a report that takes five seconds to appear, a
delete behind a confirm() dialog, and a sign-in that stops at a code only a person has.

What happened on the site is at /_state (JSON): the evals' checkers read it. Standard library only.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data

STATE = {"seed": 0, "consent": None, "basket": {}, "orders": [], "validation_errors": 0, "report_requests": 0,
         "deleted": [], "signin": {"password_ok": False, "code_ok": False, "code_tries": 0, "by": None},
         "requests": 0}
LOCK = threading.Lock()

STYLE = """
body{font:16px/1.5 system-ui,sans-serif;margin:0;color:#1d2433;background:#f6f4ef}
header{background:#1d2433;color:#fff;padding:12px 24px;display:flex;gap:24px;align-items:center}
header a{color:#fff;text-decoration:none} main{max-width:860px;margin:24px auto;padding:0 24px}
table{border-collapse:collapse;width:100%;background:#fff} td,th{padding:8px 12px;border-bottom:1px solid #e3ded3;text-align:left}
button,.btn{background:#c2410c;color:#fff;border:0;padding:8px 16px;border-radius:6px;font:inherit;cursor:pointer;text-decoration:none}
input,select{font:inherit;padding:6px 8px;border:1px solid #b9b2a3;border-radius:6px} label{display:block;margin:12px 0 4px}
.error{background:#fde8e8;border:1px solid #c81e1e;padding:8px 12px;border-radius:6px;margin:12px 0}
#consent{position:fixed;inset:0;background:rgba(20,24,33,.72);display:flex;align-items:center;justify-content:center;z-index:10}
#consent div{background:#fff;padding:24px;border-radius:12px;max-width:420px}
.spinner{display:inline-block;width:18px;height:18px;border:3px solid #b9b2a3;border-top-color:#c2410c;border-radius:50%;animation:s 1s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
"""

CONSENT = """<div id="consent" role="dialog" aria-label="Cookies"><div><h2>We use cookies</h2>
<p>Choose before you continue.</p>
<form method="post" action="/consent"><input type="hidden" name="next" value="{next}">
<button name="choice" value="necessary">Only necessary</button>
<button name="choice" value="all">Accept all</button></form></div></div>"""


def page(title: str, body: str, *, consent: bool, path: str = "/") -> bytes:
    overlay = CONSENT.format(next=html.escape(path)) if consent else ""
    count = sum(STATE["basket"].values())
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{html.escape(title)} · Fern and Flint</title>"
            f"<style>{STYLE}</style></head><body><header><strong>Fern and Flint</strong>"
            f"<a href='/catalogue'>Catalogue</a><a href='/basket'>Basket ({count})</a><a href='/report'>Reports</a>"
            f"<a href='/drafts'>Drafts</a><a href='/signin'>Sign in</a></header><main><h1>{html.escape(title)}</h1>"
            f"{body}</main>{overlay}</body></html>").encode()


class Shop(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # quiet
        pass

    # ---- plumbing ----

    def _cookies(self) -> SimpleCookie:
        return SimpleCookie(self.headers.get("Cookie", ""))

    def _send(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(json.dumps(obj).encode(), status, "application/json")

    def _redirect(self, where: str, headers: dict[str, str] | None = None) -> None:
        self._send(b"", 303, headers={"Location": where, **(headers or {})})

    def _form(self) -> dict[str, str]:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _html(self, title: str, body: str) -> None:
        consent = "consent" not in self._cookies()
        self._send(page(title, body, consent=consent, path=self.path))

    # ---- pages ----

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        seed = STATE["seed"]
        with LOCK:
            STATE["requests"] += 1
        if url.path == "/_state":
            with LOCK:
                return self._json(STATE)
        if url.path == "/":
            return self._html("Outdoor things that last", "<p>Tents, lanterns, kettles and more.</p>"
                              "<p><a class=btn href='/catalogue'>Browse the catalogue</a></p>")
        if url.path == "/catalogue":
            items = data.catalogue(seed)
            q = query.get("q", "").strip().lower()
            if q:
                items = [p for p in items if q in p["name"].lower()]
            pages = max(1, -(-len(items) // data.PAGE_SIZE))
            n = min(max(1, int(query.get("page", "1") or 1)), pages)
            rows = "".join(f"<tr><td><a href='/product/{p['id']}'>{html.escape(p['name'])}</a></td>"
                           f"<td>{data.money(p['price_cents'])} EUR</td></tr>"
                           for p in items[(n - 1) * data.PAGE_SIZE:n * data.PAGE_SIZE])
            extra = f"&q={html.escape(q)}" if q else ""
            nav = (f"<a href='/catalogue?page={n - 1}{extra}'>Previous</a> " if n > 1 else "") + f"Page {n} of {pages}" + \
                  (f" <a href='/catalogue?page={n + 1}{extra}'>Next</a>" if n < pages else "")
            return self._html("Catalogue", "<form action='/catalogue'><label for=q>Search</label>"
                              f"<input id=q name=q value='{html.escape(q)}'> <button>Search</button></form>"
                              f"<table><tr><th>Product</th><th>Price</th></tr>{rows}</table><p>{nav}</p>")
        if url.path.startswith("/product/"):
            found = next((p for p in data.catalogue(seed) if str(p["id"]) == url.path.rsplit("/", 1)[1]), None)
            if not found:
                return self._send(page("Not found", "<p>No such product.</p>", consent=False), 404)
            return self._html(found["name"], f"<p>Price: <strong>{data.money(found['price_cents'])} EUR</strong></p>"
                              f"<form method=post action='/basket'><input type=hidden name=id value='{found['id']}'>"
                              "<label for=qty>Quantity</label><input id=qty name=qty type=number min=1 value=1> "
                              "<button>Add to basket</button></form>")
        if url.path == "/basket":
            items = {p["id"]: p for p in data.catalogue(seed)}
            with LOCK:
                lines = [(items[int(i)], q) for i, q in STATE["basket"].items()]
            rows = "".join(f"<tr><td>{html.escape(p['name'])}</td><td>{q}</td><td>{data.money(p['price_cents'] * q)} EUR</td></tr>"
                           for p, q in lines)
            total = sum(p["price_cents"] * q for p, q in lines)
            body = (f"<table><tr><th>Product</th><th>Quantity</th><th>Line total</th></tr>{rows}</table>"
                    f"<p>Total: <strong>{data.money(total)} EUR</strong></p><p><a class=btn href='/checkout'>Checkout</a></p>"
                    "<form method=post action='/basket/clear'><button>Empty the basket</button></form>") if lines \
                else "<p>Your basket is empty.</p>"
            return self._html("Basket", body)
        if url.path == "/checkout":
            return self._html("Checkout", self._checkout_form({}, None))
        if url.path.startswith("/order/"):
            with LOCK:
                found = next((o for o in STATE["orders"] if str(o["number"]) == url.path.rsplit("/", 1)[1]), None)
            if not found:
                return self._send(page("Not found", "<p>No such order.</p>", consent=False), 404)
            return self._html("Thank you", f"<p>Your order number is <strong>{found['number']}</strong>.</p>"
                              f"<p>Total paid: {data.money(found['total_cents'])} EUR</p>")
        if url.path == "/report":
            return self._html("Reports", """<p>The monthly report takes a few seconds to build.</p>
<button id=go>Build the report</button><p id=out aria-live=polite></p>
<script>document.getElementById('go').onclick=async()=>{const o=document.getElementById('out');
o.innerHTML='<span class=spinner></span> Building the report...';
const r=await fetch('/api/report');const j=await r.json();o.textContent='Report ready. Reference: '+j.code;};</script>""")
        if url.path == "/api/report":
            with LOCK:
                STATE["report_requests"] += 1
            time.sleep(5)
            return self._json({"code": data.report_code(seed)})
        if url.path == "/drafts":
            with LOCK:
                left = [d for d in data.DRAFTS if d not in STATE["deleted"]]
            rows = "".join(f"<tr><td>{html.escape(d)}</td><td><form method=post action='/drafts/delete' "
                           f"onsubmit=\"return confirm('Delete the draft {html.escape(d)}? This cannot be undone.')\">"
                           f"<input type=hidden name=name value='{html.escape(d)}'><button>Delete</button></form></td></tr>"
                           for d in left)
            return self._html("Drafts", f"<table><tr><th>Draft</th><th></th></tr>{rows}</table>")
        if url.path == "/signin":
            return self._html("Sign in", self._signin_form(None))
        if url.path == "/signin/code":
            return self._html("One more step", self._code_form(None))
        if url.path == "/api/signin":
            with LOCK:
                return self._json({"code_ok": STATE["signin"]["code_ok"]})
        if url.path == "/account":
            with LOCK:
                ok = STATE["signin"]["code_ok"]
            if not ok:
                return self._redirect("/signin")
            return self._html("Your account", f"<p>Signed in as {data.USERNAME}.</p>"
                              f"<p>Customer account number: <strong>{data.account_number(seed)}</strong></p>")
        return self._send(page("Not found", "<p>Nothing here.</p>", consent=False), 404)

    def _checkout_form(self, v: dict[str, str], error: str | None) -> str:
        def val(k: str) -> str:
            return html.escape(v.get(k, ""))
        options = "".join(f"<option{' selected' if v.get('country') == c else ''}>{c}</option>" for c in data.COUNTRIES)
        return ((f"<p class=error role=alert>{html.escape(error)}</p>" if error else "") +
                "<form method=post action='/checkout'>"
                f"<label for=name>Full name</label><input id=name name=name value='{val('name')}'>"
                f"<label for=email>Email</label><input id=email name=email type=email value='{val('email')}'>"
                f"<label for=country>Country</label><select id=country name=country><option value=''>Choose...</option>{options}</select>"
                "<fieldset><legend>Delivery</legend>"
                f"<label><input type=radio name=delivery value=standard{' checked' if v.get('delivery') == 'standard' else ''}> Standard (3 to 5 days)</label>"
                f"<label><input type=radio name=delivery value=express{' checked' if v.get('delivery') == 'express' else ''}> Express (next day)</label></fieldset>"
                f"<label><input type=checkbox name=gift value=yes{' checked' if v.get('gift') else ''}> Gift wrap</label>"
                f"<label><input type=checkbox name=terms value=yes{' checked' if v.get('terms') else ''}> I accept the terms of sale</label>"
                "<p><button>Place the order</button></p></form>")

    def _signin_form(self, error: str | None) -> str:
        return ((f"<p class=error role=alert>{html.escape(error)}</p>" if error else "") +
                "<form method=post action='/signin'><label for=user>Username</label><input id=user name=user>"
                "<label for=pass>Password</label><input id=pass name=pass type=password><p><button>Sign in</button></p></form>")

    def _code_form(self, error: str | None) -> str:
        return ((f"<p class=error role=alert>{html.escape(error)}</p>" if error else "") +
                "<p>We sent a 6-digit code to the phone ending in 42. Enter it to finish signing in.</p>"
                "<form method=post action='/signin/code'><label for=code>Code</label>"
                "<input id=code name=code inputmode=numeric autocomplete=one-time-code><p><button>Verify</button></p></form>"
                "<script>setInterval(async()=>{const j=await (await fetch('/api/signin')).json();"
                "if(j.code_ok)location='/account';},1500);</script>")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        form = self._form()
        seed = STATE["seed"]
        with LOCK:
            STATE["requests"] += 1
        if url.path == "/consent":
            with LOCK:
                STATE["consent"] = form.get("choice")
            return self._redirect(form.get("next") or "/", {"Set-Cookie": f"consent={form.get('choice', 'x')}; Path=/"})
        if url.path == "/basket":
            with LOCK:
                key = str(int(form.get("id", "0")))
                STATE["basket"][key] = STATE["basket"].get(key, 0) + max(1, int(form.get("qty") or 1))
            return self._redirect("/basket")
        if url.path == "/basket/clear":
            with LOCK:
                STATE["basket"] = {}
            return self._redirect("/basket")
        if url.path == "/checkout":
            missing = [label for key, label in (("name", "your full name"), ("email", "your email"),
                                                ("country", "a country"), ("delivery", "a delivery option"))
                       if not form.get(key)]
            error = None
            if missing:
                error = "Please give " + ", ".join(missing) + "."
            elif not form.get("terms"):
                error = "You must accept the terms of sale to place the order."
            elif not STATE["basket"]:
                error = "Your basket is empty."
            if error:
                with LOCK:
                    STATE["validation_errors"] += 1
                return self._html("Checkout", self._checkout_form(form, error))
            items = {p["id"]: p for p in data.catalogue(seed)}
            with LOCK:
                lines = [{"id": int(i), "name": items[int(i)]["name"], "qty": q} for i, q in sorted(STATE["basket"].items())]
                order = {"number": 5000 + len(STATE["orders"]) + 1, "lines": lines,
                         "name": form["name"].strip(), "email": form["email"].strip(), "country": form["country"],
                         "delivery": form["delivery"], "gift": bool(form.get("gift")),
                         "total_cents": sum(items[line["id"]]["price_cents"] * line["qty"] for line in lines)}
                STATE["orders"].append(order)
                STATE["basket"] = {}
            return self._redirect(f"/order/{order['number']}")
        if url.path == "/drafts/delete":
            with LOCK:
                if form.get("name") in data.DRAFTS and form["name"] not in STATE["deleted"]:
                    STATE["deleted"].append(form["name"])
            return self._redirect("/drafts")
        if url.path == "/signin":
            ok = form.get("user") == data.USERNAME and form.get("pass") == data.PASSWORD
            with LOCK:
                STATE["signin"]["password_ok"] = ok
            if not ok:
                return self._html("Sign in", self._signin_form("That username and password do not match."))
            return self._redirect("/signin/code")
        if url.path in ("/signin/code", "/_person/code"):
            # /_person/code is the person's phone: the evals' stand-in for a human typing the code.
            with LOCK:
                STATE["signin"]["code_tries"] += 1
                ok = STATE["signin"]["password_ok"] and form.get("code") == data.login_code(seed)
                if ok:
                    STATE["signin"]["code_ok"] = True
                    STATE["signin"]["by"] = "person" if url.path == "/_person/code" else "page"
            if url.path == "/_person/code":
                return self._json({"ok": ok})
            if not ok:
                return self._html("One more step", self._code_form("That code is not right."))
            return self._redirect("/account")
        return self._send(b"not found", 404, "text/plain")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--port", type=int, default=8800)
    args = parser.parse_args()
    STATE["seed"] = args.seed
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Shop)
    print(f"Fern and Flint is open at http://localhost:{args.port} (seed {args.seed})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
