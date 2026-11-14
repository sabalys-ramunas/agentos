# Red team: try to catch our own lanes as bots

Status: built 2026-11-05 (`agentos lane redteam`); first runs on this Mac (Camoufox 158.0.1-beta.41)
and on the node VM (scratch lanes, production engine and pins). Complements the stealth audit
([stealth.md](stealth.md)): that page asks "is each lane one consistent device?"; this one asks "if
we ran an anti-bot vendor's sensor and server, would they flag us?", and keeps asking on every
change.

## What it is

A detector site we own, built like a site behind a commercial bot defence, and lanes that visit it
the way agents drive them.

- **The site** (`agentos/stealth/redteam/site/`): a landing page, a sign-up form and a long feed. Every
  page loads `sensor.js`, which does what Akamai, PerimeterX, DataDome and friends do in the page:
  it records every input event (mouse, wheel, keys, input, scroll, focus) with its timing against the
  animation frame, and on the landing page runs active tests a spoofed fingerprint cannot answer
  with a getter: real CPU parallelism against `hardwareConcurrency`, WebGL draw speed against the
  claimed GPU, the frame rate, the timer resolution.
- **The server** (`server.py`) records what only a server sees: every request's headers by name, in
  the order they arrived, and the navigation headers (Referer, `Sec-Fetch-*`) of each page load.
- **The visit** (`scenario.py`) goes through the production `PlaywrightInteraction`
  (`browser_navigate`, `browser_snapshot`, `browser_act`), so every event comes from the code path an
  agent's tool calls take: arrive by URL, follow a link to the form, type a name, an email and a
  Lithuanian sentence with an emoji, pick a country, tick a box, submit, wheel-scroll the feed to
  post 25 and like it, jump to the next page by URL (as agents do), click Home.
- **The detectors** (`detectors.py`), pure functions over the recorded visit, each `caught`,
  `suspicious`, `clean` or `skipped`, with evidence and a fix. Pooled detectors read every visit
  together, as a site sees an account over weeks, and compare with human baselines when given.
- **Human baselines**: the same site served to a person (`--serve`). Their visits are the control:
  a detector that catches a person is wrong, and the report lists it as a false positive. With
  baselines, the pooled detectors compare each timing distribution with the people's (two-sample KS).

## Running it

```
# On a Mac or any machine with Camoufox: the agent input path, fast. Not the node's pins or Xvfb.
agentos lane redteam --local --runs 3

# On the node: scratch lanes on a test slot, production engine, pins and displays (like stealth-check)
AGENTOS_TEST_SLOT=34 scripts/node-test.sh redteam --python -c "from agentos.cli import app; app()" \
    lane redteam --slot 34 --lanes 2 --runs 3 --out /tmp/redteam-out/run1

# A human baseline: open the printed URL in your own browser, follow the yellow box, Ctrl-C.
agentos lane redteam --serve 8777                # saves to bench/redteam-humans/
agentos lane redteam --local --humans bench/redteam-humans
```

