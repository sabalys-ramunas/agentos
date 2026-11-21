# Any computer as a node: desktop lanes, macOS first

Status: design, 2026-11-04. Not built: today a Mac cannot be a node (`openajar up --here` says so
and points to `openajar dev up --seed`, the fake node, for trying openajar on a Mac). Owner's bet: "Any computer as a node, including your Mac and
Windows. Make a macOS node first-class, with computer-use over the real desktop, native
apps, and the Keychain as a vault backend."

Read with [VISION.md](../VISION.md), [ARCHITECTURE.md](../ARCHITECTURE.md) (section 3,
lanes and isolation), [PRINCIPLES.md](../PRINCIPLES.md) and [PROTOCOL.md](../PROTOCOL.md).
Apple and Microsoft references are numbered like [A3] and [M2] and listed at the end.
Anything marked **(spike)** is not verified and has a spike in phase 0 that settles it
before builders depend on it.

## 1. Summary and recommendation

openajar today has one kind of screen: a browser on a Linux node, under Xvfb, driven
through Playwright. This design adds a second kind, the **desk**: a real macOS (later
Windows) graphical session that an agent drives with computer use, under the same grants,
effect classes, approvals, take over and vault as a browser lane.

Recommendations, in order of weight:

1. **A new lane kind, `desktop`, whose unit is a desk.** A desk is one graphical login
   session (one screen, one cursor). A desktop lane is a desk plus an allowlist of apps
   it may touch. The agent sees an accessibility outline with refs, the same grammar as
   the browser snapshot, built from the macOS Accessibility (AX) tree [A1]. It acts by
   AX actions where an app supports them and by human-paced synthetic input (CGEvent
   [A6]) where it does not. Screenshots and the live view come from ScreenCaptureKit
   [A3], filtered to the lane's own app windows, so the agent never sees anything else
   on that screen.
2. **Three desk hosts, picked by trust and load:**
   - **Dedicated desk** (the default, on a home Mac mini): a separate, non-admin macOS
     user whose session belongs to openajar. Full computer use, its own cursor, real Apple
     hardware. One per Mac unless spike S1 shows more sessions are practical.
   - **VM desk** (for isolation, on the Mac mini): a macOS guest in Apple's
     Virtualization framework [A9]. Strongest isolation, but at most two macOS guests run
     at once on one Mac (licence and framework limit) and the guest reads as a virtual
     machine. For untrusted or throwaway native app work, never for high-trust accounts.
   - **Lent desk** (the daily Mac): the owner's own session, lent for a short, explicit
     time to a named set of app windows. AX actions only by default, no cursor fights,
     paused the instant the owner touches the mouse or keyboard. Light by construction:
     no warm browser lanes, no scheduled work.
3. **One signed native helper app does every privileged macOS thing.** `openajar.app`
   (Swift, Developer ID signed, notarized [A16]) holds the TCC permissions (Accessibility,
   Screen Recording, Input Monitoring), runs per graphical session as a LaunchAgent
   registered with SMAppService [A13], and talks to `agentosd` over a Unix socket. The
   Python daemon stays the brain of the node; the helper is a thin, auditable set of
   hands and eyes. A stable code signature is what makes the TCC grant survive updates,
   which a Python interpreter cannot give us.
4. **The Keychain is a key custodian first, an import source second, and never a live
   read path for agents.** The vault master key is wrapped by a non-exportable Secure
   Enclave key [A20], released by Touch ID on a Mac that has it and by the existing phone
   approval on one that does not. Owner-initiated imports read login-keychain items with
   the system's own per-item consent, and iCloud Keychain passwords come in through
   Credential Exchange on macOS 26 [A22]. No MCP tool reads the Keychain.
5. **Browser lanes run natively on macOS too**, Camoufox under the dedicated desk user,
   with the viewer fed by ScreenCaptureKit window capture instead of Xvfb and x11vnc.
   This is what puts the owner's Meta accounts on a home Mac's real GPU and home IP.
   Until it ships, a Linux VM on the Mac mini running the ordinary Linux node is the
   quick win (residential IP today, real GPU later).
6. **Windows later, same shape:** UI Automation [M1] for the outline, SendInput [M3] for
   input, Windows Graphics Capture [M5] for the view, a Windows service plus a per-session
   agent (services live in session 0 [M7]), Credential Manager and DPAPI [M9, M10] with
   Windows Hello [M11]. Microsoft's own experimental Agent Workspace [M14] may become the
   Windows dedicated desk.

The scariest case is an agent with desktop control on the owner's own Mac. The design
answers it with five hard rules (section 4.9): input only into allowlisted app windows,
checked at every event; a fixed blocklist (openajar's own UI, System Settings, Terminal,
password managers, security prompts) that no grant can lift; approvals that need Touch ID
or the phone, never a click a synthetic event could make; real owner input always wins;
and a kill switch that does not depend on the agent, the controller or the network.

## 2. Goals and non-goals

### Goals

- A Mac (and later a Windows PC) joins the mesh as a node with one command, advertises
  `desktop`, `browser`, `keychain` as it can, and runs lanes the controller routes to.
- Agents use native apps with no web version through a ref-based outline, with a
  screenshot-and-coordinates fallback for apps whose accessibility is poor.
- The same safety story as browsers: grants scope the apps, effect classes route
  approvals, the node enforces every call, take over and hand back, holds and
  quarantine, honest signals, traces with redaction.
- A home Mac mini becomes the trusted node: browser lanes on real hardware and the home
  IP, native apps, and the vault key in its Secure Enclave.
- The daily Mac is never overloaded: lending a screen costs one helper process, a few
  percent of one core, and only while lent.
- Secrets reach native app fields through the guarded typist, bound to an app's code
  identity the way browser secrets are bound to a domain. The model never sees them.
- First-time permission setup is a five minute, once-per-Mac step with a checklist the
  node verifies itself.

### Non-goals

