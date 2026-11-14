# Android lanes: feasibility and recommendation

Research, 2026-11-04. Branch `t3/452b76a8`, based on `214350a`. No code and no node changes. Node checks were read-only on the 4 GB dev VM (Ubuntu 24.04.4, kernel 6.8.0-137-generic, x86_64).

## Recommendation

- **The 4 GB dev VM: do not build or run Android lanes here.** It has no hardware virtualization, so the official emulator is out. Its binder driver is built as modules, but the module files are not installed. It has 3.8 GB RAM and no swap, shared with the PoC lanes that hold real logged-in accounts. The only option on this box is one redroid instance, after the owner approves installing `linux-modules-extra-6.8.0-137-generic`. I would not do that while the PoC runs.
- **Home box or bigger server: redroid on x86_64.** Ubuntu 24.04, 16 GB RAM, 4 or more vCPU, legacy `binder_linux` (not binderfs, see below). One privileged container per warm Android lane, each in its own network namespace routed through that lane's static ISP proxy. Warm limit of 4 to 6 lanes, the rest cold (disk only). A cheaper alternative is an arm64 Ampere VM, which runs arm64 redroid images natively, but its kernel's binder support is not checked yet.
- **Apps that require a device integrity verdict: a real phone, not a virtual device.** A redroid instance cannot get the device verdict that Play Integrity defines. We do not try to fake it.
- **Build now or later: spike now, build the engine later.** A 3 to 5 day spike on a throwaway x86_64 VM with 8 GB RAM answers the three questions that decide the project: RAM per warm instance, whether the target apps run at all, and how often they challenge (the last one only with an owner-approved account). Build the engine in wave 3, after the controller and a 16 GB node exist. Only accounts that need an app-only feature need an Android lane, so expect a handful of Android lanes, not 29.

## Node check (read only, 2026-11-04)

| Check | Result | Meaning |
|---|---|---|
| OS, kernel | Ubuntu 24.04.4 LTS, 6.8.0-137-generic, x86_64 | The generic kernel that the redroid Ubuntu guide targets |
| CPU, virtualization | 2 vCPU (Intel Xeon, Skylake). `vmx`/`svm` flags: 0. `/dev/kvm` absent. `systemd-detect-virt`: kvm | A KVM guest that does not expose VT-x. The official emulator cannot accelerate here. |
| Binder | `CONFIG_ANDROID_BINDER_IPC=m`, `CONFIG_ANDROID_BINDERFS=m`, `CONFIG_ANDROID_BINDER_DEVICES=""`. `modinfo binder_linux`: not found. No `/lib/modules/.../drivers/android/`. `linux-modules-extra` not installed. No binder nodes. binder not in `/proc/filesystems` | Support is compiled as modules, but the `.ko` files are not on disk. Installing `linux-modules-extra-$(uname -r)` is the route. Nothing was loaded. |
| ashmem | No `CONFIG_ASHMEM` line | Expected for 5.18 and later kernels. Use memfd. |
| Container runtime | No docker, no podman, no `/var/run/docker.sock` | Needs installing (a change, needs approval) |
| Memory | 3819 MB total, 3134 MB available, no swap | One Android instance would take most of the free RAM |
| Disk | 28 GB free of 38 GB on `/` | Room for one or two lanes, not 29 |
| GPU | `00:01.0` Red Hat virtio 1.0 GPU. `/dev/dri/card0` and `renderD128` present | A virtual GPU. Android rendering will be virtual or software. |
| User namespaces | `kernel.unprivileged_userns_clone=1`, `user.max_user_namespaces=15127` | Namespaces are available for per-lane network isolation |
| Network | `lo` and `eth0` only | One public address, so per-lane tunnels are needed |

Nothing was installed, loaded, started or changed. `/root/agentos`, `/var/lib/agentos`, `/opt/agentos` and the agentosd service were not touched.

## 1. Options

RAM and CPU figures below are estimates, not measurements. The public figures I found are planning guesses (one blog says 2 to 4 GB per instance, another says at least 4 GB per host). The spike has to measure them.