`--out` keeps `report.txt`, `report.json` and every visit's raw session. Each run appends one line to
`bench/redteam.jsonl` (each detector's worst level, the commit, the versions) and prints what got
better or worse since the last run of the same backend (`--no-record` skips it). Exit 1 when a
detector catches a visit. The node backend reuses the stealth check's slot guards, so it never
touches the node's own lanes, displays or ports; its lanes reach the detector site on loopback, so
they run without a proxy (the network layer behind a proxy is not covered, see the gaps below).

## The detectors

| Detector | Layer | Caught / suspicious when | Basis |
|---|---|---|---|
| `kbd.dwell` | keyboard | median key hold < 20 ms / < 40 ms | people hold keys 50 to 150 ms (keystroke dynamics literature; every behavioural vendor reads it) |
| `kbd.keyless_text` | keyboard | a letter arrives as `insertText` with no keydown / only emoji do | a keyboard sends a keydown for every letter, any layout; only paste and input methods do not, and they leave their own events |
| `kbd.shift` | keyboard | a capital with neither Shift nor Caps Lock | impossible on hardware |
| `kbd.rollover` | keyboard | 40+ keys and none pressed before the last came up | fluent typists overlap keys |
| `kbd.corrections` | keyboard | 60+ characters and no Backspace | people make typos (weak) |
| `mouse.teleport` | mouse | a press away from where the pointer moved / with no movement on the page | a real press happens where the pointer is |
| `mouse.press` | mouse | button held < 25 ms | |
| `mouse.path` | mouse | median straightness > 0.985, or none of 6+ movements ends with a correction | aimed human movements curve and end with a corrective sub-movement (Fitts; Meyer's model) |
| `mouse.entry` | mouse | the pointer first appears at the top-left corner | a driver's pointer starts at (0, 0) |
| `scroll.wheel_pointer` | scroll | a wheel event away from the pointer | |
| `scroll.unexplained` | scroll | > 300 px of scrolling with no wheel, key or touch behind it | Playwright's own scrollIntoView before a click |
| `focus.input` | focus | input into an unfocused window or a hidden page | |
| `nav.typed_urls` | navigation | an inner page opened mid-visit with `Sec-Fetch-Site: none` and no Referer | agents jump by URL; people click |
| `http.headers` | http | header order unlike Firefox's, or a proxy's headers (Via, X-Forwarded-For) | server-side, unspoofable from the page |
| `http.consistency` | http | User-Agent or Accept-Language differ from what the page reads | |
| `env.cores` | environment | N workers run like < 0.4 N (claimed 4+) / < 0.6 N | a getter can lie; throughput cannot |
| `env.gpu` | environment | a hardware GPU name but > 12 ms a frame on the test shader / software renderer named | llvmpipe under an Intel or AMD name |
| `env.frames` | environment | a refresh rate no monitor has, or > 20% late frames | |
| `env.timer` | environment | `performance.now()` coarser than stock Firefox's 1 ms | resistFingerprinting |
| `fp.*` | fingerprint | the stealth probe's findings (`stealth.dump_findings`) | stealth.md |
| `pooled.kbd.flight`, `pooled.mouse.arrival_to_press`, `pooled.scroll.wheel_gap` | timing | with baselines: not the people's distribution; without: indistinguishable from a flat `random.uniform` range (KS < 1.22/sqrt(n), no skew, 60+ samples) | people's timings are log-normal. Measured: a flat range is flagged 95% of the time; a steady typist (log-normal sigma 0.25) 6%, a typical one 0.3% |
| `pooled.kbd.hold`, `pooled.mouse.press_hold`, `pooled.mouse.speed` | | not the people's distribution (KS, p < 0.001) | needs baselines |
| `pooled.mouse.frame_phase` | mouse | pointer moves spread over the frame while the people's sit on the refresh tick | Camoufox #977 |
| `link.A~B` | link | two lanes share 6 of 7 traits / share 3+ and run JavaScript at the same speed | one machine under two fingerprints |

Thresholds come from the literature and from what real Firefox does until there are human
baselines; the baselines then do two jobs: they replace "people do X" with "these people did X", and
they check every threshold (a detector that catches a person is reported).

## First results (2026-11-05)

Local (this Mac, 2 lanes x 2 visits) and node VM (1 scratch lane x 3 visits, Linux identity,
production engine and pin). Both: **detected on every visit**.

| Finding | Local | Node | Why | Fix |
|---|---|---|---|---|
| `kbd.dwell`: keys released 1 to 5 ms after they go down | caught 4/4 | caught 3/3 | `human.type_text` calls `keyboard.type(ch)` with no delay; Playwright then sends keyup right after keydown. Only `press()` holds keys | type each character with `keyboard.press(ch, delay=...)`, the hold drawn log-normal around 90 ms |
| `kbd.keyless_text`: ą, ė, į, ū, š, ž, Š arrive with no key | caught 4/4 | caught 3/3 | Playwright's `type` inserts any character outside the US layout as text, with no keydown; every Lithuanian (or Polish, German, French) word an agent types shows it | send such letters as key presses (`keyboard.down`/`up` with the letter as the key), the way a layout with them does; emoji through a composition, as an emoji picker does |
| `env.gpu`: "Intel(R) HD Graphics 400" draws at 54 to 58 ms a frame | (real GPU, 1.3 ms) | caught 3/3 | the node renders WebGL with llvmpipe; stealth.md listed this as "unknown: no published report that a site does". Twenty lines of WebGL timing see it | a GPU node; or identities whose renderer says llvmpipe (honest, but a VM to every site); or Camoufox's virtual GPU |
| `env.cores`: claims 4, runs like 1.5 to 2.1 | ok (10 claimed, 6 to 7 measured) | caught 1/3, suspicious 2/3 | the 2 vCPU VM's cores are snapped up to 4 in the pin | pin `hardwareConcurrency` to what the node can run in parallel, or a node with the cores |
| `kbd.rollover`, `kbd.corrections` | suspicious | suspicious | one key at a time, never a typo | occasional overlap; an occasional typo fixed with Backspace |
| `nav.typed_urls` | suspicious | suspicious | `browser_navigate` mid-visit | click the link when the page has one (exactly that address, fragment included), only for a caller whose `browser_act` would run on its own: a read-only agent goes to the address directly, so this tell stays for it. The click is on the record: a `link_click` row (draft, with the address) under the call, written right before the press, and the call's `tool_end` reads draft |
| `pooled.scroll.wheel_gap`: flat between 19 and 64 ms | ok at n=268 locally | suspicious | Camoufox splits a wheel step into notches at uniform random gaps | log-normal gaps, varied gesture shapes |
| `mouse.path`: straightness 0.989 | suspicious 1/4 | suspicious 1/3 | Camoufox humanize paths are nearly straight | more curve, an occasional overshoot |
| `fp.consistency` (Camoufox #974, window position) | caught (local only) | ok | locally nothing places the window; the engine does on the node | none needed on the node; `--local` leaves the probe off by default |
| `link`: two local lanes one device | caught | (one lane) | both local browsers share this Mac's screen and cores | expected locally; run `--lanes 2` on the node |

Clean everywhere: no teleporting presses, real press durations, wheel events from the pointer, no
programmatic scrolling, focused windows, Firefox's header order, headers that agree with the page,
`Sec-Fetch-User: ?1` on agent navigations (Playwright's `goto` looks like a typed URL), 1 ms timers,
58 to 120 Hz frames, shift with every capital, and the stealth probe clean on the node.

## Deep checks: the engine and the input path (2026-11-05)

Method: differential, so no threshold is guessed. The same page runs in a reference and in our
setup on the same machine, and every difference is a candidate tell.

- **Engine probe** (`site/deep.html`, loaded once per lane by the bench): the whole API surface
  (globals, navigator, document, element, CSS, Intl), permission defaults, the machine (storage quota,
  media decoders, audio device, voices), JS engine limits (recursion depth on the page and in a
  worker, allocation), WebGL claimed limits against what allocates. Run on the node VM in stock
  Firefox 158.0.1, the bare Camoufox binary, Camoufox under Playwright and a production lane.
- **Input probe** (`site/input.html`): every input event field. The same gestures from Playwright
  (Juggler) and from xdotool (XTest, the X server path a real mouse and keyboard take), same browser.

| Finding | Stock Firefox | Production lane | Detector | Fix level |
|---|---|---|---|---|
| API surface (globals, props, CSS, Intl, features, errors) | | identical | | none needed |
| Geolocation permission on a new site | `prompt` | `granted` (Camoufox sets `permissions.default.geo = 1` so the API agrees with the spoofed position) | `engine.geo_permission` | setting: `permissions.default.geo = 0`, no pinned position |
| WebGL limits | real | claims MAX_TEXTURE_SIZE 4096 or 8192; twice that allocates | `engine.webgl_limits` (caught) | pin the renderer's real limits; or a Camoufox patch that enforces claimed ones |
| Audio output | 0 channels (this VM) | 0 channels | `engine.audio` | node: a null audio sink (PipeWire/PulseAudio) |
| Storage quota | 4 GB (38 GB disk) | 4 GB, the same on every lane | `engine.quota` | setting: `dom.quotaManager.temporaryStorage.fixedLimit` 50 GiB or more (reads 10 GiB) |
| 1080p H.264 power-efficient | no (no GPU) | no, under an Intel GPU name | `engine.hw_decode` | GPU node; or Camoufox `media:spoof_codecs` and a patch |
| Pointer moves merged by the browser | | never (Juggler sends synthesized events; Firefox never merges them) | `mouse.merged` | driver: XTest input (python-xlib) at a mouse's report rate |
| Key hold | | 3 to 15 ms | `kbd.dwell` | driver: hold keys (or XTest) |
| ą č ę ū | | no keydown at all (Playwright); keydown with empty `code` (xdotool) | `kbd.keyless_text`, `kbd.physical_key` | driver: XTest with the right layout on the lane's display (`setxkbmap lt`), so ą is Digit1 |
| Recursion depth | 39,300 | 39,200 (Linux); on macOS, Juggler's debugger costs 800 frames (38,300 vs 39,100) | | none needed on the node |
| `document.hasFocus()` with no window manager | false | true (Juggler forces focus) | | real users read true; a popup and its opener both true is the tell (not measured) |
| Two production lanes | | same GPU name ("Intel HD 400"), screen, cores, dpr; timezone from the one exit | `link.*` (suspicious: shared rare GPU) | identities from a wider pool; separate exits |

XTest notes: xdotool's wheel (buttons 4 and 5) does not scroll Firefox under Xvfb (it reads XInput2
smooth scrolling), so an XTest driver needs XI2 scroll events or keeps Juggler's wheel. xdotool runs
one process per move (about 60 Hz here); a real driver should use python-xlib at 125 to 1000 Hz.

Why Camoufox has not fixed these: its patches spoof values (user agent, screen, fonts, WebGL names);
behaviour and hardware truth (speed, disk, decoders, sound) come from the host. Juggler, Playwright's
Firefox driver, needs its synthesized input, debugger and forced focus to work, and Camoufox keeps
some trade-offs on purpose (the global geolocation grant, no back-forward cache because it breaks
`go_back`). The research notes and sources are in the session that built this page (2026-11-05).

## The public gauntlet (2026-11-05)

`agentos lane redteam --slot S --gauntlet --proxy-file F [--targets kinds or names] [--out DIR]`: one
production scratch lane, through a real proxy, visits 32 public targets (`gauntlet.py`): detection
panels (CreepJS, BrowserScan, Pixelscan, incolumitas, deviceandbrowserinfo, rebrowser, Sannysoft,
browserleaks), TLS endpoints, and sites behind Cloudflare, DataDome, Akamai, F5 and Radware (Reddit,
LinkedIn, Instagram, X, TikTok, Amazon, Google, Booking, Glassdoor, Indeed, Stack Overflow...). Targets
and first-response rules are ported from Ian L. Paterson's anti-detect browser benchmark (MIT), so
both verdicts are printed. Ours reads the page that settled: a Cloudflare managed challenge answers
403 first, and Camoufox solving it is "challenged", not "blocked". A proxy that refuses connections
under load (`NS_ERROR_PROXY_FORBIDDEN`) is retried after 45 and 90 s. Also run on our own site:
Fingerprint's open-source BotD (`site/judges.html`, vendored, monitoring off), as `judge.botd`.

Run on the node VM, Linux identity, exit in Vilnius (Bite Lietuva):

| | Result |
|---|---|
| Our rules | 26 ok, 2 passed a Cloudflare challenge (Glassdoor, Stack Overflow), 1 blocked (Indeed: "Additional Verification Required"), 2 proxy errors; Medium counted gated here was its short home page (fixed) |
| Paterson's rules | 24 ok, 3 gated, 3 blocked (his Camoufox 135: 25, 3, 3) |
| TLS and HTTP/2 | JA4 `t13d1517h2_8daaf6152771_3cbfd9057e0d`, JA3 `9d42e90b...`, Akamai H2 `6ea73faa...`: identical to stock Firefox 158.0.1 through the same proxy (the stale cipher of Camoufox 135 is gone) |
| BrowserScan, deviceandbrowserinfo, rebrowser, Pixelscan bot check | normal / human / 0 failed / human |
| CreepJS | 13 to 19% "like headless" |
| Sannysoft | only the Chrome-only row (every Firefox fails it) |
| incolumitas | `webDriverAdvanced` FAIL (unverified whether stock Firefox fails it too); behavioural score never computed, **also for stock Firefox 158 moved with real X input, and with uBlock Origin removed**: broken for Firefox, not a signal |
| Pixelscan fingerprint | "inconsistent, masking detected": **not a signal**; the owner's real Edge and Safari get the same (Edge also "automated behavior detected"). Uncalibrated, ignore |

Attribution, stock Firefox 158.0.1 through the same proxy and IP: Medium, Indeed and Glassdoor gave
stock Firefox a challenge too ("Just a moment..."), so those come from the exit IP or Firefox on
Linux, not Camoufox. Stack Overflow seemed to load at once in stock Firefox once; a controlled re-test
(17 first navigations, fresh profiles 75 s apart: stock 158 and 159 and Camoufox, with and without
`Ubuntu` in the user agent, cold and warm) got a Cloudflare challenge every time, all solved. The
earlier single load was the exit IP's state at that moment, not the browser. A second gauntlet run
(2-core lanes) gave 28 ok, 2 passed a challenge, 1 blocked (Medium; stock Firefox is challenged there
too), 1 error: which of Medium and Indeed blocks moves between runs on the same IP.

Caution: the stock Firefox reference updates itself (it went from 158.0.1 to 159.0.1 during the
work); keep a copy with updates disabled for differential runs.

Lesson: a public panel is only a judge if real browsers pass it. Each new judge gets a run with a
real browser before its verdicts count.

## Fixes (2026-11-05)

Engine hardening (`engine_camoufox.harden`), applied when a lane is first pinned and recorded in the
pin as meta `hardening: 1`; a lane pinned before keeps its device:

| Detector | Before | After | How |
|---|---|---|---|
| `engine.geo_permission` | `granted` on a site never visited | `prompt`; a request waits, as for a user who ignores the prompt | `permissions.default.geo = 0`, and the browser never gets the position (`browser_view`); the pin keeps it for the exit checks. Measured: position plus `ask` hands the position out silently while the API says prompt (Camoufox #769), so both are needed |
| `engine.quota` | 4 GB, the host's disk, the same on every lane | 10 GiB (10.7 GB), what about 92% of Firefox users read | `dom.quotaManager.temporaryStorage.fixedLimit = 52428800` |
| `engine.webgl_limits` | an Intel identity claims 8192 and 16384 allocates; an NVIDIA GTX 980 identity claims 32768 and 32768 does not allocate | a new Linux lane on a GPU-less node draws an AMD Radeon HD 3200 or R9 200 identity: every recorded Linux Firefox device of both claims 16384, exactly what llvmpipe enforces (measured: 16384 allocates, 32768 fails, WebGL1 and 2) | Camoufox `webgl_config` from `SOFTWARE_HOST_GPUS`. Patching the numbers was tried first and dropped: real Firefox on a Linux Intel GPU reports 8192 (bug 1199923), so a raised Intel claim is its own lie; `harden` now only checks and warns. `webgl.max-size` does not enforce a smaller claim (measured) |
| `env.cores` | 4 claimed on 2 vCPUs (1.5 to 2.4 measured: caught or suspicious) | the host's count: 2 claimed, 1.7 to 1.9 measured, clean; CreepJS, BrowserScan and BrowserLeaks print 2 and flag nothing | a claim above the host's cores is lowered to it. Cost: two lanes on one node share an uncommon count (the link check notes it). A 4 vCPU VM avoids both |

Left as they are, with the reason:
- GPU speed and hardware decoding (`env.gpu`, `engine.hw_decode`): Camoufox claims a real GPU on a
  software-rendering host on purpose ("a software renderer is a far stronger bot signal than any
  GPU mismatch", fingerprints.py). The fix is a GPU in the node (Camoufox `headless="virtual-gpu"`).
- Sound card (`engine.audio`): a null audio sink on the node (PipeWire or PulseAudio), a node setup
  change not yet made.
- uBlock Origin: removing it changed nothing measurable on the gauntlet's panels; kept.

## Android emulator (2026-11-05, first look)

Official emulator on the owner's Mac (M1 Pro): an AVD, Pixel 8, Android 16, Play Store
image, Chrome 133, driven through the device tools (`agent-device`: press, fill, scroll), as an
Android lane's agent would. The detector site served on the Mac; `adb reverse tcp:8790 tcp:8790` so
Chrome reaches it as `http://localhost:8790` (a secure context: over `http://10.0.2.2` the sensors,
storage, media devices and client hints are not available and the probe reads nothing).

| Tell | What a page reads | Verdict |
|---|---|---|
| Model (client hints `getHighEntropyValues`) | `sdk_gphone64_arm64` | emulator by name |
| WebGL renderer | `Android Emulator OpenGL ES Translator (ANGLE (... SwiftShader ...))` | emulator by name, software rendering |
| Motion sensor | gravity (0, 9.8, 0.8), standard deviation 1e-15 over 86 samples | no real accelerometer is noiseless |
| Touch (agent-device taps and swipes) | radiusX/Y 0, force 1 on every touch | injected, not a finger |
| Text (agent-device fill) | the whole value in one `input` event, no `inputType`, no keydown | set, not typed |
| Battery | level 1, charging, chargingTime 0 | weak (a phone on a charger) |
| Storage quota | 6.2 GB | the emulator's small disk; real phones read tens of GB |
| Clean | 4 cores (3.2 measured), 60 Hz, 2 audio channels, permissions `prompt`, hardware video decoding | |

code-root/emulator-android (MIT, read before use; not run as a whole): an AVD farm whose stealth on a
Play Store image is boot-time `-prop` overrides (root-only `setprop` and `build.prop` patches, and a
Frida template for single apps, need a `google_apis` image). Tried its no-root route:
`-prop ro.product.model="Pixel 8"` (and manufacturer, brand, device, name) leaves `ro.product.model`
at `sdk_gphone64_arm64`; the image's own build properties win. Changing the model needs root
(rootAVD, Magisk `resetprop`). No property changes the WebGL renderer: it is the emulator's own GLES
translator, so a website identifies an emulator whatever the props say.

Cost: cold boot 50 s; the emulator process holds 4.0 to 4.5 GB on the host for a 2 GB guest, and
uses 1.4 to 3.3 cores for a minute after boot.

### Rooted, with a device profile (same emulator)

Root: rootAVD (newbit, last commit Oct 2024) with Magisk 31.1 swapped in for its bundled 26.4, which
predates Android 16; it patches the system image's ramdisk.img and keeps ramdisk.img.backup
(`rootAVD.sh <ramdisk> restore` undoes it). Shell root is granted in Magisk's Superuser tab ("+").

| Tell | Stock | Rooted | How |
|---|---|---|---|
| Model | `sdk_gphone64_arm64` | `Pixel 8` | Magisk `resetprop ro.product[.system/.vendor/.product/.odm/.system_ext/...].{model,brand,manufacturer,device,name}`, then Chrome relaunched |
| Battery | 100%, charging | 73%, on battery | emulator console `power ac off`, `power status discharging`, `power capacity 73` |
| Motion | constant, sd 1e-15 | a hand's sway and noise, sd 0.12 | emulator console `sensor set acceleration` and `gyroscope` at 20 Hz from the host (gravity mostly on y, slow sway, noise) |
| Touch | radius 0, force 1 (agent-device) | width 15 to 19, height 18 to 23 px, pressure 0.16 to 0.22, 1 to 3 drifts while down, held 130 to 230 ms | root writes raw multitouch frames (slot, tracking id, position, touch major and minor, pressure) to `/dev/input/event1`, the touchscreen bound to display 0, one write per frame |
| WebGL renderer | `Android Emulator OpenGL ES Translator (... SwiftShader ...)` | unchanged | the emulator's own GLES translator; no property reaches it |

Notes: `sendevent` (one process per event) makes a tap last about 500 ms; raw frames in one write
fix that. Chrome's first-run privacy screens ("ad topics", "ad privacy features") cover the page and
eat taps until dismissed. Text: agent-device `fill` commits the whole value through its own input
method, so the page sees no keys; realistic text on a phone means tapping the on-screen keyboard
(Gboard) with the finger driver, not yet done.

Verdict so far: the profile, battery, sensors and touch can be made ordinary; the GPU name cannot,
without patching the emulator's graphics stack. For websites that read WebGL (most bot defences do),
an emulator stays identifiable. For apps that check properties and files, the rooted profile plus Hide
My Emulator (LSPosed) is the next thing to measure.

Bench gaps this showed: several detectors assume Firefox on a desktop (header order, mouse press
timing; on a phone a tap fires mousedown and mouseup together), so they need to know the browser and
the input kind; a "text set in one event" detector is missing (`kbd.keyless_text` looks only at
`insertText`).

## What it does not cover yet

- **The network behind a proxy.** The node's lanes reach the detector site on loopback, so no
  proxy is in the path. Not measured: TLS (JA4) and HTTP/2 fingerprints as a remote site sees them,
  the proxy exit's TCP stack (p0f) against the claimed OS, latency (a proxy shows as a gap between
  the TCP round trip a server measures and the one the page measures), the exit IP's reputation and
  ASN, WebRTC and DNS through the proxy. Next: a public endpoint we own (a small VPS running this
  server with TLS and a raw ClientHello capture), reached through the lane's real proxy.
- **A real vendor's score.** The gauntlet reads panels and protected sites; no page of ours carries a
  live Turnstile or reCAPTCHA v3 key, which needs a public page and a site key.
- **Account-level behaviour.** Session cadence over days (the same hours, the same pauses, no idle
  browsing between actions) is what platforms model most; one visit cannot show it.
- **Network round-trip proxy detection** (Xue et al., NDSS 2025: TCP RTT ends at the proxy, TLS RTT at
  the VM; over 70% of proxied visits caught). Needs the public endpoint above.
- **Human baselines.** None recorded yet. Until there are some, the pooled detectors that need them
  are skipped and the others judge by literature thresholds.

## The public site (2026-11-06)

`redteam/` closes three of the gaps above: a public HTTPS site, a real proxy in the path, and a place
for human baselines. `redteam/site` is Larkspur, a fake neighbourhood network on Vercel
(larkspur-network.vercel.app), `redteam/detect` is a second detector suite written without reading
our automation code, and `redteam/run/lane_visit.py` drives scratch lanes through it with the pauses a
real agent left in the canary log. `redteam/run/score.py CODE ...` scores the lanes against people.

Vercel shows JA4, country and ASN. It does not show JA3, header order or casing, TCP or HTTP/2
settings, so those still need a VPS.

Runs: 2 lanes x 3 visits through the Vilnius exit, before and after the fixes below. Every visit
finished after the driver was fixed. No human sessions yet, so no rate exists.

| Fix | Bench before | Bench after |
|---|---|---|
| Lithuanian letters are real key presses: the lane's display gets the `lt` layout and xdotool sends them (`node/xkeyboard.py`) | `kbd.keyless_text` caught 6/6 | letters clean, `kbd.physical_key` ok; 1 emoji left, suspect |
| A wheel gesture is our own notches at log-normal gaps (`human._wheel`) | `wheel_gap` suspect | ok |
| Pauses are log-normal inside their range (`human._draw_ms`) | `arrival_to_press` suspect | ok |

Other languages' letters still go in as text: only the `lt` layout is set.

What the second suite found that the bench did not:

- `think_then_burst`, every session before and after: about 70% of a visit is silent and every silence
  ends straight in an action. The pointer never moves while the model thinks. Not fixed.
- `wheel_delta_model`, every session: deltas of a constant 132 px in pixel mode. Needs people's sessions
  to say whether it is a tell.
- A wrong rule of its own: it called our JA4 Chromium's. Stock Firefox 158 sends the same cipher hash.

Its environment layer scored 0 where the bench's `env.gpu` catches every visit: its WebGL sample is too
light to see software rendering. Do not read that 0 as clean.

### Final state (2026-11-06)

Sessions moved from Vercel Blob to Neon's free tier: Blob's free tier suspended the store after about
2,300 small writes. The 6 visits with the 45 s drift stayed in the suspended store and were not scored.
Sessions from before the move are in `redteam/results/archive/` (git-ignored).

Last run: 2 lanes x 3 visits, every visit finished, scored against 2 human sessions (both Mac trackpads,
so no rate is reliable). Lanes were 36 to 46% silent at 6 to 7 pointer events a second; the people 10 to
27% at 8.5 to 20. `think_then_burst` no longer fires. `wheel_delta_model` fires on every lane. The
identity layer's 100% rests on a threshold of 0 from two people, with no named evidence, so it means
nothing yet.

Bench: still detected, 6 of 6, with `env.gpu` the only catch. `kbd.keyless_text` (one emoji), `mouse.merged`,
`engine.audio`, `engine.hw_decode` and `link` are suspects. One run had `link` caught: two scratch lanes
drew the same identity from a pool of two GPUs.

Public checks on a lane, with real browsers on the owner's Mac for comparison:

| Check | Lane | Real browsers |
|---|---|---|
| reCAPTCHA v3 (antcpt) | 0.9, then 0.7 | Chrome, blank profile: 0.9 |
| Fingerprint playground | Suspect Score 0 | Chrome: 0 |
| fingerprint-scan bot score | 5/100 | Firefox 0, Chrome 0, Safari 5 |
| CreepJS "like headless" | 13% | Firefox 13%, Safari 6%, Chrome 31% |

These are single readings of what a page sees on load, with no one moving a mouse. They do not separate
a lane from a real browser, so they prove little either way. CreepJS's number is not a signal. The
2captcha Turnstile demo left the gauntlet: its site key appears to be Cloudflare's always-challenge
test key.