- **Not remote desktop software.** The viewer shows lane windows to the owner; it is not
  a general way to reach the Mac (use Apple's Screen Sharing for that).
- **Not a way around app licences or Apple's limits.** No more than two macOS guests per
  Mac [A10], no private APIs (for example no private virtual display API), no disabling
  SIP, no TCC database edits.
- **No MDM requirement.** MDM can pre-approve Accessibility but not Screen Recording
  [A17, A18]; supporting it is a later option for agencies, not the path for one owner.
- **No agent access to the owner's general desktop.** Lending is per app window and time
  boxed; there is no "control my whole Mac" mode.
- **No unattended work on the daily Mac.** Schedules, flows and brains are refused on a
  node in the `daily` profile.
- **Not iOS.** iPhone and iPad apps that run on Apple silicon Macs [A24] are covered as
  ordinary native apps on a desk; driving a physical iPhone is out of scope.
- **Windows is designed, not built**, in this round.

## 3. The owner's stories

### 3.1 A home Mac mini as the trusted node for Meta accounts

The owner buys (or already has) an Apple silicon Mac mini, plugs it into the home router
by Ethernet and leaves it on a shelf. One install creates a macOS user `agentos-desk1`
(non-admin), installs `openajar.app` and `agentosd`, and joins the mesh. The owner sits at
it once, with a keyboard or over Apple's Screen Sharing, ticks the helper under Privacy &
Security for Accessibility and Screen Recording, and `openajar doctor` on the mini goes
green.

The Meta lanes (`biz-a/facebook`, `biz-a/instagram`, `personal/facebook`) are browser
lanes on the mini: Camoufox under `agentos-desk1`, each with its own profile and pinned
fingerprint, with a macOS fingerprint that matches the real machine class (Apple GPU,
macOS fonts) instead of a Linux box pretending. The personal lane uses the home IP; the
business lanes keep their ISP proxies. The owner logs in once per lane through the viewer
link exactly as today. Posts are drafted by the agent in Claude Code on the daily Mac or
by a brain on the mini, approved on the phone, posted on the mini, with readback.

What is different from a Linux node: real WebGL and fonts, a home connection, and the
vault key sealed in the mini's Secure Enclave, so a copied disk image is useless. After a
power cut the mini waits at the FileVault screen until unlocked (section 4.5); a small UPS
and macOS 26's SSH unlock make that rare and quick.

### 3.2 A native app with no web version

One business keeps its books in a Mac desktop accounting app that has no web version
(any AppKit or Catalyst app with decent accessibility fits this story; the owner names
the real one in open question 4). The owner wants an agent to enter last week's
supplier invoices from the files inbox and export a VAT report.

The owner creates `biz-b/books` as a desktop lane on the mini's desk with the app's
bundle id and team id in its allowlist. The agent calls `lane_open`, then
`desktop_snapshot` and gets:

```
- app "Books" [pid=812] [frontmost]
  - window "Biz B: Invoices" [ref=w1] [focused] [box=0,25,1440,875]
    - toolbar
      - button "New Invoice" [ref=w1e3] [id=newInvoice]
      - searchbox "Search" [ref=w1e4]
    - table "Invoices" [rows=214]
      - row "INV-1043 Supplier X 120.00" [ref=w1e9]
```

It clicks by ref, fills fields with human rhythm, attaches a PDF from the lane's inbox
through the app's open panel, and saves. Saving an invoice is `draft` under the app's
profile; "Submit VAT return" is marked `irreversible` in the app profile, so it waits for
the owner's approval with a screenshot of the window as it stands. The vault types the
app's own login password into its secure field if the app asks, bound to the app's team
id. When a dialog the agent does not understand appears, the lane reports a signal and
the owner takes over in the viewer.

### 3.3 The daily Mac lends a screen without being overloaded

The owner is working on the MacBook. An agent needs five minutes in an app that exists
only on that Mac (say a licensed design tool or the owner's local Keynote deck with its
fonts). It calls `desktop_lend_request(app="Keynote", minutes=10, reason=...)`. The
menu-bar app shows the request; the owner clicks Lend and confirms with Touch ID.

For ten minutes the agent sees only Keynote's windows. It acts through AX actions
(press, set value, pick menu item), which do not move the owner's cursor, so the owner
keeps typing in another app. If the agent needs the pointer (a drag on a canvas), it asks
for "drive"; the menu bar shows it, and the agent gets the pointer only while the owner
has not touched the mouse or keyboard for a few seconds. The moment the owner moves the
mouse, the agent pauses. A coloured border around the lent windows and the menu-bar icon
show that an agent is acting. When the time is up, or the owner clicks Stop or presses the
stop chord, the lend grant is revoked and the helper lets go.

Load stays small: the helper captures frames only while someone watches or a screenshot
is asked for, runs at utility QoS, holds no browser, and refuses to start a lend on
battery below 20 percent, in Low Power Mode, or at serious thermal state [A15]. Anything
heavy is offered to the mini instead: "this app is also on mini-1; run it there?"

## 4. Architecture

### 4.1 Where things run on a Mac

```
+------------------------------- Mac mini (profile: dedicated) ----------------------------+
|  launchd (system)                                                                        |
|    agentosd  (LaunchDaemon, user _openajar, no GUI)                                       |
|      lane manager, grants, vault (key wrapped by the Secure Enclave), channel, runner    |
|      egress forwarders, pf anchor (spike S6), viewer relay, MCP                          |
|        |  Unix socket per desk, peer checked (uid + code signature)                      |
|        v                                                                                 |
|  GUI session of agentos-desk1 (non-admin, auto-start rules in 4.5)                       |
|    openajar.app in desk mode (LaunchAgent via SMAppService, TCC: AX, Screen, Input Mon.)  |
|      AX outline + actions | ScreenCaptureKit capture | CGEvent input | RFB over socket   |
|      launches lane apps and Camoufox as agentos-desk1                                    |
|    lane apps: Books.app, Camoufox (biz-a/instagram), Camoufox (personal/facebook) ...    |
|                                                                                          |
|  VM desks (optional, at most 2 macOS guests): Virtualization.framework, run by agentosd  |
|    inside each guest: openajar.app in desk mode, reached over a virtio socket             |
+------------------------------------------------------------------------------------------+

+------------------------------- MacBook (profile: daily) ---------------------------------+
|  openajar.app in owner mode (menu bar): approvals with Touch ID, status, lend, offload     |
|    holds the lend session; no browser lanes, no schedules                                |
|  agentosd (LaunchAgent in the owner's session, only when desktop-lend is enabled)        |
|  openajar connect tunnels (today's client, unchanged)                                     |
+------------------------------------------------------------------------------------------+
```

Why two processes and two languages. The daemon is Python and stays Python (the stack
decision holds). But every API that matters here (AX, ScreenCaptureKit, CGEvent,
LocalAuthentication, Secure Enclave keys, SMAppService) is a Swift or Objective-C API,
and TCC attributes permissions to the "responsible" code identity. A Python process
asking for Accessibility gets attributed to `python3` or to Terminal, loses the grant when
the interpreter moves, and would grant every Python script on the Mac the same power. A
small signed app has one stable designated requirement (team id plus bundle id), so the
grant survives updates and belongs to exactly that code. The helper keeps no policy: it
executes, checks a few invariants on every event (section 4.9), and reports.

Why the daemon runs as `_openajar` and not as the desk user on the mini. Lane apps
(including a browser rendering hostile pages) run as `agentos-desk1`. If agentosd ran as
that user too, a compromised lane app could read the vault database, grants and other
lanes' state. A separate service user puts the main secrets behind a uid boundary, the
closest macOS analogue to the Linux node's split between agentosd and bubblewrapped lanes.
On the daily Mac there is no such split to make (it is the owner's own session), which is
one more reason lend mode is narrow.

### 4.2 The desktop lane and its computer-use model

**Lane shape.** `LaneKind.DESKTOP` joins browser, terminal, service and android. A
desktop lane is pinned to one node and one desk and carries:

```python
class AppRef(BaseModel):
    bundle_id: str               # "com.example.books"
    team_id: str                 # code signing team; checked on every launch and every event
    name: str                    # shown to people
    profile: str | None = None   # app profile ref: effect rules, known dialogs, stable ids
    version: str | None = None   # optional pin; a mismatch is a signal, not a silent change

class DesktopSpec(BaseModel):
    host: Literal["session", "vm", "lend"]
    desk: str                    # "desk1" (session), "vm1", or "owner" (lend)
    apps: list[AppRef]           # the allowlist; empty is refused
    input: Literal["ax", "ax+pointer"] = "ax+pointer"   # lend forces "ax" unless driving
    display: tuple[int, int] | None = None              # desired size for session and vm desks
```

`LaneSpec` gains `desktop: DesktopSpec | None`. Lifecycle is the existing one: cold (apps
not running, state on disk), warm (apps running, idle), active (a tool call holds the
lane), sleeping (node asleep or offline), quarantined.

**One desk, one cursor.** On a bare-metal Mac, all apps in one graphical session share
one pointer and one keyboard focus. So the desk, not the lane, is the unit of pointer
input: the node keeps a per-desk **input lock**. AX actions (press, set value, select
menu item, raise window) do not need the pointer and can run for several lanes of one
desk concurrently; pointer and keyboard input take the desk's lock for the duration of
one atomic step. Default and recommendation: one desktop lane per desk; several only
when they are AX-only. Browser lanes on the same desk do not take the lock at all,
because Playwright delivers their input inside the browser, not through the OS.

**The snapshot: the AX tree as an outline with refs.** The helper walks the AX tree of
the lane's allowed apps [A1, A2] and agentosd renders it in the browser snapshot grammar
(`src/openajar/node/snapshot.py`), so agents and flows reuse what they know:

- One `app` line per allowed app, then its windows, then the element tree. The menu bar
  is listed only when asked (`scope="menu"`), because walking menus is slow.
- AX roles map to the browser's role words: `AXButton` to `button`, `AXTextField` to
  `textbox`, `AXSecureTextField` to `textbox [secure]`, `AXCheckBox` to `checkbox`,
  `AXRadioButton` to `radio`, `AXPopUpButton` and `AXComboBox` to `combobox`,
  `AXMenuItem` to `menuitem`, `AXLink` to `link`, `AXTable`, `AXRow`, `AXCell`,
  `AXOutline` to `table`, `row`, `cell`, `treeitem`, `AXSlider` to `slider`,
  `AXStaticText` to `text`. Unknown roles keep their AX name.
- Refs are `w<window>e<n>` (window-scoped) and `m<n>` for menu items. Like browser refs
  they are valid until the next snapshot of that window; the helper holds the
  `AXUIElement` handles, so a stale ref matches nothing and returns `ref_not_found`.
  `REF_RE` in snapshot.py widens from `(?:f\d+)?e\d+` to `(?:[fw]\d+)?e\d+|m\d+`.
- `AXIdentifier` (developer-set, not localized) is shown as `[id=...]`. App profiles and
  flows key on it, the same "structure, never localized text" rule the vault uses.
- `[box=x,y,w,h]` comes from `AXPosition` and `AXSize`, in desk coordinates.
- Secure text fields never have their value read. Text field values are shown only for
  plain `AXTextField` elements and pass through `vault.scrub()` like browser snapshots.
- Size budget, viewport focus and the hint rules work as in the browser outline. Hints for
  unnamed buttons come from `AXDescription`, `AXHelp` and `AXIdentifier`.
- Apps built on Electron or Chromium expose their tree only after the client sets
  `AXManualAccessibility` (Electron) or `AXEnhancedUserInterface` on the app element [A4];
  the helper does that for allowlisted apps only, and notes it in the trace.

**The fallback: screenshot and coordinates.** Some apps draw their own controls (canvas
editors, games, some cross-platform toolkits) and expose an empty or useless tree. For
those the agent uses `desktop_screenshot` (the lane's windows only) and
`desktop_act(point=[x, y], window="w1")`. Points are window-relative and refused outside
an allowed window. This is the classic computer-use loop, and the outline marks such
windows `[ax=poor]` so the agent knows to switch.

**Acting.** `desktop_act` takes a ref or a point and an action. `ActionKind` gains
`double_click`, `right_click`, `drag` (ref or point to ref or point), `chord` (keys with
modifiers, for example `cmd+s`), and `menu` (a menu path such as `File > Export > PDF`,
resolved by `AXIdentifier` where present, by title only as a last resort). For a ref the
helper prefers the AX action (`AXPress`, `AXConfirm`, `AXPick`, setting `AXValue` for
non-secret text, `AXRaise`) because it does not move the owner's pointer; pointer input is
used when the app ignores the AX action, when the lane is in `ax+pointer` mode and the
step needs it, or for points.

**Human-like pacing.** The owner's rule for real accounts holds for desktops: no
teleporting pointer, wheel scrolling, a look-before-you-act pause, a random point inside
the target, a real press duration, typing one character at a time with uneven rhythm.
To keep one source of truth, agentosd computes the input plan in Python (the same
generators as `src/openajar/node/human.py`: a curved path with timings, wheel steps,
key timings), and the helper replays it as CGEvents [A6, A7] with the given delays,
re-checking the target at each step. Native apps rarely profile input the way Meta does,
so pacing on desks can be faster by profile, but never instantaneous.

**Signals.** Deterministic, never a model reading the screen: `ok`, `blocked` with detail
`foreign_window` (a window from a non-allowed app is frontmost or modal over the lane),
`blocked:secure_input` (another process holds Secure Event Input), `logged_out` and
`checkpoint` from app profile markers (a known login window id), `unknown` otherwise. A
quarantine follows lost permissions (`AXIsProcessTrusted` false [A5], capture refused),
an app whose team id does not match, or an app version outside its pin.

### 4.3 Permissions (TCC) and getting them granted once

What the helper needs, per Mac:

| Permission (System Settings name) | Why | API check | Notes |
|---|---|---|---|
| Accessibility | read the AX tree, AX actions, post synthetic input | `AXIsProcessTrustedWithOptions` [A5] | MDM can pre-approve it through PPPC [A17, A18] |
| Screen & System Audio Recording | ScreenCaptureKit window capture for screenshots and the viewer | `CGPreflightScreenCaptureAccess` [A8] | MDM cannot grant it [A18, A19]; macOS 15 and later re-ask periodically for apps that capture without the system picker [A23] |
| Input Monitoring | an event tap that tells the owner's real input from ours (lend mode, kill chord) | `CGPreflightListenEventAccess` [A7] | only on the daily Mac and for the kill chord |
| Posting events | synthetic input | `CGPreflightPostEventAccess` [A7] | covered by Accessibility on current macOS (spike S3 confirms) |

How the grant is obtained once:

1. The installer places `openajar.app` in `/Applications` and registers its LaunchAgent
   with SMAppService [A13] (the owner sees one "Background item added" notice).
2. The app opens to a setup checklist. Each row has an "Open the setting" button that
   deep-links to the right Privacy & Security pane and a live tick that turns green when
   the API check passes. Nothing is granted by the program; the owner flips each switch.
3. The owner does this at the Mac with a real keyboard and mouse, or through Apple's
   Screen Sharing. **(spike S3)** confirms that Screen Sharing input is accepted in the
   permission panes and prompts; macOS filters synthetic input from some security
   prompts [R1], which is exactly why a helper cannot click its own permission.
4. `openajar doctor` on the node checks all of them and states the fix in one sentence.
5. The grant belongs to the signed code identity, so updates keep it as long as the team
   id and bundle id stay the same. Whether a grant made in one user's session covers the
   desk users too, or must be made once per user, is **(spike S3)**; the checklist handles
   either.

**The periodic Screen Recording prompt.** On macOS 15 and later, an app that captures
the screen without the system's content-sharing picker [A3] is asked again from time to
time (monthly in the shipped releases, per reports [A23]). On a shelf Mac nobody sees that
prompt. Three answers, in order: apply for Apple's persistent content capture entitlement
[A11], which exists for remote viewing apps and fits the viewer honestly; if it is
refused, keep capture-dependent features (viewer, screenshots) degraded with a clear
`needs_you` item ("approve screen recording on mini-1") while AX-only work continues;
never work around it with clock tricks or prompt-suppressing utilities. **(spike S3)**
measures what happens to capture while the prompt is open and unanswered.

### 4.4 Isolation on macOS: desks, users, VMs

macOS has no network namespaces, overlayfs or cgroups, so the four dials map differently
and the presets must say so honestly:

| Dial | Linux node today | Dedicated desk (separate macOS user) | VM desk (Virtualization.framework) |
|---|---|---|---|
| processes | PID namespace, cgroup | separate uid; the daemon kills by uid; no hard caps | own kernel, fixed vCPUs |
| filesystem | overlay on the base image | the user's home (0700); APFS clones for lane snapshots and forks | the VM's disk image, an APFS clone of a base image |
| network | netns plus forwarder | per-uid pf rules sending the desk user's traffic only to its forwarder **(spike S6)**; app proxy settings as a second layer | NAT device on the host, forwarder in front |
| programs | base image plus overlay | `/Applications` is base; `~/Applications` of the desk user is overlay | per VM |
| limits | cgroup v2 | a watchdog on resident memory (`proc_pid_rusage`), background QoS for lane apps, kill on breach | fixed RAM per VM |

Trade-offs between the three hosts:

| | Dedicated desk | VM desk | Lent desk |
|---|---|---|---|
| Isolation from the owner | strong (another user, no admin) | strongest (another kernel) | none: it is the owner's session |
| Isolation between lanes | weak inside one desk (same uid, separate profiles; browser content processes are sandboxed by the engine itself) | per VM | none |
| Looks like a real Mac to apps and sites | yes, it is one | no: the guest's hardware model reads as a virtual Mac and the GPU as paravirtual (`VZMacGraphicsDeviceConfiguration` [A9]) | yes |
| Cost | one graphical session, about 1 to 2 GB | 4 to 8 GB RAM and a disk image per guest; boot in tens of seconds **(spike S2)** | almost nothing |
| How many | one per Mac for sure; more only if S1 finds a supported way | at most two macOS guests running at once [A10] | one lend at a time |
| Apple ID inside | optional, only if an app needs it | supported with limits on recent macOS [A12] | the owner's |
| Best for | trusted accounts, native apps on real hardware, the Meta lanes | untrusted apps, throwaway work, a second concurrent desk | short, light help on the daily Mac |

How many desks a Mac mini can host is the main unknown. macOS shows one console session;
fast user switching keeps other sessions alive in the background, but whether
ScreenCaptureKit and synthetic input work in a switched-out session is doubtful
**(spike S1)**. Apple's Screen Sharing can open a session for a second user beside the
console user, with its own virtual display **(spike S1)**; if the helper works there, a
mini could host several bare-metal desks. Until S1 says yes: one dedicated desk per Mac,
plus up to two VM desks.

Two further notes. A desk needs an unlocked graphical session; a headless Mac mini also
needs a display to render at a fixed size, either a display or an HDMI dummy adapter,
unless S1 shows the headless default is usable. And the Linux answer to "fully isolated
Linux lanes on a Mac" (ARCHITECTURE.md section 12) falls out of the VM work: a Linux guest
in Virtualization.framework runs the ordinary Linux node, dials out on its own, and gets
every Linux preset. That is the phase 1b quick win.

### 4.5 The node daemon on macOS: launchd, the menu bar, sleep and power

**Processes and launchd.** On a dedicated Mac: `agentosd` is a LaunchDaemon (system
domain, user `_openajar`, `KeepAlive`), so it runs before anyone logs in and owns the
channel, the vault and the grants. The helper is a LaunchAgent in each desk user's
session, `LimitLoadToSessionType = Aqua`, registered with SMAppService [A13, A14]; it
connects to the daemon's socket for its desk and refuses to start in any other user's
session unless that session is a configured desk or the owner's lend session. On the
daily Mac there is no LaunchDaemon: `agentosd` (only when desktop-lend is enabled) and
the menu-bar app run as LaunchAgents in the owner's session.

**The menu-bar presence.** One app, three faces:
- *Owner mode* (daily Mac): approvals with Touch ID, node status, Lend and Stop, offload
  this session, the kill chord. This is the backlog's "Mac menu-bar app" item.
- *Desk mode* (desk users and VM guests): a small status item and the lane border, plus
  the kill chord for someone sitting at the mini.
- *Setup*: the permission checklist (4.3).

**Sleep and power.**

| Situation | Dedicated Mac mini | Daily MacBook |
|---|---|---|
| Idle | holds an `IOPMAssertion` that prevents idle system sleep [A15] while any lane is warm or a schedule is due within 10 minutes; lets the display sleep | never prevents sleep for agent work |
| Asleep | node `sleeping`, pinned lanes `sleeping`, schedules wait and alert (no silent failover, as today) | the lend ends at once; nothing is pinned here |
| Lid closed | n/a | the node goes offline; lends end |
| Battery, Low Power Mode, thermal | n/a (on mains; a UPS is recommended) | lend refused on battery below 20 percent, in Low Power Mode, or at serious thermal state; an active lend pauses |
| App Nap | the helper opts out with `ProcessInfo.beginActivity` [A15]; lane apps in a desk get App Nap turned off per app by the desk setup | the helper opts out only while lent |
| Power loss | "start up automatically after a power failure" on; then FileVault (below) | n/a |

**FileVault and restarts.** With FileVault on, a Mac boots to an unlock screen and no
session (no desk) starts until someone unlocks it; automatic login is not available with
FileVault. On Apple silicon with macOS 26, Remote Login can unlock FileVault over SSH at
that screen [R2], which works over Ethernet (and Wi-Fi on later releases, per reports)
from the home network; the mesh is not up yet at that point. Planned restarts (updates)
use `fdesetup authrestart`. Then the desk user's session must start: with FileVault on,
the unlock user is logged in, so the cleanest setup makes the desk user the one that
unlocks, or the owner logs the desk user in once over Screen Sharing. Open question 2
picks the default.

**Node profile.** A node advertises a profile at enrollment and in `config`:
`dedicated` (desks, browser lanes, schedules, brains) or `daily` (only lend, no
schedules, no brains, no warm browser lanes, `max_warm_lanes = 0`). The controller routes
unpinned work only to `dedicated` nodes, and the node refuses work its profile forbids
even when asked directly.

### 4.6 Browser lanes on macOS

Camoufox ships macOS builds, arm64 only going forward [R3]. On a Mac:

- **Where it runs.** Headful, in the dedicated desk user's session, launched by the helper
  as that user. Headless Firefox is a tell on real accounts and there is no Xvfb on macOS.
  Each browser lane keeps its own profile directory and pinned fingerprint under the
  daemon's lane store, shared with the desk user through a per-lane directory the desk
  user owns (0700), never the daemon's whole home.
- **How agentosd drives it.** The daemon cannot be the parent of a process in another
  user's session, so the helper starts Camoufox in remote server mode and hands the
  daemon a per-launch endpoint with a random secret path; the daemon connects Playwright
  to it. The endpoint is loopback only and the pf anchor allows only `_openajar` to reach
  it **(spike S5)**. If that proves fragile, the fallback is a small lane runner process
  in the desk session that owns Playwright and exposes the `BrowserSession` contract over
  the desk socket.
- **Fingerprint.** On a real Mac the pinned fingerprint should be a macOS one that agrees
  with the hardware (Apple GPU renderer, macOS fonts, screen size of the desk display),
  because the rendering is real. **(spike S5)** checks coherence on the mini against the
  existing self-check and a fingerprint test page, on a throwaway account first.
- **Network.** Per-lane proxy in the engine as today; the per-uid pf rule makes "no
  proxy, no traffic" hold even if the engine leaks. The self-check (exit IP, STUN, DNS,
  timezone) runs unchanged. The personal lane may run with no proxy on the home IP.
- **Input.** Playwright input as today (no OS pointer, no desk input lock), with the
  Camoufox humanize cursor and `human.py`.
- **The viewer.** The helper captures that one Camoufox window with ScreenCaptureKit
  (`SCContentFilter` for a single window [A3]; it captures the window even when other
  windows cover it) and serves it as RFB over the desk Unix socket. `vnc_relay.py`
  connects to a Unix socket instead of x11vnc's TCP port, so noVNC, signed links, view
  and control modes and the events stream stay as they are, and no TCP viewer port
  exists at all (the SEC-1 lesson). Take-over input for a browser lane goes through the
  engine (Playwright mouse and keyboard), not the OS, so it never needs the desk lock.
- **Windows of a lane can overlap.** Capture is per window and input is in-browser, so
  several browser lanes share a desk without a window manager. The desk display size
  bounds the largest window; pinned window sizes come from the fingerprint as today.

### 4.7 The Keychain as a vault backend

What macOS allows, plainly [A19, A20, A21, A25]:

- An app can create and read its own Keychain items freely. In the data protection
  keychain (the modern one), items are readable only by apps in the same keychain access
  group, which is tied to the signing team. openajar cannot read other vendors' items there.
- Items in the file-based login keychain carry per-item access lists. Reading another
  app's item makes macOS ask the user, in a system dialog, to allow it once or always,
  with the login password. That dialog is a security prompt an agent cannot click
  through.
- Passwords saved in the Passwords app and iCloud Keychain are not readable by third
  party apps. On macOS 26 they can be moved app to app with Credential Exchange, a
  system-brokered, user-initiated export and import [A22] (ARCHITECTURE.md section 6
  already plans this route).
- A Secure Enclave key never leaves the chip; it can decrypt only on that Mac, gated by an
  access control (user presence, current biometry set, or none) [A20, A21].

The design uses these in three ways:

1. **Key custody (phase 4, the default on Macs).** A new `KeyCustody` backend:
   `keychain-se`. The vault's master key (today `secrets/vault.key` or a passphrase,
   `src/openajar/vault/crypto.py`) is wrapped with a Secure Enclave P-256 key using ECIES
   (`SecKeyCreateEncryptedData` [A20]); the wrapped blob lives in the vault meta, the key
   handle in the helper's keychain. Unwrap needs the helper, so the helper is the only
   process that can unseal; it returns the master key to agentosd over the peer-checked
   socket, and agentosd keeps it in RAM as today. Two access policies:
   - *Node unseal* (the mini at boot): no biometry, `AfterFirstUnlockThisDeviceOnly`.
     A stolen disk or a copied vault.db is useless elsewhere; a running, unlocked mini
     can unseal without a person, which a node that works unattended must.
   - *Crown-jewel release* (subscription sessions, `pay` cards, chosen entries): a second
     SE key with `.biometryCurrentSet` or `.userPresence` [A21], wrapping those entries'
     data keys. Released by Touch ID on the daily Mac (LocalAuthentication [A26]) or on a
     mini with a Touch ID keyboard; otherwise by phone approval, which then unlocks a
     short-lived release on the node.
2. **Import (phase 4).** In the menu-bar app (never over MCP), the owner picks entries to
   import: login keychain items (the system asks per item), Credential Exchange from the
   Passwords app or another manager, or the existing Vaultwarden importer. Each imported
   entry is bound to a domain (browser) or an app (desktop) and lanes, exactly as vault
   entries are today. After import the vault is the source of truth; the Keychain item is
   not read again.
3. **No live read-through.** There is no `keychain.get` tool, no "use whatever is in my
   keychain" mode, and the agent never learns what the Keychain holds beyond the entry
   labels the owner chose to import.

**Secrets into native apps.** The vault's No-Body-Injection rule
(`src/openajar/vault/policy.py`) gains three sinks: `app_username`, `app_password`,
`app_otp`. An entry is bound to an app by bundle id plus team id (the analogue of a
registrable domain); a field qualifies only if it is in a window of that app, the app's
running code passes its designated requirement check (team id), the element is
`AXSecureTextField` for passwords (an `AXTextField` with a matching `AXIdentifier` or
content type for usernames and codes), enabled and focused. The desktop typist adapter
implements the existing `Typist` contract (`src/openajar/vault/typist.py`): the helper
receives the secret as a binary frame, focuses the field, and before every keystroke
re-checks the frontmost app's pid and team id, the focused element and its subrole; any
change aborts with `TypistAborted`. Keys are sent as CGEvents carrying the character
[A6], never `AXSetValue` (which would bypass the per-keystroke checks), never logged,
screenshotted or returned. Whether synthetic keystrokes reach a field while Secure Event
Input is on is **(spike S4)**.

### 4.8 Take over on a real desktop

Two different situations:

- **A lane's screen** (dedicated or VM desk): the existing model, unchanged. A signed
  view or control link opens the viewer; in control mode with control `HUMAN`, viewer
  input goes to the helper, which posts it as CGEvents into the desk (input lock taken by
  the human, agents wait at `using()`); hand back as today. `note_human_input` feeds idle
  hand-back. Someone physically at the mini is detected the same way as on the daily Mac
  (real input on the desk) and takes control automatically.
- **The owner's own screen** (lend): there is nothing to take over remotely; the owner is
  the screen. The helper's event tap separates real input (from the HID system) from the
  events it posts itself (it tags its own events with a private source and checks the
  poster's pid field) [A7]. Any real input on the lent desk moves control to `HUMAN` at
  once; the agent resumes only after the owner hands back from the menu bar, or after
  the idle hand-back delay if the owner enabled it for this lend. Watching a lend
  remotely (from the phone) is view-only and shows only the lent windows.

### 4.9 Security model

**Threats, worst first.**

| Threat | Example | Blast radius | Mitigation |
|---|---|---|---|
| A hijacked agent drives the owner's own Mac | injected text tells the agent to open Terminal, or Mail, or the openajar panel and approve itself | everything the owner can do | lend is opt-in, per app window, time boxed, Touch ID to start; the fixed blocklist; input only into allowlisted windows checked per event; approvals never clickable; real input wins; kill chord |
| The agent approves its own request | clicks Approve in the panel or the menu bar, or "Always Allow" in a Keychain prompt | every gate | approvals on a Mac need Touch ID or the phone (biometry cannot be synthesized); openajar's own windows and every system security prompt are on the blocklist; capture excludes openajar's own windows |
| Reading secrets off the screen | the agent opens Passwords or a password manager in a lent or desk session | the owner's passwords | blocklist; capture is per allowed window; secure fields never read; desk users have no password manager |
| A lane app escapes on the mini | a hostile page exploits the browser, then reads other lanes' profiles | lanes of that desk | engine sandbox for content processes; desk user has no admin, cannot read `_openajar`; pf limits its network to its forwarders; VM desks for untrusted apps |
| Foreign dialog steals focus | a system alert or another app's modal appears between check and click | the click lands in the wrong place | the helper checks the window under the point and the frontmost pid immediately before each event and refuses (`blocked:foreign_window`); AX actions target a specific element, not a location |
| The helper is compromised or replaced | a different binary claims to be the helper | full desk control | daemon checks the peer's code signature (audit token, designated requirement) on every socket connection; the helper checks the daemon's uid; helper has no network access and no listener other than its socket |
| TCC grant abused by something else | another process asks the helper to act | desk control | the socket is in a 0700 directory, peer uid and signature checked; the helper accepts commands only from agentosd |
| Unattended work on the daily Mac | a schedule fires at 3 am on the laptop | the owner's machine while away | `daily` profile refuses schedules, flows and brains; lend grants cannot be used by a run |

**The hard rules, enforced in the helper (the last line) and in the daemon (the policy):**

1. **App allowlist, every event.** Input goes only to windows owned by a process whose
   bundle id and team id are in the lane's allowlist, checked at the moment of each event,
   for AX actions and pointer and keyboard alike. Launching an app outside the list is
   refused. Open and save panels belong to the app and are allowed; the files they reach
   on a desk are the desk user's.
2. **The fixed blocklist, no grant can lift it:** openajar.app and the panel in any browser,
   System Settings, Terminal and other terminal emulators, Script Editor, Automator,
   Shortcuts, Keychain Access, Passwords, known password managers, Activity Monitor,
   Installer, the login window and screen lock, and every system security prompt and
   authorization dialog. On a lend, also Finder and the owner's browsers.
3. **Approvals cannot be made by synthetic input.** On a Mac, an approval is Touch ID
   (LocalAuthentication with a biometric policy, no password fallback for agent-reachable
   flows [A26]) or the phone. The existing rule stands: no link approves anything.
4. **Real input wins.** On the daily Mac, any real keyboard or mouse input pauses the
   agent at once. On a desk, a person at the machine takes control the same way.
5. **The kill switch does not depend on the agent.** Tiers: stop one lane (today's
   `stop_agent`); the menu-bar Stop and the kill chord on that Mac (the helper drops all
   input, ends lends, pauses every desk lane, and the daemon revokes every grant family
   with a desk lane in it); the phone break-glass from the backlog (node or every node);
   and the helper's dead-man (it stops posting input if the daemon's heartbeat is silent
   for 5 seconds). Last resort, documented: turn the helper off under Privacy & Security.

**Grants, effects and approvals for desktops.**

- New special capabilities, off by default like `exec`: `desktop` (act in a dedicated or
  VM desk lane) and `desktop:lend` (act in the owner's session). A `desktop:lend` grant is
  minted only by the owner at lend time, lives at most the lend length (default 10
  minutes, ceiling 60), cannot be narrowed into a schedule, flow or brain, and dies when
  the lend ends.
- Effects: `desktop_snapshot`, `desktop_screenshot` and `desktop_apps` are `read`, and
  their output is tool-untrusted (tainted) like page text. `desktop_act` is `draft` in a
  dedicated or VM desk, raised by the app profile: controls marked `commit`, `pay` or
  `irreversible` by `AXIdentifier` or menu path, and chords the profile lists (for
  example a send shortcut), take that effect. In a lend, every `desktop_act` is `commit`
  and the lend itself is the standing approval for the named apps and length, which the
  owner gave with Touch ID.
- Honest limit: a browser's commit gate (AUD-03) can read the real publish request; a
  native app commits through its own network code, which the node cannot parse. So for
  native apps the effect comes from the app profile and the lane's ceiling, not from the
  wire. An app without a profile gets a conservative default: its lane's ceiling is
  `draft`, and any act on a control whose role is `button` in a sheet or dialog asks for
  approval, until the owner writes or accepts a profile.
- Approval content is node-authored as today: the summary names the app, window and
  control from the AX tree (never text the model wrote) and attaches a screenshot of the
  lane's window as it stands, bound to the content hash.
- Trifecta: a desk holds private data, untrusted input (whatever the app shows) and
  network (the app's own). Dedicated and VM desks are tier 0 only because the allowlist,
  the pf egress and the taint rules contain them. A lend is tier 2 by construction; it
  is allowed only because it is short, owner-started, Touch ID confirmed and scoped to a
  few windows, and it refuses to start in a grant family that has already read untrusted
  content and private data together, unless the owner co-signs.

**Failure modes.**

| Failure | What happens |
|---|---|
| Helper crashes | desk lanes go `unknown` and agents get `unavailable`; launchd restarts it; in-flight step fails, never half-retried; the dead-man already stopped input |
| TCC permission removed mid-run | `AXIsProcessTrusted` false or capture refused: lanes of that desk quarantined with reason `permission_lost`, one `needs_you` item |
| Periodic screen recording prompt pending | viewer and screenshots degraded, AX work continues if the app profile allows, `needs_you` item |
| App updated, AX tree changed | flows keyed on `AXIdentifier` fail loudly at the missing id; version pin mismatch is a signal; nothing guesses |
| Foreign window or system alert | `blocked:foreign_window`, a hold until it is gone or a human dismisses it |
| Secure Event Input held by another app | `blocked:secure_input`; no typing until it clears |
| Screen locked or display asleep on a desk | the desk keeps no screen lock (open question 6); if locked anyway, lanes hold with `blocked:locked` |
| Mac sleeps or loses power | node `sleeping`; pinned work waits and alerts; FileVault unlock needed after a power cut |
| Desk input lock contended | the second step waits up to the lane busy timeout, then `lane_busy` |
| VM limit reached | a third macOS VM desk is refused with `resources_low` and a reason |

## 5. Fit: what changes in which package

**The Interaction protocol, generalized.** Today `LaneManager.using()` yields a Playwright
`Page` and `Interaction` takes a `Page`. ANDROID.md already proposed a parallel protocol;
this design unifies them. Sketch for `src/openajar/node/interfaces.py` (a lead change):

```python
class Target(BaseModel):
    ref: str | None = None                  # "e12", "w1e4", "m3"
    point: tuple[float, float] | None = None
    window: str | None = None               # required with point on desktops

class Surface(Protocol):
    """What an agent can see and do in a lane, whatever the lane is."""
    kind: str                                # "browser" | "desktop" | "android"

    async def snapshot(self, *, scope: str | None = None) -> SnapshotResult: ...
    async def act(self, target: Target, action: ActionKind, text: str | None = None,
                  *, keys: list[str] | None = None, to: Target | None = None) -> ActionResult: ...
    async def screenshot(self, *, window: str | None = None) -> bytes: ...
    async def signal(self) -> Signal: ...
    def typist(self) -> Typist: ...          # the vault's adapter for this surface

class LaneHandle(Protocol):
    surface: Surface
    page: Page | None                        # browser lanes only, for existing code paths

class DeskEngine(Protocol):
    name: str                                # "macos-session" | "macos-vm" | "macos-lend" | "windows-session"
    async def attach(self, spec: LaneSpec, *, lane_dir: Path) -> "DeskSession": ...
    async def sweep_orphans(self) -> int: ...

class DeskSession(Protocol):
    @property
    def alive(self) -> bool: ...
    surface: Surface
    async def apps(self) -> list[dict]: ...                     # running allowed apps and windows
    async def launch(self, app: AppRef) -> None: ...            # verified against team id
    async def open_view(self, mode: ViewMode) -> "StreamEndpoint": ...   # RFB over a Unix socket
    async def permissions(self) -> dict[str, bool]: ...         # accessibility, screen, listen, post
    async def pause(self) -> None: ...                          # drop input now (kill, take over)
    async def close(self) -> None: ...
```

The existing browser `Interaction` becomes `BrowserSurface`, a thin adapter around the
current module, so browser tools and flows change only in how they get their surface.
`LaneManager.using()` yields a `LaneHandle`; `handle.page` keeps every browser call site
working during the migration. The control gate, holds, signals and leases are untouched:
they already work per lane.

**Package by package.**

| Package | Change |
|---|---|
| `openajar.core.models` | `LaneKind.DESKTOP`; `AppRef`, `DesktopSpec`, `LaneSpec.desktop`; `ActionKind` adds `double_click`, `right_click`, `drag`, `chord`, `menu`; `Signal` details for `foreign_window`, `secure_input`, `locked`, `permission_lost`; `Effect` specials `desktop`, `desktop:lend`; node profile enum `dedicated`, `daily` |
| `openajar.node.interfaces` | `Surface`, `Target`, `LaneHandle`, `DeskEngine`, `DeskSession` (sketch above); `REF_RE` widened |
| `openajar.node.snapshot` | the outline renderer split into a shared core and two front ends: Playwright aria text (today) and an AX JSON dump (new); same budget, hints and value rules |
| `openajar.node.desktop` (new) | the desk engine client: helper socket protocol, input plans from `human.py`, desk input lock, app profile loader, `desktop_*` tools |
| `openajar.node.lanes` | dispatch on kind to a browser engine or a desk engine; `using()` yields a handle; desk sessions count against warm limits with their own memory estimate |
| `openajar.node.vnc_relay`, `viewer_app` | relay to a Unix socket endpoint as well as a TCP port; nothing else |
| `openajar.node.control_tools` | none; `lane_control` works for desktop lanes as is |
| `openajar.node.daemon` | macOS startup: node profile, power assertions, the desk sockets, FileVault and session checks in health |
| `openajar.vault` | sinks `app_username`, `app_password`, `app_otp`; entries bound by `app` (bundle id plus team id); `KeyCustody` protocol with `file`, `passphrase`, `keychain-se`; Credential Exchange importer behind the menu-bar app |
| `openajar.policy` | effect rules for desktop tools and app profiles; `desktop:lend` grant constraints (no schedules, no flows, no brains, TTL ceiling); trifecta check for lend |
| `openajar.env` | a `MacHost` alongside `NullHost` and `LinuxHost`: uid-based process control, the resource watchdog, pf anchor management; presets report the macOS mapping honestly (`can_isolate` per dial) |
| `openajar.client` | `openajar connect` unchanged; the menu-bar app reuses its Keychain token store |
| `openajar.ops` | `doctor` checks: TCC states, SMAppService status, FileVault, power settings, display, desk session up |
| new `macos/` (Swift package) | `openajar.app`: menu bar, setup checklist, desk helper (AX, ScreenCaptureKit, CGEvent, event tap, RFB encoder, Secure Enclave custody, LocalAuthentication) |
| `docs/PROTOCOL.md` | additive only (below) |

**Protocol (additive, stays v1).** `enroll` already carries `os` and `capabilities`; new
capability strings `desktop`, `desktop-lend`, `keychain`. `welcome.config` and
`config_update` may carry `profile` (`dedicated` or `daily`). `heartbeat.load` may add an
optional `power { on_battery, low_power, thermal, assertion }` so the controller can
avoid routing to a struggling laptop. `stream_open` kind `vnc` is reused for desks. The
`lane` event payload carries `desktop { host, desk, apps }` (no window titles, which can
be private). A peer that does not know a field ignores it, as v1 already requires.

**Data model.** No new entity beyond `Desk` (node-local: id, host kind, macOS user or VM
id, display size, input lock holder) and `AppProfile` (first-party, reviewed, versioned
like flows; effect rules, stable ids, known dialogs and login markers). App profiles are
curated in the repo, not a marketplace (principle 8).

## 6. Phased plan

Sizes for one builder: S up to 3 days, M 1 to 2 weeks, L 3 to 4 weeks.

| Phase | What | Size | Needs |
|---|---|---|---|
| 0 | Spikes S1 to S6 (below), written up in docs/design/desktop-nodes-spikes.md | M total | a Mac mini, or a dedicated macOS test user the owner creates; the owner's hands for TCC |
| 1 | Contracts and pure logic, all on fakes (briefs below) | M (four builders in parallel) | nothing beyond the repo |
| 1b | Quick win: a Linux node in a Virtualization.framework guest on the Mac mini, as a node template | S | the Mac mini |
| 2 | The Swift helper: AX outline dump, AX actions, CGEvent replay of input plans, window capture to RFB over a Unix socket, event tap, permission checks, peer checks | L | Apple Developer ID (open question 1) |
| 3 | macOS packaging: LaunchDaemon plus SMAppService agent, desk user setup, power, FileVault guide, doctor, menu-bar owner mode with Touch ID approvals | M | phase 2 |
| 4 | Keychain custody (`keychain-se`), crown-jewel release, the import UI, Credential Exchange | M | phase 2 |
| 5 | Native browser lanes on macOS: Camoufox in the desk, remote server endpoint, pf egress, window viewer, fingerprint coherence, self-check | M | phases 2 and 3, spike S5 |
| 6 | Lend mode on the daily Mac | M | phases 2 to 4 |
| 7 | VM desks | L | spike S2 |
| 8 | Windows (section 6.3) | L to XL | a Windows machine |

Order rationale: phase 1 can start now with no hardware. 1b gives the owner Meta lanes on
a home IP as soon as a mini exists. Phases 2 and 3 are the foundation; 4 and 5 deliver
the "trusted Mac mini" story; lend (6) waits until the safety machinery is proven on a
machine that is not the owner's daily one.

### 6.1 Phase 0 spikes

| Spike | Question | Done when |
|---|---|---|
| S1 desks | Do ScreenCaptureKit and CGEvent work in a fast-user-switched background session, and in a Screen Sharing session for a second user? Headless display behaviour on a mini? | a table: per session type, AX yes/no, capture yes/no, input yes/no, resolution |
| S2 VM desks | Boot time, RAM, AX and capture inside a macOS guest, what apps and sites can tell, Apple ID limits | numbers and a list of tells |
| S3 TCC | Per-user or per-Mac grants; grants across an update with the same signature; Screen Sharing input on permission panes; behaviour of the periodic capture prompt when nobody answers; is post-event access covered by Accessibility | a written answer per item with macOS version |
| S4 secure input | Do synthetic keystrokes reach an `AXSecureTextField` while Secure Event Input is on; can our tap see real input then | yes/no with the macOS version |
| S5 browser | Camoufox remote server endpoint from another user's session; macOS fingerprint coherence on real Apple GPU; the existing self-check on macOS | self-check green on a throwaway account; the fingerprint test page shows no mismatch |
| S6 egress | pf rules by uid on current macOS (the `user` qualifier) and an anchor that survives reboots and updates | a desk user's direct connection is dropped, its forwarder path works |

### 6.2 Phase 1 builder briefs

**What is safe to run on the owner's Mac, for every phase 1 builder.** Only the repo's
own tests and tools: `uv run pytest`, `make check`, linters, all against fakes and
fixtures. Do not import or call pyobjc, AppKit, ApplicationServices or ScreenCaptureKit;
do not run `security`, `launchctl`, `sfltool`, `tccutil`, `pfctl`, `sysadminctl`, `dscl`,
`fdesetup`, `pmset`, `caffeinate`, `swift`, `xcodebuild`, or anything that posts input,
captures the screen, reads the Keychain, creates users or changes system settings. Do not
install packages or tools. Do not open apps. Nothing in phase 1 needs a real Mac API; if a
test seems to, it needs a fake. Keep to the existing dependency lock. The owner's Mac is
already busy (about 10 load on 10 cores), so run focused test files while iterating and
the full suite once at the end.

Builders in phase 2 and later that touch real macOS APIs run them only in a dedicated
macOS test user the owner creates and logs into (fast user switching), or on the Mac
mini, never in the owner's own session; the owner grants TCC by hand there. Compiling the
Swift package is safe; running the helper is not, outside those places.

**Brief P1-A: desktop contracts and the AX outline.**
Goal: the data shapes and the outline renderer, so a desk can be described and its tree
shown exactly like a page.
- Add `LaneKind.DESKTOP`, `AppRef`, `DesktopSpec`, `LaneSpec.desktop`, the new
  `ActionKind` values and signal details in `openajar.core.models`, with validation
  (allowlist non-empty, team id shape, lend forces `input="ax"`).
- Propose the `Surface`, `Target`, `LaneHandle`, `DeskEngine`, `DeskSession` protocols for
  `openajar.node.interfaces` as a patch for the lead (that file is lead-owned); keep the
  browser path compiling with `handle.page`.
- Split `snapshot.py` into a shared outline core and the Playwright front end, with no
  behaviour change (existing tests stay green), and add the AX front end: input is a JSON
  dump of the AX tree (define its schema in `openajar.node.desktop.axdump`), output is the
  outline of section 4.2: role map, `[id=]`, `[box=]`, window and menu refs, secure fields
  never valued, `[ax=poor]` marking, the same size budget.
- Fixtures: hand-written AX dumps for an AppKit form window, a table, a menu, an Electron
  app with and without its tree, a secure field. Widen `REF_RE` and test it.
- Done when: the outline for each fixture is golden-tested; no secure value can appear;
  browser snapshot tests unchanged.

**Brief P1-B: desktop tools, policy and the fake desk.**
Goal: agents can use desktop lanes end to end on `openajar dev up` with a fake desk.
- `openajar.node.desktop`: the desk client against an abstract socket transport, the
  per-desk input lock, input plans built from `human.py` generators (a plan is a list of
  timed events; the helper will replay it), and the tools `desktop_snapshot`,
  `desktop_act`, `desktop_screenshot`, `desktop_apps`, `desktop_launch`,
  `desktop_lend_request` with ToolSpecs, effects and node-authored summaries.
- A `FakeDesk` engine (promoted next to the other dev fakes): a scripted app with windows
  and an AX dump, which applies acts to its own state, can raise a foreign window, lose
  permissions and hold secure input on demand.
- Policy in `openajar.policy`: the `desktop` and `desktop:lend` specials, app allowlist and
  the fixed blocklist checks at the daemon, app profile effect rules, the lend
  constraints (TTL ceiling, no schedules, flows or brains, not narrowable into a run),
  the lend trifecta check.
- App profile format (YAML, first-party) with effect rules by `AXIdentifier` or menu path,
  login markers and known dialogs; one sample profile for the fake app.
- Done when: an MCP session on `dev up` opens a fake desktop lane, snapshots, fills a form
  by ref, hits an `irreversible` control and gets `approval_pending` with a node-written
  summary; acting on a blocklisted or non-allowed app is refused with a clear code; a
  foreign window yields `blocked:foreign_window` and a hold; take over and hand back work
  through the existing control tools.

**Brief P1-C: vault app sinks and key custody.**
Goal: secrets can be bound to an app and typed by a desktop typist; the master key can
live behind a custodian.
- `openajar.vault.policy`: sinks `app_username`, `app_password`, `app_otp`; entries bound
  by `app` (bundle id plus team id) with the same MAC-protected metadata as domains;
  field qualification from structural AX facts only (subrole, identifier, content type,
  enabled, focused, owning app and team).
- The desktop typist adapter against the `Typist` protocol, talking to an abstract desk
  transport: secret sent as a binary frame, per-keystroke checks reported back by the
  helper, abort semantics; tested with the `FakeDesk` from P1-B (coordinate the fake's
  interface with that builder, or stub it locally).
- `KeyCustody` protocol in `openajar.vault.crypto` with today's `file` and `passphrase`
  moved behind it unchanged, and a `FakeEnclave` custodian implementing wrap, unwrap and a
  presence-gated release, so the `keychain-se` backend can drop in later.
- Done when: a password reaches only a secure field of the bound app with matching team
  id; every mismatch (other app, other team, plain text field, focus change mid-type) is
  refused or aborted; existing vault tests unchanged; a vault created under `file` reads
  identically through the protocol.

**Brief P1-D: node profiles, power policy and protocol fields.**
Goal: a node knows whether it is a dedicated Mac or a daily Mac and behaves accordingly.
- Node profile (`dedicated`, `daily`) in node.toml and `config`, enforced in the daemon:
  `daily` refuses schedules, flows, brains and warm browser lanes, and allows only lend
  desktop lanes.
- A power policy module with a `PowerSource` protocol and a fake: decides "hold a sleep
  assertion", "refuse a lend", "pause a lend" from battery, low power, thermal state,
  warm lanes and upcoming schedules; no real IOKit calls.
- Additive protocol fields: capabilities `desktop`, `desktop-lend`, `keychain`; `profile`
  in config; optional `power` in heartbeat load; `desktop` in the lane event payload.
  Update docs/PROTOCOL.md "v1 clarifications" and the controller to route unpinned work
  only to `dedicated` nodes.
- `doctor` checks as pure functions over a fake system report (TCC states, FileVault,
  power settings, desk session up), so phase 3 only adds the real probes.
- Done when: a `daily` node refuses a schedule with a clear code; the controller never
  routes unpinned work to it; the power policy table in section 4.5 is covered by tests;
  an old controller ignores the new fields.

### 6.3 Windows later

Same lane kind, same `Surface`, a `windows-session` desk engine:

- **Outline:** UI Automation [M1]: the control view of the tree, `ControlType` mapped to
  the same role words, `AutomationId` as the stable `[id=]`, `IsPassword` marks secure
  fields (never valued), `BoundingRectangle` for boxes. Patterns (Invoke, Value, Toggle,
  SelectionItem, ExpandCollapse) are the AX-action analogue [M2].
- **Input:** SendInput [M3] with the same input plans. User Interface Privilege Isolation
  blocks input from a lower integrity process into a higher one [M4]; the per-session
  agent runs at medium integrity and does not request `uiAccess` (which would need a
  signed binary in a protected location [M4]) unless an allowlisted app runs elevated,
  which the lane should refuse by default. UAC prompts are on the secure desktop, which
  synthetic input cannot reach, and that is the property we want.
- **View:** Windows Graphics Capture [M5] for per-window capture (Desktop Duplication [M6]
  for a whole desk). Windows can mark windows as excluded from capture
  (`SetWindowDisplayAffinity`), which the outline reports.
- **Daemon:** `agentosd` as a Windows service; services run in session 0 with no access to
  the user's desktop [M7], so a per-session agent is started in the desk user's session
  (`WTSQueryUserToken` plus `CreateProcessAsUser` [M8]) and talks to the service over a
  named pipe with a restrictive DACL.
- **Desks:** a client edition of Windows has one interactive session at a time; a
  dedicated Windows mini PC with an `openajar-desk` user is the dedicated desk. Microsoft's
  experimental Agent Workspace, a separate session under a separate agent account [M14],
  may be the native answer and should be evaluated when it leaves preview. Hyper-V and
  Windows Sandbox [M12, M13] are the VM desk analogue.
- **Vault:** Credential Manager entries are DPAPI protected [M9, M10] and readable by any
  process of the same user, weaker than Keychain access lists, so they are an import
  source only. Key custody is a non-exportable key in the TPM through the Microsoft
  Platform Crypto Provider, presence through Windows Hello [M11].

## 7. Risks

| Risk | Likelihood | Effect | Mitigation |
|---|---|---|---|
| Apple tightens capture or input permissions further | medium | features degrade or need re-approval | stay on public APIs and the documented entitlement route; AX-first design keeps working without capture |
| Only one bare-metal desk per Mac | high | fewer concurrent native app lanes per mini | VM desks for a second and third; most of the owner's load is browser lanes, which share a desk without contention |
| Native apps with poor accessibility | medium | the agent falls back to screenshots and points, which is slower and more fragile | `[ax=poor]` marking, coordinate fallback, app profiles; pick first apps with good AX |
| Meta links the businesses because they share one Mac | medium | linked accounts, checkpoints | per-company proxy and fingerprint as today (AUD-02 decision applies); the personal account on the home IP; first test on throwaway accounts |
| The helper becomes a high-value target | medium | full desk control | minimal code, no network, peer checks both ways, signed and notarized, hardened runtime [A27], its own review |
| Agent clicks the wrong thing in a real app | medium | wrong data in the books, an email sent | app profiles with commit controls, approvals with screenshots, readback in flows, start on a test company file |
| FileVault reboot leaves the mini offline | medium | scheduled posts miss | UPS, SSH unlock from the LAN, authrestart for updates, the dead-man alert already in the runner |
| Two languages to maintain | certain | slower changes to the helper | keep the helper dumb (no policy, replay plans), its protocol versioned and tested from Python with a fake |
| Developer ID and notarization cost and process | certain | 99 USD a year and an Apple account | open question 1; without it TCC grants are fragile |
| The daily Mac gets slower anyway | low | the original pain returns | `daily` profile refuses heavy work; lends are capped and pause on power and thermal pressure; benchmarks in the backlog |

## 8. Open questions for the owner

1. **Apple Developer Program membership** for signing and notarizing `openajar.app`, and
   for requesting the persistent content capture entitlement. Recommended default: yes,
   under the owner's own team; without it, TCC grants break on every rebuild.
2. **FileVault on the Mac mini.** Recommended default: on, with SSH unlock from the home
   network after a power cut (Apple silicon, macOS 26), `fdesetup authrestart` for
   updates, and a small UPS. The desk user is the one that unlocks, so its session starts
   after unlock. The alternative (FileVault off with automatic login) restarts without
   help but leaves the disk readable to anyone who takes the mini.
3. **Do you have a Mac mini, and which?** Recommended default: an Apple silicon Mac mini
   with at least 24 GB (32 GB if VM desks are wanted), on Ethernet, with an HDMI dummy
   adapter if spike S1 says the headless display is not usable.
4. **Which native app first?** The design needs one real app to profile. Recommended
   default: the business app you would most like off your hands that has no web version,
   provided its accessibility tree is decent (S1 includes a quick check of your
   candidates).
5. **Lend mode on the daily Mac at all?** Recommended default: built (phase 6) but off
   until you turn it on; AX-only; 10 minutes default; Finder and your browsers always
   blocked.
6. **Screen lock on the desk user.** Recommended default: no screen lock on the desk
   user, because lanes cannot act on a locked session; the desk user has no admin rights,
   no password manager and no Apple ID, and the mini is at home. If you prefer a lock,
   desk lanes pause whenever it is locked.
7. **Reading your existing login keychain.** Recommended default: import only, started by
   you in the menu-bar app, per item with the system's own prompt; no agent-visible
   Keychain access ever.
8. **Meta lanes before native macOS browser lanes ship.** Recommended default: if the
   mini arrives before phase 5, run the Linux VM node on it (phase 1b) for the residential
   IP, and move the lanes to native macOS lanes after spike S5 passes on a throwaway
   account, with your go before any real account moves (a device change is a checkpoint
   trigger).
9. **Approvals on the mini.** Recommended default: phone approvals only; a Touch ID
   keyboard on the mini is optional for when you are at home.
10. **Windows timing.** Recommended default: after phase 6, and only once you name a
    Windows-only app that matters; evaluate Microsoft's Agent Workspace then.

## References

Apple

- [A1] Accessibility API, AXUIElement: https://developer.apple.com/documentation/applicationservices/axuielement_h
- [A2] Accessibility programming guide for macOS (roles, actions, attributes): https://developer.apple.com/library/archive/documentation/Accessibility/Conceptual/AccessibilityMacOSX/
- [A3] ScreenCaptureKit, SCContentFilter, SCContentSharingPicker: https://developer.apple.com/documentation/screencapturekit
- [A4] Electron accessibility (`AXManualAccessibility`): https://www.electronjs.org/docs/latest/tutorial/accessibility
- [A5] AXIsProcessTrustedWithOptions: https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions
- [A6] CGEvent (keyboard, mouse, scroll events; posting to a process): https://developer.apple.com/documentation/coregraphics/cgevent
- [A7] Event taps and access checks (CGPreflightListenEventAccess, CGPreflightPostEventAccess, CGRequestPostEventAccess): https://developer.apple.com/documentation/coregraphics/quartz-event-services
- [A8] CGPreflightScreenCaptureAccess: https://developer.apple.com/documentation/coregraphics/cgpreflightscreencaptureaccess()
- [A9] Virtualization framework, running macOS in a virtual machine on Apple silicon: https://developer.apple.com/documentation/virtualization and https://developer.apple.com/documentation/virtualization/running-macos-in-a-virtual-machine-on-apple-silicon
- [A10] The two concurrent macOS guests limit (licence and framework): https://eclecticlight.co/2022/08/04/virtualisation-on-apple-silicon-macs-8-how-apple-limits-vms/ and https://developer.apple.com/forums/thread/729580
- [A11] Persistent content capture entitlement: https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.persistent-content-capture and https://developer.apple.com/forums/thread/761641
- [A12] Using iCloud with macOS virtual machines: https://developer.apple.com/documentation/virtualization/using-icloud-with-macos-virtual-machines
- [A13] SMAppService: https://developer.apple.com/documentation/servicemanagement/smappservice
- [A14] launchd jobs (LaunchAgents, LaunchDaemons): https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html
- [A15] Power assertions (IOPMAssertionCreateWithName), ProcessInfo activities and thermal state: https://developer.apple.com/documentation/iokit/1557134-iopmassertioncreatewithname and https://developer.apple.com/documentation/foundation/processinfo
- [A16] Notarizing macOS software: https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution
- [A17] Privacy Preferences Policy Control payload: https://developer.apple.com/documentation/devicemanagement/privacypreferencespolicycontrol
- [A18] PPPC in the Apple Platform Deployment guide: https://support.apple.com/guide/deployment/dep38df53c2a
- [A19] Keychain services: https://developer.apple.com/documentation/security/keychain-services
- [A20] Protecting keys with the Secure Enclave; SecKeyCreateEncryptedData: https://developer.apple.com/documentation/security/protecting-keys-with-the-secure-enclave
- [A21] SecAccessControlCreateWithFlags (user presence, biometry): https://developer.apple.com/documentation/security/secaccesscontrolcreatewithflags(_:_:_:_:)
- [A22] Credential Exchange: ASCredentialImportManager, ASCredentialExportManager (macOS 26): https://developer.apple.com/documentation/authenticationservices/ascredentialimportmanager
- [A23] The periodic screen recording prompt on macOS 15 (reports): https://tidbits.com/2024/08/19/apple-reduces-excessive-sequoia-permission-requests-shifts-to-monthly and https://9to5mac.com/2024/08/14/macos-sequoia-screen-recording-prompt-monthly/
- [A24] Running iOS apps on Apple silicon Macs: https://developer.apple.com/documentation/apple-silicon/running-your-ios-apps-in-macos
- [A25] TN3137, On Mac keychain APIs and implementations: https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains
- [A26] LocalAuthentication, LAContext: https://developer.apple.com/documentation/localauthentication/lacontext
- [A27] Hardened runtime: https://developer.apple.com/documentation/security/hardened-runtime

Microsoft

- [M1] UI Automation: https://learn.microsoft.com/windows/win32/winauto/entry-uiauto-win32
- [M2] UI Automation control patterns: https://learn.microsoft.com/windows/win32/winauto/uiauto-controlpatternsoverview
- [M3] SendInput: https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-sendinput
- [M4] UAC, integrity levels and UIAccess: https://learn.microsoft.com/windows/security/application-security/application-control/user-account-control/how-it-works and https://learn.microsoft.com/windows/security/threat-protection/security-policy-settings/user-account-control-only-elevate-uiaccess-applications-that-are-installed-in-secure-locations
- [M5] Windows Graphics Capture: https://learn.microsoft.com/windows/uwp/audio-video-camera/screen-capture
- [M6] Desktop Duplication API: https://learn.microsoft.com/windows/win32/direct3ddxgi/desktop-dup-api
- [M7] Interactive services and session 0: https://learn.microsoft.com/windows/win32/services/interactive-services
- [M8] WTSQueryUserToken: https://learn.microsoft.com/windows/win32/api/wtsapi32/nf-wtsapi32-wtsqueryusertoken
- [M9] CredReadW (Credential Manager): https://learn.microsoft.com/windows/win32/api/wincred/nf-wincred-credreadw
- [M10] CryptProtectData (DPAPI): https://learn.microsoft.com/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata
- [M11] KeyCredentialManager (Windows Hello): https://learn.microsoft.com/uwp/api/windows.security.credentials.keycredentialmanager
- [M12] Hyper-V on Windows: https://learn.microsoft.com/virtualization/hyper-v-on-windows/
- [M13] Windows Sandbox: https://learn.microsoft.com/windows/security/application-security/application-isolation/windows-sandbox/
- [M14] Experimental agentic features and Agent Workspace in Windows 11 (preview, reports): https://www.windowscentral.com/microsoft/windows-11/microsoft-just-revealed-how-windows-11-is-evolving-into-an-agentic-os-finally-the-explanation-weve-all-been-waiting-for

Other

- [R1] Synthetic clicks and macOS security prompts (2018 to 2019 research; current behaviour is spike S3): https://objective-see.org/blog/blog_0x36.html
- [R2] Remote FileVault unlock over SSH in macOS 26 (reports): https://www.jeffgeerling.com/blog/2025/you-can-finally-manage-macs-filevault-remotely-tahoe/
- [R3] Camoufox (macOS arm64 builds): https://github.com/daijro/camoufox