| Option | Host needs | RAM and CPU per instance | Fits on 4 GB / 16 GB | License | Android versions | How it looks to apps |
|---|---|---|---|---|---|---|
| **redroid** (Android in Docker) | Docker, binder (`binder_linux` module or binderfs), memfd or ashmem, privileged container. Runs on x86_64 and arm64. | Estimate 1.5 to 2.5 GB resident when booted, plus host overhead. CPU near zero idle, 0.5 to 1 vCPU while an app is busy. | 4 GB: 1, and not beside anything else. 16 GB: 4 to 6 warm, the rest cold. | Apache per a mirror of the README (not confirmed upstream). Kernel modules GPL-2.0. Google apps (GApps) are separate and proprietary. | README lists Android 16 down to 8.1. Docker Hub tags for amd64 and arm64. | AOSP-based. No Google services in the base image. OpenGApps, MicroG or MindTheGapps can add them. Virtual hardware is probably visible. Not tested. |
| **Waydroid** | binder, plus ashmem or memfd. A Wayland session (headless is not documented). | Not measured. Similar order to redroid. | Probably 1 on 4 GB. 4 to 6 on 16 GB (estimate). | GPL-3.0 | Image is based on Android 13 (LineageOS 20) per the docs. Release 1.6.3 (May 2026) adds "initial support for Android 16 images". | LineageOS-based. GApps via `waydroid init -s GAPPS`. Not tested. |
| **Official Android Emulator** | KVM with VT-x or AMD-V. Google says: "You can't run a VM-accelerated emulator inside another VM, such as a VM hosted by VirtualBox, VMWare, or Docker." | Google does not state a recommendation. The `-memory` option accepts 1536 to 8192 MB. | Not possible on the 4 GB dev VM. Technically possible on a home box with VT-x. | The SDK terms (§3.1) limit use to "developing applications for compatible implementations of Android". I found no clause about hosting, but I read that as not allowing this product. Confirm with Google. | Current. Android 16 is stable, Android 17 is in the docs with 17 QPR1 in beta (April 2026). | Real Google system images. Best realism, blocked by the license and by hardware on the cheap VM. |
| **Anbox / Anbox Cloud** | n/a | n/a | n/a | Anbox archived on 13 February 2024. Anbox Cloud is closed, Canonical, enterprise. | Stale | Do not use. Waydroid is the successor. |
| **Genymotion** | Desktop, with VirtualBox for x86 images (not verified here) | Not verified | Not verified | The free build is for personal use. Sources say business use breaks its terms. Paid price sources conflict (one Q&A gives $206 per computer per year). | Sources conflict on whether Android 16 needs a paid license. | Real-looking images, commercial. Not recommended for a business without a paid license. |
| **arm64 cloud VM with redroid** | Ampere or Arm VM. Ubuntu arm64 kernel with binder (not checked). | Same as redroid. Native arm64 images, no translation. | 16 GB Arm VM: 4 to 6 warm (estimate). | Same as redroid | Same as redroid | Most mainstream apps ship arm64 builds (check per app). Still a virtual device, so no device verdict. |

Prices and limits (sources conflict; check the official pages before deciding):
- Hetzner CAX (Ampere): an August 2026 comparison lists CAX11 (2 vCPU, 4 GB) at about 6 per month, currency not shown.
- Oracle Always Free A1: InfoQ and Linuxiac report a June 2026 cut from 4 OCPU and 24 GB to 2 OCPU and 12 GB. The official page snippet I saw still showed the old figure.

### Why the legacy binder module, not binderfs

The redroid Debian guide says binderfs allows unlimited containers and gives the kernel options. The Ubuntu guide and the README use the legacy path: `modprobe binder_linux devices="binder,hwbinder,vndbinder"`. Issue 859 (open since June 2025) reports that images 10 to 15 fail with `Binder driver '/dev/binder' could not be opened` when binderfs is used on a modern kernel. So on this kernel, use the legacy module, and have the spike confirm that image 16 boots. Waydroid hits a similar problem on stock Ubuntu 24.04 (issue 1387, still open, last comment February 2026).

### Node install (needs approval, not done)

On the test VM: `apt install linux-modules-extra-$(uname -r)`, then `modprobe binder_linux devices=binder,hwbinder,vndbinder`, persisted through `/etc/modules-load.d/`, then install Docker. The redroid-modules README gives the devices list and the persistence step. The apt package step comes from community posts, not from official Ubuntu docs.

## 2. Detection and account safety

### Play Integrity and SafetyNet

