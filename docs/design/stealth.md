# Stealth and isolation: one ordinary device per lane, and lanes that cannot touch each other

Status: audited and partly fixed 2026-11-04 on Camoufox 158.0.1-beta.41 (Python package 0.5.9,
Playwright 1.62); the review's findings fixed and the owner's decisions built the same day (section 7).
Backlog: "Pin the complete fingerprint", "Startup self-check", "Soak and chaos
tests", "Lane and node benchmarks", "RAM-aware warm-lane cap and per-lane memory limits".

The product fails if a site sees a lane as automated, links two lanes to each other, or one lane
disturbs another. This page is the audit of both halves against what anti-bot vendors look at in a
Playwright-driven Firefox today, what was fixed, what is still open, and the repeatable check that
catches a regression.

## 1. The check

Two commands, both in `openajar.stealth` (and a third, `openajar lane redteam`, that tries to catch the
lanes the way a bot defence would, input and hardware included: [redteam.md](redteam.md)):

- `openajar lane audit [--json]` reads every pinned fingerprint on a node and nothing else (no browser,
  no request), so it is safe on a node with live lanes. Per pin: user agent, platform, oscpu and
  User-Agent header agree; the WebGL renderer fits the OS and is not a software renderer; no font
  of another OS; Accept-Language follows the locale; the window fits the screen; no touchscreen on a
  Mac or Linux desktop; every key Camoufox would redraw is pinned; the Firefox version against the
  installed browser; Camoufox's own coherence rules (`camoufox.coherence`). Across pins: two lanes on
  one device behind different proxies (fail), behind one proxy (warn: right only for AUD-02's one
  company computer), the same font list, one exit IP pinned through different proxies. Devices
  (AUD-02, docs/PROXIES.md): a device's pin is audited once, as `device:<name>`, and its lanes count as
  that one computer; the audit fails two devices on one proxy or one exit IP, a device's lane on
  another proxy than the device's, and lanes of two workspaces on one device; it warns on a device
  and a lane of its own behind one proxy, and on a pin whose timezone, country or locale is not where
  its proxy exits now (the lane check's daily lookup in `cache/exit-geo.json`).
- `openajar lane stealth-check --slot S [--lanes N] [--relaunches R] [--os windows,macos,linux]
  [--soak-minutes M --kill-every K] [--public] [--proxy host:port:user:pass] [--out DIR]` opens N
  scratch lanes (no account) in a throwaway OPENAJAR_HOME on a test slot's own displays and ports,
  with the production lane manager, engine and displays. It reads the node.toml of OPENAJAR_HOME, of
  /var/lib/agentos and of every running agentosd, and refuses a slot that touches any of their
  displays, ports or homes; its display sweep stops only processes that name its own home. SIGTERM
  and SIGHUP (a closed terminal) close its lanes as Ctrl-C does, and the next check stops what a
  check killed outright left (scratch homes are `openajar-stealth-*` with an owner file). Each lane loads `stealth_probe.html` (shipped
  in the package; the node tests' `fingerprint.html` is the same file) from a local server, and the
  page's own script dumps what a fingerprinting script reads. The check reports, per lane and across
  lanes: automation traces, consistency problems, relaunch drift, collisions, leaks, browser memory
  (PSS) and the node's available memory with all lanes warm, the warm lanes a node of this and of
  4, 8 and 16 GB could hold, and first-launch and relaunch wake times. `--soak-minutes` cycles the
  lanes (wake, load, sleep at random) and every K seconds kills one lane's browser with SIGKILL while
  another lane loads a page: the other lane must finish with the same browser process, and the
  killed one must wake again as the same pinned device; descriptor and driver counts are sampled for
  leaks. Before every wake it reads the available memory and stops, closing its lanes, under
  `--min-available-mb` (2048 by default). `--public` also visits CreepJS, Sannysoft, BrowserScan and
  Pixelscan from the first lane and keeps screenshots and page text (read by a person; nothing is
  scored, and they leave from the node's IP unless `--proxy` is given).

The lanes of the check differ from production lanes in one way: the engine may reach private
addresses (the probe server is on localhost), so the network guard's PAC is not installed. The
guard has its own node tests (`test_network_guard_node.py`, `test_stealth_node.py`).

Running it. On the node, from a dev install (docs/DEVELOPING.md):
`OPENAJAR_HOME=$PWD/.agentos-home .venv/bin/openajar lane stealth-check --slot 33 --lanes 3 --out /tmp/stealth`.
Without a node, in the Docker node (Ubuntu 24.04 arm64, the same apt packages, Camoufox fetched;
recipe in section 6). The node tests are `tests/node/test_stealth_node.py`.

## 2. The stealth audit

Status: **ok** (checked, nothing to fix), **fixed** (a gap fixed on this branch), **gap** (open),
**unknown** (not measurable here). Evidence names the measurement or the source; vendor claims and
single reports are marked weak.

### Automation traces

| Check | Status | Evidence | Fix or why not |
|---|---|---|---|
| `navigator.webdriver` | ok | false, a native getter on `Navigator.prototype`, no own property (probe, Windows and Linux identities) | |
| Playwright and Juggler globals (`__pwInitScripts`, `__playwright__binding__`, `__juggler`, Selenium, Puppeteer names) | ok | none on `window` (probe scans every global by name and pattern). openajar never uses `add_init_script`, `expose_binding` or main-world evaluation (grep); our scripts run in Camoufox's isolated world. Beta.41 hides Juggler (Camoufox #852); the cloverlabs fork 0.6.0 leaked these globals (#733) | keep `main_world_eval` off; the probe fails the check if a name appears |
| Patched (non-native) functions | ok | 15 functions fingerprinting scripts test (`Function.prototype.toString`, navigator and screen getters, `Permissions.query`, canvas, WebGL, audio, timezone, `getBoundingClientRect`) are native code | Camoufox patches in C++ |
| `window.chrome`, driver attributes on `<html>` | ok | undefined, none | |
| A worker or a frame that sees another browser | ok | a dedicated worker and a `srcdoc` frame report the same user agent, platform, cores, languages, timezone and screen as the page, and an `OffscreenCanvas` WebGL in the worker names the same GPU | |
| Headless tells | ok | headful on Xvfb; five PDF plugin entries with `pdfViewerEnabled`; pointer fine, hover; toolbar height 85 px | |
| The real window position (Camoufox #974) | **fixed** | a page reads `window.screenX/Y` from the pin and `mozInnerScreenX/Y` (and every mouse event's `screenX/Y`) from the real window, which sat at +0+0 with no window manager: a lane pinned at (206, 2) showed its content at (0, 85), and every macOS identity (`screenY` 25, under the menu bar) showed 25 px too high | each lane's display is now its pinned screen, and the engine moves the browser's window to the pinned position through libX11 on every launch (`place_window`); a page reads `mozInnerScreenY = screenY + toolbars` (measured 115 = 30 + 85). Node tests: `test_the_browser_window_sits_where_its_pin_says`, the probe's 974 check |
| Window larger than the screen (Camoufox #972) | ok | no window manager adds a frame; the probe and the audit check `outerWidth <= screen.width` | |
| CSS `device-width`, `device-height`, `resolution` against `screen` and `devicePixelRatio` | ok | all match (probe) | |
| Keystrokes of `browser_act` type and fill | **fixed** for holds, rollover and corrections; **gap** for letters outside the US layout (measured 2026-11-05) | keys are released 1 to 5 ms after they go down (people hold them 50 to 150 ms), and letters outside the US layout (ą, ė, š, ž) arrive with no key at all ([redteam.md](redteam.md), `kbd.dwell`, `kbd.keyless_text`) | hold each key (`keyboard.press(ch, delay=...)`); send non-US letters as key presses |
| Mouse timing of Camoufox's humanize (#977: 21 to 43% of `mousemove` events on an animation frame, 90% in stock Firefox) | gap | upstream report, reproducible by a page comparing events with `requestAnimationFrame` | not fixable here without replacing humanize; watch Camoufox PR #978 and take it when it lands |
| Our own input path | ok | every agent click, press, scroll and keystroke goes through `node/human.py`: wheel scrolling, a look before acting, a random point in the element, a real press of 60 to 150 ms, typing one character at a time at 70 to 190 ms with pauses; no `fill()`, `click()` shortcuts or synthetic events (`node/interaction/`, `flow_driver.py`) | |
| `page.route` interception (PerimeterX and DataDome have detected it, #271, #428) | ok by default | lanes run no route; only a strict egress lane routes, and only while an agent acts (egress-guard.md) | leave `egress_strict` off on sites behind those vendors |
| uBlock Origin in every lane (Camoufox's default add-on) | ok, by choice | `UBO` is bundled and loaded on every launch; an ad blocker is common in real Firefox | the owner can exclude it (Camoufox `exclude_addons`) if a site breaks |

### One consistent device

| Check | Status | Evidence | Fix or why not |
|---|---|---|---|
| User agent, platform, oscpu, appVersion, User-Agent header | ok | consistent in every pin and probe; `openajar lane audit` checks each pin | |
| Fonts against the claimed OS | ok | a Windows identity shows 49 Windows fonts and no Linux font; a Linux identity shows Linux fonts and Calibri and Cambria, which real Linux desktops answer through Carlito and Caladea | |
| GPU name against the OS | ok | ANGLE (Direct3D 11) strings on Windows identities, an AMD name on Linux ones | |
| Windows or macOS identity on a Linux node (Camoufox #895, open) | **decided** | text metrics, form widget sizes, system colours and emoji widths come from Linux whatever the identity says, and with canvas cover on (any identity that is not the host's OS) canvas readback is new random data on every read, which reads as a privacy-hardened Firefox (the README; Castle on canvas noise, weak). Measured: the Windows identity's canvas changes within a page, the Linux one's does not | `lane_os` makes new lanes claim the host's OS, and is `["linux"]` by default since the owner's decision (section 7): honest Linux identities with real canvas readback. Existing lanes keep their pinned OS, Windows and macOS ones included (changing it is a new device to the site); `lane_os = []` gives Camoufox's mix again |
| Software rendering (no GPU on the node) | **gap** (measured 2026-11-05) | WebGL and canvas pixels come from llvmpipe while the GPU name says Intel or AMD. The red team's draw-speed test sees it on the node VM: an "Intel(R) HD Graphics 400" lane draws its test shader at 54 to 58 ms a frame, a real GPU under 5 ([redteam.md](redteam.md), `env.gpu`) | a GPU node, or Camoufox's `virtual-gpu` mode; not on this VM |
| Windows maths (`javascript.options.use_ucrt_math`) | unknown | the probe's six maths values were the same for Windows, macOS and Linux identities, on arm64 Docker and on the x86 node: either these inputs round alike in glibc and the Windows CRT, or the CRT maths is not in effect | compare with a stock Firefox on Windows; until then it is one more value every lane of a node shares |
| Codecs | ok | H.264, HEVC, AAC, VP9, AV1 and Opus all "probably" for both identities | |
| Timezone, locale and location against the exit IP | ok | the first launch takes them from the exit (geoip) and pins them; later launches compare the exit with the pin and warn on a move (engine); `openajar lane check` compares country and timezone offset daily | |
| Accept-Language against `navigator.languages` | ok | `en-US,en;q=0.9` and `["en-US", "en"]` | |
| The same device on every launch | ok | 6 lanes x 3 launches, no drift (section 3); the pin holds every key Camoufox would redraw | |
| Firefox version | **fixed** | Camoufox 158.0.1 (Oct 29); Firefox stable is 159.0.1 and 160 ships Nov 10. One release behind is ordinary; Akamai's block on the old 135 base shows that a stale base is the risk (#555) | agentosd moves a pin's Firefox version to the installed browser's on the lane's first launch after a Camoufox upgrade (`CamoufoxEngine.bump_firefox`): only the `rv:` and `Firefox/` tokens of the user agent and its header, never the device; the browser must confirm it before the pin is written; the old pin stays as `fingerprint.json.firefox-<old>.bak`, the pin's `firefox_updates` and a `fingerprint_version_bumped` lane event record it, and `openajar lane audit` is quiet afterwards. Never moved back. `openajar lane refresh-fingerprint --keep-device [--version N]` does it by hand (and rolls back) |
| Upgrading Camoufox | watch | Checked 2026-11-04: 158.0.1-beta.41 is the newest build (daijro/camoufox releases; package 0.5.9, the newest on PyPI, pairs with it through its `browser-pin.json`), so there is nothing newer to move to: upstream merged Firefox 159 (#986, Nov 2) but has not released a build of it, and openajar takes only released builds the package pins. #995 (Nov 3, one report, open): hCaptcha detects identities drawn by fpgen, the generator the 0.5.9 package already uses; it touches only new lanes' first draws (a pin replays what it holds) | run the stealth check on a new build before rolling it out; the steps are in DEVELOPING.md, "Upgrading Camoufox" |

### Lanes linked to each other

| Check | Status | Evidence | Fix or why not |
|---|---|---|---|
| The whole device | ok | no two of 6 lanes share user agent, screen, window, cores, GPU, timezone, fonts and voices together (section 3); `openajar lane audit` fails two lanes pinned to one device behind different proxies | |
| Fonts, GPU, canvas noise | ok | each lane's font list, GPU parameters and canvas digest differ (section 3) | |
| Cores claimed against cores measured | **gap** (measured 2026-11-05) | the node VM has 2 vCPUs and the pin says 4; 4 workers run like 1.5 to 2.1 in parallel ([redteam.md](redteam.md), `env.cores`) | pin `hardwareConcurrency` to what the node can run in parallel |
| What every lane on one node shares | gap, inherent | audio output, maths, codecs, media devices before a camera grant, the cores count (the host's, snapped), WebGL pixels and, for Linux identities, text metrics are the same on every lane of a node, as for every user of the same build on the same kind of machine (Camoufox: "one canvas per host" since 156, #860) | they link lanes only together with the rest; spread accounts that must never be linked over several nodes |
| One exit for several lanes | by design | AUD-02, built: lanes of one company share one device (`openajar device`), one pin and one proxy, each lane with its own profile, display and scope; the audit counts a device once and fails a device seen through several proxies, several devices on one exit, and two companies on one device | owner decision AUD-02 (docs/PROXIES.md) |
| TCP/IP fingerprint (p0f) | unknown | a site sees the proxy exit's TCP stack, not the node's: an ISP proxy on Linux under a Windows user agent can disagree (weak, proxy vendor blogs) | ask the proxy provider; Linux identities avoid it |
| TLS (JA3/JA4) and HTTP/2 | ok | Camoufox is Firefox's own NSS; Akamai's #555 showed them identical to stock Firefox. A proxy that intercepts TLS would replace them: the owner's proxies are CONNECT proxies | |

### Network leaks

| Check | Status | Evidence | Fix or why not |
|---|---|---|---|
| WebRTC | ok | no raw address among the ICE candidates (probe); `media.peerconnection.ice.proxy_only` on proxied lanes; `openajar lane check` reads the effective prefs | |
| DNS | ok | a proxied lane resolves no name on the node | |
| IPv6 | ok | a proxied lane opens no socket of its own; WebRTC is proxy-only | |
| A proxy that dies | ok | measured: a refused, a reset and an unresolvable proxy each fail every later fetch and navigation, and the fixture server saw no request around the proxy (`network.proxy.failover_direct` never applies to Playwright's proxy) | `test_a_dead_proxy_never_falls_back_to_a_direct_connection` keeps it so |
| A lane without a proxy reaching the node | ok | 8 fetches to 127.0.0.1 and localhost are all blocked, none reaches the server | |

## 3. Isolation and performance

Measured 2026-11-04 with Camoufox 158.0.1-beta.41, the Python package 0.5.9 and Playwright 1.62, on
two machines:

- **Docker node** (Ubuntu 24.04 arm64 on a Mac, 10 cores, 7.8 GB): 6 scratch lanes (2 Windows, 2 macOS,
  2 Linux identities), 2 relaunches each, then a 6 minute soak with a kill every 45 s
  (`--lanes 6 --relaunches 2 --os windows,macos,linux --soak-minutes 6 --kill-every 45`), and the same
  6 lanes again with 1 relaunch after the font and collision rules were corrected.
- **The node VM** (Ubuntu 24.04 x86_64, 2 vCPU, 3.8 GB, the live test running beside it, so at most
  two browsers of ours and never under 2 GB available at a wake): 2 lanes (macOS, Windows), 2
  relaunches, a 2 minute soak with a kill every 30 s, plus every node test of this branch.

| | Docker, 6 lanes | Node VM, 2 lanes |
|---|---|---|
| First launch (pin made; on the VM with the exit IP lookup) | median 5.8 s, p90 7.7 s | 10.5 and 12.8 s |
| Relaunch (a sleeping lane wakes) | median 2.3 s, p90 2.8 s, max 4.0 s (95 wakes) | median 3.9 s, p90 4.8 s, max 5.4 s (20 wakes) |
| Browser PSS per warm lane on a light page | 458 MB (384 to 663) | 585 MB (495 and 675) |
| Available memory per warm lane (browser, display, viewer, driver) | 496 MB | 624 MB |
| Warm lanes at once, after the node's own use and the 800 MB floor | this node 9; 4 GB 2, 8 GB 10, 16 GB 27 | this node 3; 8 GB 10, 16 GB 23 |
| Soak | 6 min, 201 page loads, 6 kills, 0 failures | 2 min, 35 page loads, 3 kills, 0 failures |
| A killed lane back as the same device | 2.4 to 4.0 s | 4.4 to 5.4 s |
| The other lane during a kill | same browser process, its page loaded, every time | same |
| Descriptors of the check | 10 to 40, following the warm lanes | 10 to 20 |
| Left running afterwards | nothing | nothing |

Run again on 2026-11-04 after the review and the merge with the integration line (section 7), in
the Docker node with the same command and the same versions (Camoufox 158.0.1-beta.41, package
0.5.9, Playwright 1.62): no automation trace, no inconsistency and no drift on any of the 6 lanes,
no collision but what a kind of machine shares, first launch median 4.9 s (p90 7.3 s), relaunch
median 1.9 s (p90 2.1 s, max 2.5 s, 116 wakes), browser PSS 473 MB per warm lane and 557 MB of
available memory per lane, a 6 minute soak with 262 page loads, 7 kills and no failure (each killed
lane back as the same device in 1.9 to 2.5 s, the other lane untouched), descriptors 15 to 40, and
nothing left running. The only warnings are #895's, on the Windows and macOS identities the run
asked for. The Linux lanes' canvas reads the same twice within a page (not covered), and a lane
made with the default `lane_os` pins `canvas_noise: false`. The live view's relay handshake reports
the pinned screen at 1366x768, 2560x1440, 3456x2234 and 7680x4320, through Xvfb's cookie and
x11vnc's unix socket.

Memory is measured on the probe page. A logged-in social feed costs more (the production lane's
main process alone was 473 MB resident on the node), so plan on about 0.8 to 1 GB per warm lane
there. That budget holds the browser, x11vnc, the Playwright driver and the Xvfb framebuffer, which
is the pinned screen at 4 bytes a pixel: 8 MB at 1920x1080, 31 MB for a 3456x2234 Retina screen,
133 MB at the 7680x4320 the display manager allows at most. It is also why `lane_memory_max_mb`
may not be set under 1536 (MemoryHigh, where reclaim starts, is 90% of it): 2 warm lanes on this 3.8 GB VM beside agentosd, about 6 on 8 GB and 13 on 16 GB. Lanes that
sleep cost nothing but disk, and wake in about 4 s, so 29 lanes need not be warm together.

Findings of the runs: no automation trace, no worker, frame or CSS inconsistency, no raw WebRTC
address, no drift in 3 launches of any lane, no two lanes the same device. What lanes shared: audio,
maths, codecs and media devices on every lane; text metrics between lanes of one OS; fonts and the
GPU between the two macOS lanes and between the two Linux lanes (as real machines of the kind do).
The first run flagged Linux fonts (DejaVu, Liberation) on a Windows identity: Camoufox draws them
from real Windows machines, where LibreOffice installs them, so the rule was corrected, not the pin.

| Concern | Status | How |
|---|---|---|
| Profiles never shared or cross-written | ok | one `user_data_dir` per lane under `lanes/<ws>/<name>/profile` (700); a launch first stops any browser already holding the profile; an import refuses a live lock, symlinks out and a profile inside itself (lanes hardening) |
| Cookies, storage, cache, service workers, HSTS, permissions | ok | all live in the Firefox profile, so they are per lane by construction; the disk cache is capped at 256 MB per lane |
| One proxy per lane, failing closed | ok | the proxy is the browser-wide filter; a dead proxy fails requests (section 2); the self-check reads the exit IP inside the lane at every warm and quarantines a lane that leaves from the node's IP |
| Display and live view per lane | ok | one Xvfb and one x11vnc per lane on its own slot; the display is now the lane's pinned screen |
| Downloads and uploads per lane | ok | `lanes/<ws>/<name>/files/outbox` and the per-lane inbox |
| CPU, tasks and memory per lane | **fixed** (and reviewed, section 7) | before: every browser ran in agentosd's cgroup, so a lane with eight content processes had eight times the CPU share of a lane with one, agentosd and every browser shared the service's `TasksMax` (4538 on the node), and nothing capped a runaway lane unless `lane_memory_max_mb` was set. Now every lane's browser runs in its own systemd scope by default (`lane_scopes`): equal `CPUWeight`, `TasksMax` 4096; `lane_cpu_max_pct` adds a CPU cap and `lane_memory_max_mb` a memory cap with `MemoryHigh` at 90% of it (verified on systemd 255 on the node; 1536 MB by default since 2026-11-06, section 7). Default scopes are made only as root and when agentosd's own unit has no memory cap: the hardened unit caps the whole service at 85% and 92%, and browsers in scopes of their own would leave that cap. A browser in a scope outlives an agentosd killed outright until the next start sweeps it (as with a memory cap before) |
| File descriptors | ok | a per-process limit: each lane's processes have their own, and nothing is shared between lanes but the system-wide maximum |
| A crash in one lane | ok | SIGKILL of a lane's browser while another lane loads a page: the other lane finishes with the same browser process, and the killed lane wakes again as the same device (soak) |
| Leftovers | ok | after the check no browser, Xvfb, x11vnc or driver is left (the check fails otherwise) |

## 4. What changed on this branch

- The display is the pinned screen, and the browser window is placed where the pin says
  (`engine_camoufox.place_window`, `display_size_of`, `window_position_of`; lanes use the engine's
  `display_size`). Pins do not change.
- `lane_os` (node.toml): the OS a new lane's identity claims. Empty keeps Camoufox's mix.
- `lane_scopes`, `lane_cpu_max_pct`; `lane_memory_max_mb` adds `MemoryHigh`. Since 2026-11-06 every
  lane has a memory ceiling by default (derived from RAM and max_warm_lanes, `openajar.node.memwatch`;
  0 turns it off), with a watchdog that warns and then closes a lane's browser gracefully.
- `openajar lane audit`, `openajar lane stealth-check`, `openajar.stealth.stealth`,
  `openajar.stealth.stealthcheck`, the probe page in the package, and `tests/node/test_stealth_node.py`.

## 5. For the owner to decide

Decided 2026-11-04: 1 (Linux for new lanes, the default now), 2 (the version moves by itself) and 3
(upgrade now: there is no newer build yet). Section 7 has what was built.

1. **The OS of new lanes.** Windows and macOS identities on a Linux node show Linux text rendering,
   form widgets and colours (#895), and their canvas is covered with noise, the look of a privacy
   browser. Recommendation: `lane_os = ["linux"]` for every lane made from now on; it is honest and
   consistent, and Linux desktops are an ordinary minority on social sites. Existing lanes keep their
   device (a new OS is a new computer to the site). The alternative for Windows-looking lanes is a
   Windows node.
2. **The Firefox version in pins.** A real Firefox updates itself, so its user agent moves every four
   weeks; a pin that keeps an old version on a new engine is the stranger of the two. Recommendation:
   bump the version on the first launch after a Camoufox upgrade, as `refresh-fingerprint
   --keep-device` does by hand today.
3. **Camoufox upgrades.** Pin the build (today 158.0.1-beta.41), and before moving lanes to a new one,
   run `openajar lane stealth-check` and one throwaway account on it (#995 shows a release can get
   worse).
4. **A test proxy.** The check ran without a proxy (no proxy was given): timezone, location and the
   WebRTC address behind a proxy were not exercised end to end here. A throwaway ISP proxy lets
   `--proxy` cover them.
5. **Spreading accounts.** Lanes on one node share host surfaces (audio, text metrics, WebGL pixels).
   Accounts that must never be linked (different owners) belong on different nodes.

## 6. The Docker node

For running the node tests and the check on a laptop:

```
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends xvfb x11vnc fonts-liberation \
    fonts-noto-color-emoji imagemagick rsync curl ca-certificates iproute2 procps python3
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH=/root/.local/bin:$PATH UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --extra node --no-install-project
RUN /opt/venv/bin/python -m playwright install-deps firefox && /opt/venv/bin/python -m camoufox fetch
```

Run with the checkout mounted (`docker run --rm --init --shm-size=2g -v $PWD:/src:ro -e
PYTHONPATH=/src/src <image> /opt/venv/bin/python -m pytest -m node ...`). It is arm64 on Apple
silicon, so host-derived values (maths, WebGL pixels) can differ from an x86 node.

In the Docker node, `test_fingerprint_pin_node.py`'s Linux case fails now and then (about 1 run in
4, also on the commit before this branch). Twice the failing section was the probe's
`mediaDevices` (`enumerateDevices` taking more than 8 s). Not seen on the node; that file and
`test_lanes_node.py` were not run on the node on 2026-11-04, because its available memory stayed
under 2 GB while the live test ran. They are the owner's to run there:
`OPENAJAR_TEST_SLOT=33 scripts/node-test.sh stealth tests/node/test_fingerprint_pin_node.py tests/node/test_lanes_node.py`.

## 7. The review and the owner's decisions (2026-11-04)

Merged with the integration line, where every display has its own Xauthority cookie and x11vnc
listens only on a unix socket:

- `place_window` opens the display with the lane's cookie (the file the browser gets), handed to
  libX11 through `XSetAuthorization` for that one connection, so the environment of agentosd is
  never changed while other threads read it. The node tests' X clients (`xwininfo`, `import`,
  `xdotool`) carry `XAUTHORITY` too. A window that is there but cannot fit at its pinned position
  is reported once, at once (it used to be logged every 0.1 s for 5 s).
- The stealth check reads every node configuration on the machine and refuses a slot that touches
  one (a node with `max_slots = 20` from `:110` made slot 11 kill a live lane's Xvfb and x11vnc), and
  sweeps only its own home's displays. Stopped by SIGTERM or SIGHUP, it closes its lanes; killed
  outright, the next check stops what it left.
- Lane scopes:
  - All lanes' scopes sit in `agentos-lanes.slice` (under `openajar.slice`, beside `system.slice`):
    together they weigh like one unit against agentosd and the system, however many lanes are
    warm, and equally among themselves inside it. Before, each scope had CPUWeight 100 beside
    agentosd in `system.slice`, so the control plane's share shrank as lanes grew.
  - `OOMPolicy=continue`: measured on systemd 255 in the Docker node, a scope's default (`stop`, from
    DefaultOOMPolicy) killed every process of the scope when the kernel killed one for memory; with
    `continue` only the offender dies, and a browser whose main process died is noticed like any
    crash. A systemd that cannot set it on a scope gets the scope without it.
  - Default scopes (no cap asked for) only as root, on cgroup v2, and when no cgroup from
    agentosd's own up to the root has `memory.max` or `memory.high` set; on cgroup v1 or hybrid or
    an unreadable file, none (the browsers stay in agentosd's cgroup, as before scopes).
  - A cap that systemd refuses (busctl denied, as for the hardened unit's unprivileged user) is
    logged once, `memory_status` reports a cap only while it is applied (`lane_limits` says what was
    asked, made and refused), and `openajar doctor` has a `lane_limits` check that reads the warm
    browsers' cgroups (or, with none warm, the service's user).
  - `lane_memory_max_mb` is refused under 1536, except 0.
  - Since 2026-11-06 the default scopes carry a memory cap of 1536 MB (`LANE_MEMORY_DEFAULT_MB`, MemoryHigh
    at 1382 MB, no swap). Ten lanes at that ceiling are 15 GB of a 16 GB node; at the 0.8 to 1 GB they
    really take, about 10 GB. It is also the smallest cap allowed, so nothing lower is on offer, and
    ten warm lanes still need `max_warm_lanes = 10` (the default is 3). The default never asks for a
    scope: where default scopes do not fit (not root, a capped service or slice, cgroup v1) nothing
    changes and the service's own cap holds the browsers. `lane_scopes = false` still means no scopes
    and no cap, `lane_memory_max_mb = 0` keeps the scopes without a cap, and a number still always
    asks for a scope. A lane that reaches the cap has a content process killed (`OOMPolicy=continue`:
    the browser stays up), so the 2.3 GB LinkedIn page seen on the node now loses its tab instead of
    starving the other lanes; the graceful recycle before the limit is still in the backlog.
- A pinned screen larger than Xvfb may be (over 7680x4320) gives a display of the window's own
  size, never the 1920x1080 fallback under a larger window.
- Decisions: `lane_os = ["linux"]` by default, the automatic Firefox version move (section 2), and
  the Camoufox upgrade (none newer than 158.0.1-beta.41 exists on 2026-11-04, so the pin stays; section 2, and DEVELOPING.md "Upgrading Camoufox" for the next one). The check was run again on the merged branch (section 3).
