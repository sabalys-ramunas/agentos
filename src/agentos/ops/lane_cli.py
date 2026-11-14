"""The ops commands on one lane, mounted in `agentos lane`: refresh-fingerprint, check, health,
audit, stealth-check and redteam."""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Any

import typer

from agentos.core.config import load_config
from agentos.core.layout import ops_socket
from agentos.core.models import parse_lane_id
from agentos.core.system import Host
from agentos.node import fingerprint
from agentos.ops.cli_common import JsonOpt, _emit, _errors, _fail, _home, _node_name


def refresh_fingerprint_cmd(
    lane: Annotated[str, typer.Argument(help="workspace/name.")],
    keep_device: Annotated[bool, typer.Option("--keep-device", help="Change only the browser version fields "
                                                                    "(required).")] = False,
    version: Annotated[int | None, typer.Option(help="Firefox major version (default: the installed "
                                                     "Camoufox's).")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the change without writing it.")] = False,
    yes: Annotated[bool, typer.Option("--yes", help="For a lane on a device: change the device's pin, for every "
                                                    "lane on it.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Bump the browser version in a lane's pinned fingerprint to the installed Camoufox, keeping
    every other pinned value (navigator.userAgent and headers.User-Agent change; nothing else).
    For a lane on a device the change is the device's, so it needs --yes, after the list of every
    lane it affects."""
    try:
        parse_lane_id(lane)
    except ValueError as e:
        _fail(str(e))
    if not keep_device:
        _fail("pass --keep-device: only the browser version changes and the lane stays the same device. A new "
              "device (deleting fingerprint.json) makes sites see a new computer.")
    home = _home()
    cfg = load_config(home)
    from agentos.core.layout import lane_pin_path, lane_specs

    specs = lane_specs(home)
    device = specs[lane].device if lane in specs else None
    path = lane_pin_path(home, lane, specs)  # a lane on a device: the device's pin (AUD-02)
    if not path.exists():
        _fail(f"{lane} has no pinned fingerprint yet ({path}); it is pinned on first launch")
    host = Host()
    if version is None:
        cache = home / ".cache"
        info = host.camoufox(cache if (cache / "camoufox").exists() else None)
        version = fingerprint.browser_major(info.get("verstr"))
        if version is None:
            _fail(f"cannot tell the installed Camoufox version ({info.get('error', 'unknown')}); pass --version")
    on_device = sorted(i for i, spec in specs.items() if device and spec.device == device)
    if device and not dry_run and not yes:
        try:
            would = fingerprint.refresh(path, lane, version, dry_run=True)
        except (OSError, ValueError, TypeError) as e:
            _fail(f"cannot read {path}: {type(e).__name__}")
        if would.changed:
            _fail(f"{lane} is on device {device}, so this changes the device's pin, for every lane on it: "
                  f"{', '.join(on_device)} (Firefox {would.old} to {would.new}; nothing else). Nothing changed. "
                  f"If you want it for all of them: agentos lane refresh-fingerprint {lane} --keep-device --yes")
    try:
        result = fingerprint.refresh(path, lane, version, dry_run=dry_run)
    except (OSError, ValueError, TypeError) as e:
        _fail(f"cannot update {path}: {type(e).__name__}")
    running = bool(host.browser_pids(cfg.profile_dir(lane)))
    if device and result.changed and not dry_run:
        raw = path.read_bytes()
        for other in on_device:  # each lane on the device keeps a copy of its pin
            if cfg.lane_dir(other).exists():
                fingerprint.write_private(cfg.fingerprint_path(other), raw)
        running = running or any(host.browser_pids(cfg.profile_dir(other)) for other in on_device)
    if as_json:
        _emit({**result.to_dict(), "running": running})
        return
    if not result.changed:
        typer.echo(f"{lane} already announces Firefox {version}; nothing to change.")
        return
    verb = "Would change" if dry_run else "Changed"
    if device:
        typer.echo(f"{lane} is on device {device}: the change is the device's, for {', '.join(on_device)}.")
    typer.echo(f"{verb} {lane}: Firefox {result.old} -> {result.new} in {', '.join(result.changed)}; every "
               "other pinned value is unchanged.")
    if result.backup:
        typer.echo(f"  the old fingerprint is kept at {result.backup}")
    if running and not dry_run:
        typer.echo("  the lane is warm: the change takes effect the next time it warms (lane_close, then open it).")


def lane_check_cmd(
    lane: Annotated[str | None, typer.Argument(help="workspace/name; leave out with --all.")] = None,
    all_lanes: Annotated[bool, typer.Option("--all", help="Check every lane on this node.")] = False,
    offline: Annotated[bool, typer.Option("--offline", help="Do not probe proxies (no network at all).")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Is this lane safe to use right now? A checklist per lane: proxy, exit IP and its location,
    WebRTC and DNS leaks, profile and pin, signed in (from the cookies already in the profile), the
    expected account, and any hold. Never warms a lane, opens a page or contacts an account's site.
    Exits 1 when any lane fails a check."""
    from agentos.stealth.lanecheck import LaneChecker, LaneReport, format_report

    if (lane is None) == (not all_lanes):
        _fail("name one lane (agentos lane check biz-a/instagram) or pass --all")
    if lane is not None:
        try:
            parse_lane_id(lane)
        except ValueError as e:
            _fail(str(e))
    home = _home()
    _node_name(home)
    payload: dict[str, Any] = {"op": "lane_check", **({"lane": lane} if lane else {})}
    resp = None if offline else Host().unix_request(ops_socket(home), payload, timeout=120)
    note = None
    if resp and resp.get("ok"):
        reports = resp["reports"]
    elif resp and isinstance(resp.get("error"), dict) and resp["error"].get("code") != "invalid":
        _fail(f"{resp['error'].get('code')}: {resp['error'].get('message')}")
    else:
        import asyncio

        checker = LaneChecker(load_config(home), network=not offline, write_cache=False)
        with _errors():
            found = asyncio.run(checker.check(lane) if lane else checker.check_all())
        reports = [r.to_dict() for r in (found if isinstance(found, list) else [found])]
        note = ("--offline: every lane was read from disk as cold, and no proxy was probed" if offline
                else "agentosd is not running: every lane was read from disk as cold")
    if as_json:
        _emit({"lanes": reports, "ok": all(r["level"] != "fail" for r in reports),
               **({"note": note} if note else {})})
    else:
        if not reports:
            typer.echo("No lanes. Create one with `agentos lane create <workspace/name>`.")
        color = sys.stdout.isatty()
        for i, r in enumerate(reports):
            if i:
                typer.echo("")
            typer.echo(format_report(LaneReport.from_dict(r), color=color))
        if note:
            typer.echo(f"\n({note})")
    raise typer.Exit(1 if any(r["level"] == "fail" for r in reports) else 0)


def lane_health_cmd(
    lane: Annotated[str | None, typer.Argument(help="workspace/name; leave out with --all.")] = None,
    all_lanes: Annotated[bool, typer.Option("--all", help="Every lane on this node.")] = False,
    days: Annotated[int, typer.Option("--days", min=1, max=365, help="Count challenges and quarantines over "
                                                                     "this many days.")] = 30,
    fresh: Annotated[bool, typer.Option("--fresh", help="Run the lane check now instead of using its last "
                                                        "run.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """One grade per lane from the checks agentos already runs: Looks normal, Check this or Stop, with
    countable facts and each finding's fix. It reports consistency, leaks and the challenges sites
    actually showed. It cannot see a site's own risk model, the exit IP's reputation or whether a
    site's terms allow automation (docs/RESPONSIBLE-USE.md). Exits 1 when any lane grades Stop."""
    from agentos.stealth import health
    from agentos.stealth.lanecheck import LaneChecker

    if (lane is None) == (not all_lanes):
        _fail("name one lane (agentos lane health biz-a/instagram) or pass --all")
    if lane is not None:
        try:
            parse_lane_id(lane)
        except ValueError as e:
            _fail(str(e))
    home = _home()
    payload: dict[str, Any] = {"op": "lane_health", "days": days, "fresh": fresh, **({"lane": lane} if lane else {})}
    resp = Host().unix_request(ops_socket(home), payload, timeout=120)  # agentosd (or agentos dev up) knows best
    note = None
    if resp and resp.get("ok"):
        found = resp["lanes"]
    elif resp and isinstance(resp.get("error"), dict) and resp["error"].get("code") != "invalid":
        _fail(f"{resp['error'].get('code')}: {resp['error'].get('message')}")
    else:
        import asyncio

        _node_name(home)
        checker = LaneChecker(load_config(home), write_cache=False)
        with _errors():
            made = asyncio.run(health.gather(checker, lane, days=days, fresh=True) if lane
                               else health.gather_all(checker, days=days, fresh=True))
        found = [h.to_dict() for h in (made if isinstance(made, list) else [made])]
        note = "agentosd is not running: every lane was read from disk as cold, and checked now"
    if as_json:
        _emit({"lanes": found, "days": days, "cannot_see": list(health.CANNOT_SEE), "docs": health.DOCS,
               **({"note": note} if note else {})})
    else:
        if not found:
            typer.echo("No lanes. Create one with `agentos lane create <workspace/name>`.")
        for i, h in enumerate(found):
            if i:
                typer.echo("")
            typer.echo(health.format_health(h))
        if found:
            typer.echo(f"\nWhat this cannot see: {' '.join(health.CANNOT_SEE)} See {health.DOCS}.")
        if note:
            typer.echo(f"\n({note})")
    raise typer.Exit(1 if any(h["grade"] == health.STOP for h in found) else 0)


def lane_audit_cmd(as_json: JsonOpt = False) -> None:
    """Does every lane pin one consistent, ordinary device, and do any two lanes pin the same one?
    Reads the pinned fingerprints on disk only: no browser starts and nothing is sent (safe on a
    node with live lanes). Exits 1 when a pin fails. docs/design/stealth.md lists the rules."""
    from agentos.stealth import stealth

    home = _home()
    _node_name(home)
    cache = home / ".cache"
    installed = fingerprint.browser_major(Host().camoufox(cache if (cache / "camoufox").exists() else None)
                                          .get("verstr"))
    findings, count = stealth.audit_pins(home, installed_major=installed)
    level = stealth.worst(findings)
    if as_json:
        _emit({"level": level, "pins": count, "installed_firefox": installed,
               "findings": [f.to_dict() for f in findings]})
    else:
        typer.echo(f"{count} pinned lanes, installed Firefox {installed or 'unknown'}: {level}")
        for line in stealth.format_findings(findings):
            typer.echo(line)
    if level == "fail":
        raise typer.Exit(1)


def stealth_check_cmd(
    slot: Annotated[int, typer.Option(help="Test slot (11 to 59): displays :S0 to :S9, VNC ports 5000 + 10S, viewer "
                                           "ports 6000 + 10S, as scripts/node-test.sh hands them out.")],
    lanes: Annotated[int, typer.Option(min=1, max=8, help="Scratch lanes to open.")] = 3,
    relaunches: Annotated[int, typer.Option(min=0, help="Relaunches of each lane after the first.")] = 2,
    os_names: Annotated[str, typer.Option("--os", help="Identity OSes to cycle over the lanes, comma separated "
                                                       "(windows, macos, linux); empty: the node default, lane_os (Linux).")] = "",
    soak_minutes: Annotated[float, typer.Option("--soak-minutes", help="Cycle the lanes for this long, killing a "
                                                                      "browser now and then (0: no soak).")] = 0.0,
    kill_every: Annotated[float, typer.Option("--kill-every", help="Seconds between kills in the soak.")] = 60.0,
    public: Annotated[bool, typer.Option("--public", help="Also visit public fingerprint test pages (CreepJS, "
                                                          "Sannysoft, BrowserScan, Pixelscan) from the first "
                                                          "lane; screenshots go in --out.")] = False,
    proxy: Annotated[str | None, typer.Option(help="A test proxy for every scratch lane, host:port[:user[:pass]] "
                                                   "(never a production lane's).")] = None,
    min_available_mb: Annotated[int, typer.Option("--min-available-mb", help="Stop before memory available on "
                                                                             "this machine falls under this.")] = 2048,
    out: Annotated[Path | None, typer.Option(help="Folder for report.json, report.txt, every dump and the "
                                                  "public pages' screenshots.")] = None,
    home_root: Annotated[Path | None, typer.Option("--home-root", help="Where the throwaway AGENTOS_HOME is made "
                                                                       "(default: the system temp folder).")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Open scratch lanes (no account) in a throwaway home on a test slot and check, the way a page's
    own script sees them: automation traces, one consistent device, the same device on every launch,
    no two lanes alike, no WebRTC leak; plus memory per warm lane, wake times and, with --soak-minutes,
    lanes cycling while browsers are killed. Never touches this node's own lanes, home or ports.
    Exits 1 when a check fails. docs/design/stealth.md says what each finding means."""
    import asyncio

    from agentos.commands.base import parse_proxy
    from agentos.stealth import stealthcheck

    if not sys.platform.startswith("linux"):
        _fail("the stealth check runs on a Linux node (it starts Xvfb and Camoufox)")
    if shutil.which("Xvfb") is None:
        _fail("Xvfb is not installed")
    root = home_root or Path(tempfile.gettempdir())
    try:  # every node on this machine (agentosd's home, AGENTOS_HOME, /var/lib/agentos) keeps its slots
        test_slot = stealthcheck.slot(slot, stealthcheck.node_configs(), home=root)
    except ValueError as e:
        _fail(str(e))
    names = [n.strip() for n in os_names.split(",") if n.strip()]
    unknown = sorted(set(names) - {"windows", "macos", "linux"})
    if unknown:
        _fail(f"unknown OS {', '.join(unknown)}: use windows, macos or linux")
    test_proxy = parse_proxy("stealth-test", proxy) if proxy else None
    home = Path(tempfile.mkdtemp(prefix=stealthcheck.SCRATCH_PREFIX, dir=root))
    stealthcheck.claim_home(home)
    opts = stealthcheck.Options(lanes=lanes, relaunches=relaunches, os_names=names, public=public,
                                soak_minutes=soak_minutes, kill_every=kill_every,
                                min_available_mb=min_available_mb, out=out)
    lab = stealthcheck.NodeLab(home, test_slot, proxy=test_proxy)

    def say(line: str) -> None:
        if as_json:
            return
        with contextlib.suppress(OSError):  # a closed terminal must not stop the lanes from closing
            typer.echo(line, err=True)

    try:
        with _errors():
            report = asyncio.run(stealthcheck.run_until_stopped(lab, opts, say=say))
    except stealthcheck.Stopped as e:
        say(f"{e}; the scratch lanes are closed")
        raise typer.Exit(128 + e.signum) from None
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if as_json:
        _emit(report.to_dict())
    else:
        typer.echo(stealthcheck.format_report(report))
    if report.level == "fail":
        raise typer.Exit(1)


def redteam_cmd(
    local: Annotated[bool, typer.Option("--local", help="Plain Camoufox on this machine (macOS works): the agent "
                                                        "input path, not the node's pins, Xvfb or proxy.")] = False,
    slot: Annotated[int | None, typer.Option(help="Node test slot (11 to 59) for scratch lanes with the "
                                                  "production engine and pins (Linux node).")] = None,
    lanes: Annotated[int, typer.Option(min=1, max=8, help="Lanes, each its own device.")] = 1,
    runs: Annotated[int, typer.Option(min=1, max=50, help="Visits per lane (pooled detectors want 3 or more).")] = 3,
    os_names: Annotated[str, typer.Option("--os", help="Identity OSes to cycle over the lanes, comma separated "
                                                       "(windows, macos, linux); empty: the default.")] = "",
    humans: Annotated[list[Path] | None, typer.Option("--humans", help="Human baseline sessions (files or folders "
                                                                       "from --serve) to compare with and to "
                                                                       "check the detectors against.")] = None,
    serve: Annotated[int | None, typer.Option("--serve", help="Serve the detector site on this port for a person "
                                                              "to record a baseline; Ctrl-C saves it.")] = None,
    host: Annotated[str, typer.Option(help="Address for --serve (0.0.0.0 to reach it from another device).")]
    = "127.0.0.1",
    probe: Annotated[bool | None, typer.Option("--probe/--no-probe", help="Read the stealth probe page in each "
                                                                         "lane (default: on a node, off with "
                                                                         "--local, where the window is not "
                                                                         "placed as the engine places it).")]
    = None,
    gauntlet: Annotated[bool, typer.Option("--gauntlet", help="Instead of our detector site: one scratch lane "
                                                              "through --proxy-file visits public bot-detection "
                                                              "panels and sites behind Cloudflare, DataDome, Akamai "
                                                              "and F5 (docs/design/redteam.md).")] = False,
    targets: Annotated[str, typer.Option(help="Gauntlet target kinds or names, comma separated: panel, tls, protected, "
                                              "content (default: all).")] = "",
    proxy_file: Annotated[Path | None, typer.Option("--proxy-file", help="A file holding host:port[:user[:pass]] "
                                                                        "(kept out of the command line).")] = None,
    no_record: Annotated[bool, typer.Option("--no-record", help="Do not append to bench/redteam.jsonl.")] = False,
    out: Annotated[Path | None, typer.Option(help="Folder for report.json, report.txt and every session.")] = None,
    home_root: Annotated[Path | None, typer.Option("--home-root", help="Where the node's throwaway AGENTOS_HOME is "
                                                                       "made (default: the system temp folder).")]
    = None,
    as_json: JsonOpt = False,
) -> None:
    """Try to catch our own lanes as bots. Lanes visit a detector site the way an agent drives them
    (navigate, snapshot, act), and about 30 detectors judge what a vendor's sensor and server would
    see: keystroke and mouse dynamics, scrolling, focus, navigation and headers, claimed versus
    measured hardware, the stealth probe, and lanes linked to each other. Exits 1 when a detector
    catches a visit. docs/design/redteam.md says what each detector means."""
    import asyncio

    from agentos.stealth.redteam import runner
    from agentos.stealth.redteam.server import DetectorServer

    if serve is not None:
        _redteam_serve(serve, host, humans[0] if humans else Path("bench/redteam-humans"))
        return
    if local == (slot is not None):
        _fail("pick one: --local (Camoufox on this machine) or --slot S (scratch lanes on this node)")
    names = [n.strip() for n in os_names.split(",") if n.strip()]
    unknown = sorted(set(names) - {"windows", "macos", "linux"})
    if unknown:
        _fail(f"unknown OS {', '.join(unknown)}: use windows, macos or linux")
    opts = runner.Options(lanes=lanes, runs=runs, os_names=names, humans=runner.load_humans(humans or []),
                          probe=(not local) if probe is None else probe, out=out)

    def say(line: str) -> None:
        if not as_json:
            with contextlib.suppress(OSError):
                typer.echo(line, err=True)

    test_proxy = None
    if proxy_file is not None:
        from agentos.commands.base import parse_proxy
        try:
            test_proxy = parse_proxy("redteam-test", proxy_file.read_text().strip().splitlines()[0])
        except (OSError, IndexError) as e:
            _fail(f"cannot read the proxy file: {e}")
    if gauntlet and test_proxy is None:
        _fail("the gauntlet visits public sites: give it a proxy (--proxy-file), never the node's own IP")
    if not gauntlet and test_proxy is not None:
        _fail("our detector site is on loopback, which a proxied lane cannot reach; use --proxy-file with --gauntlet")
    home: Path | None = None
    if local:
        lab: Any = runner.LocalLab(proxy=test_proxy)
    else:
        from agentos.stealth import stealthcheck
        if not sys.platform.startswith("linux") or shutil.which("Xvfb") is None:
            _fail("--slot runs on a Linux node with Xvfb; use --local elsewhere")
        root = home_root or Path(tempfile.gettempdir())
        try:
            test_slot = stealthcheck.slot(slot, stealthcheck.node_configs(), home=root)
        except ValueError as e:
            _fail(str(e))
        home = Path(tempfile.mkdtemp(prefix=stealthcheck.SCRATCH_PREFIX, dir=root))
        stealthcheck.claim_home(home)
        lab = runner.NodeBench(stealthcheck.NodeLab(home, test_slot, proxy=test_proxy))
    if gauntlet:
        kinds = {k.strip() for k in targets.split(",") if k.strip()} or None
        try:
            with _errors():
                result = asyncio.run(runner.run_gauntlet(lab, kinds=kinds, out=out, say=say))
        finally:
            if home is not None:
                shutil.rmtree(home, ignore_errors=True)
        if as_json:
            _emit({k: v for k, v in result.items() if k != "text"})
        else:
            typer.echo(result["text"])
        return
    try:
        with _errors():
            report = asyncio.run(runner.run(lab, opts, say=say, server=DetectorServer().start()))
    finally:
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)
    changes = [] if no_record else runner.compare(runner.record(report), report)
    if as_json:
        _emit({**report.to_dict(), "changes": changes})
    else:
        typer.echo(runner.format_report(report))
        if changes:
            typer.echo("\nsince the last run:\n  " + "\n  ".join(changes))
    if report.summary()["outcome"] == "detected":
        raise typer.Exit(1)


def _redteam_serve(port: int, host: str, folder: Path) -> None:
    """Serve the detector site for a person; save each human visit as a baseline on Ctrl-C."""
    import signal
    import time as _time

    from agentos.stealth.redteam import detectors
    from agentos.stealth.redteam.server import DetectorServer

    server = DetectorServer(host, port).start()
    sid = f"human-{_time.strftime('%Y%m%d-%H%M%S')}"
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    typer.echo(f"Open http://{shown}:{port}/?run={sid}&mode=human in your own browser and follow the yellow "
               "box. Press Ctrl-C here when you are back on the home page.")
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()
    _time.sleep(0.5)
    server.stop()
    folder.mkdir(parents=True, exist_ok=True)
    saved = 0
    for name in server.store.ids():
        data = server.store.session(name)
        if not data or not data.get("pages"):
            continue
        data["human"] = True
        (folder / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False))
        saved += 1
        typer.echo(f"\nsaved {folder / (name + '.json')}; what the detectors say about this person:")
        for v in detectors.judge(data):
            if v.level != detectors.SKIPPED:
                typer.echo(f"  {v.level:10} {v.id:24} {v.detail}")
    if not saved:
        typer.echo("nothing was recorded (did the page load from this server?)")