- SafetyNet Attestation is gone. Google deprecated it in 2022 and turned it off on 31 January 2025. Apps must use Play Integrity.
- Device verdicts, from Google's definitions:

| Label | Requires | On a redroid lane |
|---|---|---|
| MEETS_DEVICE_INTEGRITY | A genuine, certified Android device. On Android 13 and later, hardware proof of a locked bootloader. | Not met |
| MEETS_BASIC_INTEGRITY (opt-in) | Passes basic system checks. The bootloader may be unlocked, and the device may not be certified. | Possible, not verified |
| MEETS_STRONG_INTEGRITY (opt-in) | A certified device with a recent security update | Not met |
| MEETS_VIRTUAL_INTEGRITY | Documented only for Google Play Games for PC, an emulator powered by Google Play services | Not applicable |
| Empty | API hooking, a rooted device, or not a physical device | Likely |

So a redroid lane cannot pass the device check, by design. I could not find public docs on whether the apps in scope call Play Integrity, or how they act on the result. Treat that as unknown and measure it with a throwaway account.

**Decision:** we do not spoof, hide or relay Play Integrity verdicts. For an app that requires a device verdict, the honest path is a real certified phone with a locked bootloader, on the home node or behind a mobile proxy.

### Root and emulator detection in Instagram, Facebook, LinkedIn and Reddit

I found no public, current documentation of what these apps check. Treat it as unknown. Assume that a virtual device with Google services is a known category, so expect challenges.

### Device identity: what apps can read

| Identifier | Who can read it | Stability | Notes |
|---|---|---|---|
| ANDROID_ID | Any app | Per signing key, per user, per device. Changes on factory reset or a signing-key change. | The lane's data volume is what has to persist. |
| Advertising ID | Any app | User-resettable | Not a device identifier |
| IMEI, serial | Needs device owner, carrier permissions or READ_PRIVILEGED_PHONE_STATE (Android 10 and later) | n/a | Normal apps cannot read them, so they are not a lever |
| MAC address | System apps only | n/a | |
| Build fields, `ro.*` properties | Any app | Set at boot from the image | Not covered by the pages I read. Verify. |
| GSF ID, MediaDRM ID | Not covered by the pages I read | | Verify before relying on either |

What "stable" means for agentos: one lane is one data volume. The container is recreated from the same pinned image, with the same identity file, and an existing account's data volume is never replaced. The identity file (model, build properties, timezone, locale, screen size, GPU renderer) is written once on first launch, the same way `fingerprint_path` works today. Every field must agree with the lane's proxy country. The goal is consistent, stable identity, not an undetectable device. Virtual sensors and the virtual GPU are visible in principle. Not tested.

### Per-lane proxying

- **Route:** one network namespace per lane. Its default route goes through a tunnel into the lane's static ISP proxy, and all other egress from that namespace is dropped, so nothing can reach `eth0` directly. TCP, UDP, DNS and IPv6 all go through the tunnel, or IPv6 is disabled.
- **Android's own proxy setting** (`androidboot.redroid_net_proxy_type`, host, port) only sets the system proxy. Apps can ignore it, so it is not a boundary. Use the namespace route instead.
- **Google services**, if installed, must use the same tunnel. Their background calls would otherwise show the node's IP.
- **Per-app VpnService** is the device-side alternative. It takes an allow list or a deny list (not both), and the user must accept a consent prompt. The namespace route is simpler and stronger.
- **Self-check (spike B):** exit IP equals the proxy IP. DNS resolves through the proxy. No IPv6 leak. A packet capture on the host shows no direct egress from the namespace.
- **WebRTC:** apps rarely use it. Not assessed, low priority.

### What is not achievable

- A device verdict (MEETS_DEVICE_INTEGRITY) on a virtual device.
- Indistinguishability from a real phone for server-side behavior models or sensor checks. We can only be consistent.
- A residential IP from a datacenter node. The proxy's IP reputation is outside our control.
- Several accounts in one Android lane. One account, one lane.
- A guarantee that a given app does not detect the container.

## 3. Control and viewing

- **Transport:** adb over TCP. The redroid run command maps port 5555. Everything goes through adb.
- **Snapshot and refs:** uiautomator2 (openatx; Python 3.8+; device-side jar on Android 4.4+; built on UiAutomator, which is built on the accessibility service). Use its hierarchy dump to build an aria-like snapshot with `[ref=eN]`. Password fields are redacted in the snapshot, and their values are never returned. Verify the API and the masking in spike C. uiautomator2's license is not confirmed here.
- **Acting:** uiautomator2 `click` and `swipe`, but humanized: a random point inside the bounds, a real press duration, swipe-based scrolling, pauses. The same rules as the browser lanes (the owner's rules for real-account automation: human pacing, no robotic input, verify the real effect).
- **Live view and take over:** scrcpy mirrors and controls a device over adb, with no root and Android 5 and later. It is Apache-2.0 per fork notices (confirm in the official LICENSE). Run its window inside an Xvfb slot and reuse the existing x11vnc and noVNC path (`DisplayManager`), so the viewer does not change. It needs OpenGL, so test software GL on the virtio GPU. Apps may block capture on secure screens (FLAG_SECURE). Not tested.
- **Take over and hand back:** reuse `take_control`, `hand_back` and `note_human_input` from the lane manager.
- **Secrets:** never run `adb shell input text "<secret>"`. The text becomes a command argument, which other processes can see while it runs. I inferred this and did not test it. Check with `ps` in spike C. Use the vault typist contract instead: set the text through uiautomator2 (not as an argument), check the focused field and the app before each keystroke, stop on any change, and never log, trace, screenshot or return the text.
- **Installing apps without a Google account:** `adb install` an APK from the vendor's official source, after checking its signature against the vendor's published certificate. This needs no Google account. Google Play does need one, and a GApps or MicroG image adds a Google identity to each lane, so avoid it unless needed. Push notifications may depend on Google services. Not verified.

## 4. Fit with agentos

**Already in the repo:** `LaneKind.ANDROID` (`src/agentos/core/models.py`). The account preset is allowed for android lanes (`src/agentos/env/manager.py`). The lanes view maps android to the account preset (`src/agentos/api/lanes_view.py`). `agentos.android` is referenced in `src/agentos/core/runtime.py` but does not exist. `docs/ARCHITECTURE.md` already names redroid as an engine.

**Lifecycle, unchanged:**

| State | Android meaning |
|---|---|
| cold | Data volume on disk, container stopped, no RAM |
| warming | Container starts. Wait for `sys.boot_completed=1`. adb connects. Self-check runs. |
| warm | Container up and idle. Counts against `max_warm_lanes`. |
| active | A tool call holds the lane. uiautomator2 session in use. |
| quarantined | Self-check failed, or a challenge or checkpoint screen was detected by deterministic markers (current activity, visible text). Refuses traffic until a human reopens it. No automatic retry. |

**Self-check (Android):** exit IP through the tunnel. DNS and IPv6 as in section 2. Timezone and locale match the proxy's country. adb reachable. Target app installed at a pinned version. Logged-in marker detected by a flow, not by a model.

**Engine interface.** `src/agentos/node/interfaces.py` is browser-specific: `Engine.launch` returns a `BrowserSession`, and `LaneManager.using()` yields a Playwright `Page`. The Android engine needs a generic handle. Proposed, in the same Protocol style as `interfaces.py` (the file says changes are made by the lead):

```python
class AndroidEngine(Protocol):
    name: str  # "redroid"

    async def launch(self, spec: LaneSpec, *, data_dir: Path, identity_path: Path,
                     display: str, proxy: ProxyConfig | None) -> "AndroidSession":
        """Start the container in its own netns, route it through the proxy tunnel,
        pin the identity on first launch (identity_path), wait for boot, connect adb."""

    async def sweep_orphans(self, root: Path) -> int:
        """Stop containers whose data volume lives under root (left by a crashed daemon)."""


class AndroidSession(Protocol):
    @property
    def serial(self) -> str: ...        # adb endpoint, e.g. "127.0.0.1:5555"
    @property
    def alive(self) -> bool: ...
    async def close(self) -> None: ...  # stop the container, flush the data volume


class AndroidInteraction(Protocol):
    async def snapshot(self, s: AndroidSession) -> SnapshotResult: ...   # refs; password values redacted
    async def act(self, s: AndroidSession, ref: str, action: ActionKind,
                  text: str | None = None) -> ActionResult: ...          # humanized input only
    async def type_secret(self, s: AndroidSession, ref: str, secret: str) -> None: ...  # typist contract, never logs
    async def screenshot(self, s: AndroidSession) -> bytes: ...
    async def signal(self, s: AndroidSession) -> Signal: ...
```

Changes needed elsewhere:
- `LaneManager.using()` yields a generic handle: a `Page` for browser lanes, an `AndroidSession` for android lanes.
- Presets: only Account. `Locked` (microVM) is not achievable, because a privileged container shares the host kernel, so `Locked` must be refused for android lanes.

**Identity file.** Written once on first launch and pinned (chmod 600), then reused on every launch. It holds the model, build properties, timezone, locale, screen size, renderer, and the proxy country it must match. ANDROID_ID is created by Android and persists in the data volume.

**Isolation presets (Account):**
- Network: per-lane namespace and tunnel.
- Filesystem: private data volume.
- Processes: container PID namespace and cgroup.
- Programs: per-lane APKs (overlay).
- No shell. This is already enforced in `manager.py`.

## 5. Risks to the owner's accounts

| Risk | Why it matters | Mitigation |
|---|---|---|
| Platform terms | Instagram's terms prohibit creating accounts or collecting information in automated ways without express permission (help.instagram.com, from search snippets, verify). An Android lane does not change that. | No account creation. Human-paced operation. The owner decides per platform and per feature. |
| Virtual-device signal | No device verdict, so challenges are likely | A challenge means quarantine. A human acts by hand. No retry. Start with a non-critical account. |
| Identity drift | A rebuilt data volume or a new ANDROID_ID looks like a new device | Never replace an account's data volume without owner approval. Alert when the identity changes. |
| Shared kernel, privileged container | One container escape reaches the host and every lane on it. 29 accounts on one host is one blast radius. | Android lanes never run on the Mac or the PoC node. Consider one VM per company. |
| Network leak | Google services or a stray app sends traffic from the node's IP | Namespace default-deny. Packet-capture check in the spike and in the self-check. |
| Secrets in process lists | `adb input text` puts the secret in argv | Typist contract only. Test with `ps` in spike C. |
| Datacenter IP, proxy reputation | A cloud node IP is a risk flag | Same rule as browser lanes: every lane exits through its own static ISP proxy |
| Licensing | Redistributing Google apps. The SDK emulator and Genymotion's free build for business use. | Use redroid (Apache per the README mirror) with MicroG or no Google services. Do not use the SDK emulator or Genymotion's free build for the business. |
| Upgrade lag | The newest redroid image is Android 16 in the README. Android 17 is in the docs. The binderfs issue is open. | Pin the image tag per lane. Test upgrades in a spike before moving a lane. |

## 6. Build plan

Sizes: S is under 3 days, M is 3 to 7 days, L is over 7. These are estimates, not commitments.

| # | Step | Size | Acceptance |
|---|---|---|---|
| 0 | Owner approves a throwaway x86_64 VM with at least 8 GB RAM, Ubuntu 24.04, plus `linux-modules-extra` and Docker on that VM. Not the 4 GB dev VM. | S (owner time) | Owner approval recorded |
| A | Spike A: redroid 16 with legacy `binder_linux` on the throwaway VM. Measure boot time and RSS and CPU, idle and with one app open. Run a uiautomator2 hierarchy dump. Run scrcpy in Xvfb with software GL. | S, 1 to 2 days | Boots in under 3 minutes with no binder errors. adb and the hierarchy work. Numbers written into this doc. |
| B | Spike B: a network namespace with a tunnel to a test static proxy. Check with an IP echo service and a packet capture. | S, 1 to 2 days | Exit IP is the proxy IP. DNS and IPv6 do not leak. The capture shows no direct egress. |
| C | Spike C: secret entry through uiautomator2 `set_text`. `ps` check. Password masking in the snapshot. | S, half a day | No secret in argv. Snapshot shows no password values. |
| Gate | The owner reviews the numbers and the risk table, and decides go or no-go. Real accounts only after a separate owner approval. | | |
| 1 | `AndroidEngine`: container lifecycle, identity file, namespace tunnel, self-check, orphan sweep, tests with fakes (the same pattern as the browser engine tests) | M, 1 to 1.5 weeks | Unit tests pass. One-hour soak with start and stop cycles. |
| 2 | `AndroidInteraction`: snapshot with refs, humanized `act`, `type_secret`, screenshot, `signal` | M, about 1 week | Snapshot matches the browser contract. Typist contract tests pass. |
| 3 | Viewer: scrcpy in an Xvfb slot through `DisplayManager`, reusing noVNC | S, 2 to 3 days | The human can see and take over the lane in the existing viewer |
| 4 | Lane integration: generalize `LaneManager.using()` to a handle. Dispatch for `LaneKind.ANDROID`. Warm limit, reaper, quarantine. | S to M, about 1 week | Existing browser tests pass. Android lanes follow the same lifecycle. |
| 5 | Soak, then a read-only pilot on a non-critical account, then an owner-approved real account | S | Owner signs off |

Total: about 4 to 5 weeks of focused work after the owner's approvals, plus the spikes. The change to `node/interfaces.py` needs the lead's sign-off, per that file's rules.

## Open questions I could not settle

- Real RAM, disk and CPU per redroid instance (spike A).
- Whether image 16 and newer boot on `binder_linux` on this kernel, and whether any binderfs path works (issue 859).
- Whether the target apps run on a virtual device at all, and how often they challenge (needs an owner-approved account).
- Current Hetzner and Oracle prices and limits (sources conflict).
- Genymotion's paid prices and business terms (sources conflict; I did not read the official pricing page in full).
- Any root or emulator checks in Instagram, Facebook, LinkedIn or Reddit.
- GSF ID and MediaDRM ID rules, and which Build fields apps can read.
- The license of the redroid docs repository, and of uiautomator2.
- Whether Waydroid runs without a display on a server.

## Sources

Official pages
- redroid README and guides: https://github.com/remote-android/redroid-doc ; https://github.com/remote-android/redroid-doc/blob/master/deploy/ubuntu.md ; https://github.com/remote-android/redroid-doc/blob/master/deploy/debian.md
- redroid issue 859 (binderfs on modern kernels): https://github.com/remote-android/redroid-doc/issues/859
- redroid images: https://hub.docker.com/r/redroid/redroid/tags
- redroid-modules: https://github.com/remote-android/redroid-modules
- Waydroid: https://docs.waydro.id/usage/install-on-desktops ; https://docs.waydro.id/debugging/getting-essential-information.md ; https://docs.waydro.id/faq/get-waydroid-to-work-through-a-vm.md ; https://docs.waydro.id/usage/waydroid-command-line-options.md ; https://github.com/waydroid/waydroid ; issue 1387: https://github.com/waydroid/waydroid/issues/1387
- Android emulator: https://developer.android.com/studio/run/emulator-acceleration ; https://developer.android.com/studio/run/emulator-commandline ; SDK terms: https://developer.android.com/studio/terms
- Android versions: https://developer.android.com/about/versions
- Play Integrity: https://developer.android.com/google/play/integrity/verdicts ; https://developer.android.com/google/play/integrity/overview ; https://developer.android.com/google/play/integrity/setup
- SafetyNet: https://developer.android.com/training/safetynet/attestation
- Identifiers: https://developer.android.com/reference/android/provider/Settings.Secure ; https://developer.android.com/training/articles/user-data-ids
- VpnService: https://developer.android.com/reference/android/net/VpnService.Builder
- scrcpy: https://github.com/Genymobile/scrcpy
- uiautomator2: https://github.com/openatx/uiautomator2
- Instagram terms: https://help.instagram.com/581066165581870
- Anbox: https://github.com/anbox/anbox ; https://en.wikipedia.org/wiki/Anbox
- Oracle free tier: https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm

Secondary sources (used where the official page did not settle the point)
- Oracle A1 cut: https://infoq.com/news/2026/07/oracle-cloud-free-tier-limits/ ; https://linuxiac.com/oracle-quietly-cuts-free-tier-ampere-a1-resources-in-half/
- Hetzner CAX price: https://pcr.cloud-mercato.com/providers/hetzner/flavors/cax11
- Genymotion: https://www.genymotion.com/pricing/ ; https://rozz.genymotion.com/qna/what-are-genymotion-s-pricing-options.html
- Ubuntu binder modules (community): https://forum.proxmox.com/goto/post?id=644050
- redroid README mirror (license line): a mirror of remote-android/redroid-doc, not the upstream repository
