"""Devices (AUD-02): one pinned fingerprint plus one proxy, shared by several lanes,
like one office PC with a browser profile per account.

Why: a site links accounts that log in from one computer, and also notices fifteen different
computers on one IP. The owner's plan is one device per company (its lanes share the device's
fingerprint and its static ISP proxy), and personal accounts on a device of their own.

What a device is on disk:

    devices/<name>/fingerprint.json   the device's pin: the one its lanes launch with (600)
    agentos.db, table devices         name, proxy label, where its pin came from

Each lane on a device keeps its own profile (cookies, storage, history), its own display, its
own systemd scope and its own browser process, and launches with the device's pin and proxy.
Its own `fingerprint.json` is kept as a byte-for-byte copy of the device's pin, so everything
that reads a lane's pin by its id (the timezone for active hours, `agentos lane check`, keepalive,
a backup, an older agentos after a rollback) reads the device's.

The rule that matters: a lane that a site has seen keeps the device it was seen on. A new
fingerprint is a new computer to the site, which is how checkpoints start. So:

- a lane made with `--device` takes the device's pin (or makes it, on the device's first launch);
- `agentos device create NAME --from-lane WS/NAME` makes a device from a lane's pin, so the
  company's other lanes join the computer the site already knows, with no change for that lane;
- moving a lane that has its own pin onto a device (or off one) is a new computer to its site.
  It is explicit (`agentos lane set-device`), says so plainly, and needs `--yes` (confirm).

Launches on one device are serialised (a per-device lock), so the first launch makes the pin
once, and a Firefox version move or a pin completion is written once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agentos.core import fs
from agentos.core.errors import ConfirmRequired, InvalidRequest, NotFound
from agentos.core.models import DeviceSpec, LaneSpec, device_name_problem

log = logging.getLogger("agentos.devices")

PIN_META = "agentos:pin"  # agentos.node.engine_camoufox.PIN_META
BEFORE_DEVICE = "before-device"  # fingerprint.json.before-device.bak: the lane's own pin before it joined


def read_pin_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


PinStatus = Literal["pinned", "absent", "unreadable", "conflict"]


@dataclass(frozen=True)
class PinState:
    """What a pin file (or a device with its lanes' copies) holds, read once by everything that
    plans or writes a pin, so a plan and the write after it can never disagree:

    - pinned: readable, `raw` is the bytes and `data` the settings; `source` says where they came
      from (the file itself, or a device's lanes' copies when the device's own file is gone);
    - absent: no pin anywhere, so the first launch makes one;
    - unreadable: a file is there but holds no readable pin (a torn write, an empty file);
    - conflict: a device without its file whose lanes hold copies that disagree.

    Unreadable and conflict are refused by every write: the computer a site knows is not drawn
    again or overwritten on a guess (AUD-02)."""
    status: PinStatus
    raw: bytes | None = None
    data: dict[str, Any] | None = None
    source: str | None = None
    detail: str = ""

    @property
    def pinned(self) -> bool:
        return self.status == "pinned"


def pin_state(path: Path) -> PinState:
    """One pin file: pinned, absent or unreadable (never None for a torn file)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return PinState("absent")
    except OSError as e:
        return PinState("unreadable", detail=f"{path} cannot be read ({type(e).__name__})")
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not data:
        return PinState("unreadable", detail=f"{path} holds no readable pin (a torn or empty write)")
    return PinState("pinned", raw=raw, data=data, source=str(path))


def pin_summary(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """What a person needs to recognise a pinned device: OS, screen, GPU, timezone, locale,
    Firefox version, and where the pin was made. None: not pinned yet."""
    if not data:
        return None
    from agentos.stealth.fingerprint import pinned_major
    from agentos.stealth.stealth import platform_os, ua_os

    config = {k: v for k, v in data.items() if k != PIN_META}
    meta = data.get(PIN_META) if isinstance(data.get(PIN_META), dict) else {}
    geo = meta.get("geo") if isinstance(meta.get("geo"), dict) else {}
    width, height = config.get("screen.width"), config.get("screen.height")
    language, region = config.get("locale:language"), config.get("locale:region")
    locale = "-".join(str(p) for p in (language, region) if p) or None
    return {
        "os": ua_os(config.get("navigator.userAgent")) or platform_os(config.get("navigator.platform")),
        "screen": f"{width}x{height}" if width and height else None,
        "window": (f"{config.get('window.outerWidth')}x{config.get('window.outerHeight')}"
                   if config.get("window.outerWidth") else None),
        "gpu": config.get("webGl:renderer"),
        "timezone": config.get("timezone"),
        "locale": locale,
        "firefox": pinned_major(config),
        "pinned_at": meta.get("pinned_at"),
        "pinned_exit_ip": geo.get("ip"),
        "pinned_country": geo.get("country"),
    }


def same_pin(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _write_private(path: Path, raw: bytes) -> None:
    from agentos.node.engine_camoufox import write_private_bytes

    write_private_bytes(path, raw)


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def valid_device_name(name: str) -> str:
    if not isinstance(name, str):
        raise InvalidRequest(f"invalid device name {name!r}")
    problem = device_name_problem(name)
    if problem:
        raise InvalidRequest(problem)
    return name


class DevicesMixin:
    """Device operations of the lane manager (agentos.node.lanes.LaneManagerImpl)."""

    # ---- lookups ------------------------------------------------------------------------------

    def _device_lock(self, name: str) -> asyncio.Lock:
        locks: dict[str, asyncio.Lock] = self.__dict__.setdefault("_device_locks", {})
        return locks.setdefault(name, asyncio.Lock())

    def _require_device(self, name: str) -> DeviceSpec:
        valid_device_name(name)
        device = self.store.device(name)
        if device is None:
            raise NotFound(f"no device named {name}. Make it with: agentos device create {name} --proxy <label>")
        return device

    def pin_path_for(self, spec: LaneSpec) -> Path:
        """The pin a lane launches with: its device's, or its own."""
        if spec.device:
            return self.config.device_pin_path(spec.device)
        return self.config.fingerprint_path(spec.id)

    def _device_lanes(self, name: str) -> list[LaneSpec]:
        return [rec.spec for rec in self.store.list() if rec.spec.device == name]

    def _refuse_other_company(self, device: DeviceSpec, lane_id: str) -> None:
        """One device is one company's computer: a lane of another workspace on it links that
        company's accounts to this one's at every site that sees both. Personal is a company of
        its own."""
        workspace = lane_id.split("/", 1)[0]
        owners = {s.id.split("/", 1)[0] for s in self._device_lanes(device.name) if s.id != lane_id}
        if device.workspace:
            owners.add(device.workspace)
        others = sorted(owners - {workspace})
        if others:
            raise InvalidRequest(f"device {device.name} is {', '.join(others)}'s computer, so a lane of {workspace} "
                                 f"on it would link the two workspaces' accounts at every site that sees both. Give "
                                 f"{workspace} a device of its own: agentos device create <name> --proxy <label>")

    def _claim_device(self, name: str, lane_id: str) -> None:
        device = self.store.device(name)
        if device is not None and device.workspace is None:
            self.store.update_device(device.model_copy(update={"workspace": lane_id.split("/", 1)[0]}))

    def _refuse_shared_proxy(self, name: str, proxy_label: str | None, from_lane: str | None,
                             confirm: bool) -> None:
        """One proxy is one computer's exit: a second device behind it, or a device beside lanes
        that are computers of their own, is several computers on one IP, an office the sites
        never saw together."""
        if proxy_label is None:
            return
        devices = sorted(d.name for d in self.store.devices() if d.proxy_label == proxy_label and d.name != name)
        if devices:
            raise InvalidRequest(f"proxy {proxy_label} is already the exit of device {', '.join(devices)}: two "
                                 "computers on one IP look like an office the sites never saw together. Give "
                                 f"device {name} a proxy of its own, or put its lanes on {devices[0]} (one company "
                                 "per device)")
        own = sorted(r.spec.id for r in self.store.list()
                     if r.spec.device is None and r.spec.proxy_label == proxy_label and r.spec.id != from_lane)
        if not own:
            return
        if from_lane is None:
            raise InvalidRequest(f"proxy {proxy_label} is already the exit of {', '.join(own)}, each a computer of "
                                 "its own. Make the device from the lane its site knows, so nothing changes for it: "
                                 f"agentos device create {name} --from-lane {own[0]}. Or give the device another "
                                 "proxy")
        if not confirm:
            raise ConfirmRequired(
                f"{', '.join(own)} also leave through proxy {proxy_label} with a computer of their own, so after "
                f"this the sites see {len(own) + 1} computers on one IP. Move them onto device {name} (agentos lane "
                "set-device) or to another proxy. Pass --yes to make the device anyway",
                details={"proxy": proxy_label, "lanes": own, "confirm": True})

    def device_info(self, device: DeviceSpec | str) -> dict[str, Any]:
        if isinstance(device, str):
            device = self._require_device(device)
        state = self.device_pin_state(device.name)
        pin = state.data
        lanes = []
        for spec in self._device_lanes(device.name):
            info = self.get(spec.id)
            lanes.append({"id": spec.id, "state": info.state.value, "proxy": spec.proxy_label,
                          "pin_copy": same_pin(self.config.fingerprint_path(spec.id),
                                               self.config.device_pin_path(device.name))})
        return {"name": device.name, "proxy": device.proxy_label, "created_at": device.created_at.isoformat(),
                "from_lane": device.from_lane, "note": device.note, "pinned": state.pinned,
                "pin_status": state.status, "pin_problem": state.detail or None,
                "pin": pin_summary(pin), "lanes": lanes,
                "workspaces": sorted({lane["id"].split("/", 1)[0] for lane in lanes})}

    def devices(self) -> list[dict[str, Any]]:
        return [self.device_info(d) for d in self.store.devices()]

    # ---- create and remove --------------------------------------------------------------------

    async def create_device(self, name: str, *, proxy_label: str | None = None, from_lane: str | None = None,
                            note: str | None = None, confirm: bool = False, by: str = "owner") -> dict[str, Any]:
        """A new device. With `from_lane`, the device takes that lane's pin and proxy and the lane
        joins it: nothing changes for the lane or its site. Without, the device is pinned on the
        first launch of any lane made on it, from the proxy's exit."""
        valid_device_name(name)
        async with self._device_lock(name):
            return self._create_device_locked(name, proxy_label=proxy_label, from_lane=from_lane, note=note,
                                              confirm=confirm, by=by)

    def _create_device_locked(self, name: str, *, proxy_label: str | None, from_lane: str | None,
                              note: str | None, confirm: bool, by: str) -> dict[str, Any]:
        if self.store.device(name) is not None:
            raise InvalidRequest(f"device {name} already exists (agentos device show {name})")
        if proxy_label is not None and proxy_label not in self.proxies_loader():
            raise InvalidRequest(f"unknown proxy {proxy_label!r}. Add it first with: agentos proxy add {proxy_label} "
                                 "host:port:user:pass")
        pin_dir = self.config.device_dir(name)
        if pin_dir.exists() and any(not fs.is_temp(p) for p in pin_dir.iterdir()):
            raise InvalidRequest(f"{pin_dir} already holds files of an earlier device {name}; move them away first")
        source = None
        source_state = PinState("absent")
        if from_lane is not None:
            rec = self.store.require(from_lane)
            if rec.spec.device:
                raise InvalidRequest(f"{from_lane} is already on device {rec.spec.device}")
            source = self.config.fingerprint_path(from_lane)
            source_state = pin_state(source)
            self.refuse_unsafe_pin(source_state, f"device {name} was not made from {from_lane}")
            if not source_state.pinned:
                raise InvalidRequest(f"{from_lane} has no pinned fingerprint yet (it is pinned on its first launch), "
                                     f"so there is no device to share. Make the device with --proxy instead and "
                                     f"create the lanes with --device {name}")
            if proxy_label is not None and proxy_label != rec.spec.proxy_label:
                raise InvalidRequest(f"{from_lane} leaves through proxy {rec.spec.proxy_label or 'none'}; a device "
                                     f"made from it keeps that proxy (another one is a new IP for its site). Leave "
                                     "out --proxy")
            self._refuse_busy(from_lane, "make a device from")
            proxy_label = rec.spec.proxy_label
        self._refuse_shared_proxy(name, proxy_label, from_lane, confirm)
        device = DeviceSpec(name=name, proxy_label=proxy_label, from_lane=from_lane, note=note,
                            workspace=from_lane.split("/", 1)[0] if from_lane else None)
        self.store.add_device(device)  # first: a name taken meanwhile fails here, before any file is written
        try:
            _private_dir(self.config.devices_dir)
            _private_dir(pin_dir)
            if source is not None:
                _write_private(self.config.device_pin_path(name), source_state.raw or b"")
            if from_lane is not None:
                rec = self.store.require(from_lane)
                self.store.update_spec(rec.spec.model_copy(update={"device": name}))
        except BaseException:
            self.store.delete_device(name)
            self.config.device_pin_path(name).unlink(missing_ok=True)
            raise
        if from_lane is not None:
            self._event(from_lane, "device_joined", {"device": name, "by": by, "pin": "kept (the device took it)"})
        log.info("created device %s (proxy %s%s)", name, proxy_label or "none",
                 f", pin from {from_lane}" if from_lane else "")
        return self.device_info(device)

    async def remove_device(self, name: str, *, by: str = "owner") -> Path | None:
        """Remove a device no lane uses. Its directory moves to the trash, never deleted."""
        self._require_device(name)
        async with self._device_lock(name):
            return self._remove_device_locked(name, by=by)

    def _refuse_device_in_use(self, name: str) -> None:
        using = [s.id for s in self._device_lanes(name)]
        if using:
            raise InvalidRequest(f"device {name} is used by {', '.join(using)}; move or delete those lanes first")

    def _remove_device_locked(self, name: str, *, by: str) -> Path | None:
        self._require_device(name)
        self._refuse_device_in_use(name)
        src = self.config.device_dir(name)
        moved = None
        if src.exists():
            _private_dir(self.trash_dir)
            moved = self.trash_dir / f"device--{name}--{self.now().strftime('%Y%m%dT%H%M%SZ')}"
            n = 1
            while moved.exists():
                n += 1
                moved = moved.with_name(f"{moved.name}-{n}")
            os.rename(src, moved)
        try:
            self._refuse_device_in_use(name)  # a lane made on it meanwhile (another process) keeps it
        except InvalidRequest:
            if moved is not None:
                os.rename(moved, src)
            raise
        self.store.delete_device(name)
        log.info("removed device %s (by %s)%s", name, by, f"; its files are in {moved}" if moved else "")
        return moved

    # ---- lanes on devices ---------------------------------------------------------------------

    def _join_new_lane(self, spec: LaneSpec) -> LaneSpec:
        """A new lane on a device: the device's proxy, and a copy of its pin when it has one."""
        device = self._require_device(spec.device or "")
        self._refuse_other_company(device, spec.id)
        if spec.proxy_label and spec.proxy_label != device.proxy_label:
            raise InvalidRequest(f"device {device.name} leaves through proxy {device.proxy_label or 'none'}; a lane "
                                 "on it uses the device's proxy. Leave out --proxy")
        return spec.model_copy(update={"proxy_label": device.proxy_label})

    def device_pin_state(self, name: str) -> PinState:
        """The device's pin: its own file when there is one (pinned or unreadable). Without it, the
        copies its lanes hold: one pin they all agree on is the device's (pinned, from those
        copies), copies that disagree are a conflict, an unreadable copy is unreadable, and no copy
        at all is absent (the first launch makes the pin)."""
        own = pin_state(self.config.device_pin_path(name))
        if own.status != "absent":
            return own
        copies: dict[bytes, list[str]] = {}
        torn: list[str] = []
        for spec in self._device_lanes(name):
            state = pin_state(self.config.fingerprint_path(spec.id))
            if state.status == "pinned" and state.raw is not None:
                copies.setdefault(state.raw, []).append(spec.id)
            elif state.status == "unreadable":
                torn.append(spec.id)
        if len(copies) > 1:
            groups = "; ".join(", ".join(lanes) for lanes in copies.values())
            return PinState("conflict", detail=f"device {name} has no pin file and its lanes hold copies that "
                                               f"disagree ({groups})")
        if torn:
            return PinState("unreadable", detail=f"device {name} has no pin file and the copy of "
                                                 f"{', '.join(torn)} cannot be read")
        if copies:
            [(raw, lanes)] = copies.items()
            return PinState("pinned", raw=raw, data=json.loads(raw), source=f"copies:{','.join(lanes)}")
        return PinState("absent")

    def refuse_unsafe_pin(self, state: PinState, what: str) -> None:
        if state.status in ("unreadable", "conflict"):
            raise InvalidRequest(f"{what}: {state.detail}. Nothing was changed and no new fingerprint was drawn, "
                                 "because that would be a new computer to the sites that know this one. Put the "
                                 "right pin back (a backup, or one of the lanes' fingerprint.json copies), then "
                                 "try again")

    def ensure_device_pin(self, spec: LaneSpec) -> None:
        """Before a device lane launches: a device whose pin file is gone (an older backup, a hand
        edit) while any of its lanes still holds a copy gets that copy back, so the launch shows
        the computer the sites know instead of drawing a new one. Copies that disagree, or a pin
        that cannot be read, refuse the launch: nothing picks one on a guess, nothing draws anew."""
        if not spec.device:
            return
        state = self.device_pin_state(spec.device)
        self.refuse_unsafe_pin(state, f"{spec.id} was not launched")
        if state.source is None or not state.source.startswith("copies:") or state.raw is None:
            return
        device_pin = self.config.device_pin_path(spec.device)
        _private_dir(device_pin.parent)
        _write_private(device_pin, state.raw)
        lanes = state.source.split(":", 1)[1]
        log.warning("device %s had no pin; took it back from the copy of %s, the computer its sites know",
                    spec.device, lanes)
        self._event(spec.id, "device_pin_restored", {"device": spec.device, "from": lanes.split(",")})

    def sync_device_pins(self, name: str) -> list[str]:
        """Make every lane's copy of the device's pin match it. Returns the lanes updated."""
        state = pin_state(self.config.device_pin_path(name))
        if not state.pinned or state.raw is None:
            return []  # nothing, or nothing readable: a torn pin is never copied over a lane's
        raw = state.raw
        updated = []
        for spec in self._device_lanes(name):
            dest = self.config.fingerprint_path(spec.id)
            if not dest.parent.exists():
                continue
            try:
                if dest.exists() and dest.read_bytes() == raw:
                    continue
                _write_private(dest, raw)
                updated.append(spec.id)
            except OSError as e:
                log.warning("%s: could not copy device %s's pin: %s", spec.id, name, type(e).__name__)
        return updated

    def plan_device_move(self, lane_id: str, device: str | None) -> dict[str, Any]:
        """What moving a lane onto `device` (None: off its device) changes for the lane's site,
        in plain words, before anything is changed. `confirm` says whether it needs --yes."""
        spec = self.store.require(lane_id).spec
        target = self._require_device(device) if device else None
        old = spec.device
        lane_state = pin_state(self.config.fingerprint_path(lane_id))
        self.refuse_unsafe_pin(lane_state, f"{lane_id} was not moved: its own pin")
        own_pin = lane_state.data
        plan: dict[str, Any] = {"lane": lane_id, "from": old, "to": device, "changes": [], "warnings": [],
                                "confirm": False, "proxy": {"from": spec.proxy_label, "to": spec.proxy_label}}
        if old == device:
            plan["changes"].append(f"{lane_id} is already on {device or 'its own device'}; nothing changes")
            return plan
        if target is not None:
            plan["proxy"]["to"] = target.proxy_label
            self._refuse_other_company(target, lane_id)
            device_state = self.device_pin_state(target.name)
            self.refuse_unsafe_pin(device_state, f"{lane_id} was not moved onto device {target.name}")
            device_pin = device_state.data
            if device_pin is None and own_pin is not None and old is None:
                plan["changes"].append(f"device {target.name} has no pin yet, so it takes {lane_id}'s pin: the "
                                       "lane stays the same computer to its site")
            elif own_pin is None:
                plan["changes"].append(f"{lane_id} has never launched, so its site has not seen it: it starts on "
                                       f"device {target.name}")
            elif device_pin is not None and json.dumps(device_pin, sort_keys=True) == json.dumps(own_pin, sort_keys=True):
                plan["changes"].append(f"{lane_id} already shows device {target.name}'s fingerprint: no change")
            else:
                plan["warnings"].append(
                    f"{lane_id} will launch with device {target.name}'s fingerprint. To its site this is a new "
                    "computer: expect a re-login, a security check or a checkpoint. Its cookies stay, but a new "
                    "device on a logged-in account is what sites check hardest. Do it once, before a busy day, "
                    "and be ready to answer the check in the live view")
                plan["confirm"] = True
        else:
            backup = pin_state(self.config.fingerprint_path(lane_id).with_name(f"fingerprint.json.{BEFORE_DEVICE}.bak"))
            self.refuse_unsafe_pin(backup, f"{lane_id} was not moved off device {old}: the pin it had before")
            if own_pin is None and old is not None:
                device_state = self.device_pin_state(old)
                self.refuse_unsafe_pin(device_state, f"{lane_id} was not moved off device {old}")
                own_pin = device_state.data
            if backup.pinned:
                plan["warnings"].append(
                    f"{lane_id} leaves device {old} and gets back the fingerprint it had before it joined. To its "
                    "site this is a new computer: expect a re-login, a security check or a checkpoint")
                plan["confirm"] = True
                where = self._location_warning(backup.data or {}, spec.proxy_label)
                if where:
                    plan["warnings"].append(f"the fingerprint it gets back does not match where proxy "
                                            f"{spec.proxy_label} exits: {where}")
            elif own_pin is None:
                plan["changes"].append(f"{lane_id} has never launched: it gets its own device on its first launch")
            else:
                plan["changes"].append(
                    f"{lane_id} leaves device {old} and keeps its fingerprint as a computer of its own: the same "
                    f"computer to its site, now alone (a new one only with agentos lane refresh-fingerprint "
                    f"{lane_id})")
        if plan["proxy"]["to"] != plan["proxy"]["from"]:
            plan["warnings"].append(
                f"{lane_id} moves from proxy {plan['proxy']['from'] or 'none'} to {plan['proxy']['to'] or 'none'}: "
                "its site sees a new IP address")
            if own_pin is not None:
                plan["confirm"] = True
        return plan

    async def set_device(self, lane_id: str, device: str | None, *, confirm: bool = False,
                         by: str = "owner") -> dict[str, Any]:
        """Move a cold lane onto a device (None: off its device). A move its site would notice
        (a new fingerprint or a new IP on a lane that has launched) needs confirm=True; without
        it this raises ConfirmRequired with the plan, and nothing changes. Holds the lock of each
        device it touches (a launch on it waits, and the plan is read under the lock)."""
        current = self.store.require(lane_id).spec.device
        async with contextlib.AsyncExitStack() as stack:
            for name in sorted({n for n in (current, device) if n}):
                await stack.enter_async_context(self._device_lock(name))
            if self.store.require(lane_id).spec.device != current:
                raise InvalidRequest(f"{lane_id} moved to another device meanwhile; look again and retry")
            return self._set_device_locked(lane_id, device, confirm=confirm, by=by)

    def _set_device_locked(self, lane_id: str, device: str | None, *, confirm: bool, by: str) -> dict[str, Any]:
        plan = self.plan_device_move(lane_id, device)
        if plan["from"] == plan["to"]:
            return plan
        if plan["confirm"] and not confirm:
            raise ConfirmRequired(" ".join(w + "." for w in plan["warnings"]) + " Pass --yes to go ahead.",
                                  details=plan)
        self._refuse_busy(lane_id, "move to another device")
        rec = self.store.require(lane_id)
        spec = rec.spec
        lane_pin = self.config.fingerprint_path(lane_id)
        backup = lane_pin.with_name(f"fingerprint.json.{BEFORE_DEVICE}.bak")
        # The same predicate the plan read: a torn or conflicting pin was refused there.
        lane_state = pin_state(lane_pin)
        if device is not None:
            target = self._require_device(device)
            device_state = self.device_pin_state(device)
            self.refuse_unsafe_pin(device_state, f"{lane_id} was not moved onto device {device}")
            device_pin = self.config.device_pin_path(device)
            _private_dir(device_pin.parent)
            if device_state.pinned and device_state.raw is not None and not device_pin.exists():
                _write_private(device_pin, device_state.raw)  # the device's pin, back from its lanes' copies
            if device_state.status == "absent":
                if spec.device is None and lane_state.pinned and lane_state.raw is not None:
                    _write_private(device_pin, lane_state.raw)  # the device takes the lane's pin
                elif lane_state.pinned:
                    lane_pin.unlink()  # a copy of another device's pin: this device draws its own (confirmed)
            elif device_state.raw is not None and lane_state.raw != device_state.raw:
                if lane_state.pinned and spec.device is None and not backup.exists():
                    _write_private(backup, lane_state.raw or b"")
                _write_private(lane_pin, device_state.raw)
            new = spec.model_copy(update={"device": device, "proxy_label": target.proxy_label})
        else:
            if pin_state(backup).pinned:
                os.replace(backup, lane_pin)  # the computer it was before it joined (confirmed)
            elif not lane_state.pinned and spec.device:
                # No copy: it keeps the device's pin as its own, the same computer, now alone.
                device_state = self.device_pin_state(spec.device)
                if device_state.pinned and device_state.raw is not None:
                    _write_private(lane_pin, device_state.raw)
            new = spec.model_copy(update={"device": None})
        self.store.update_spec(new)
        if device is not None:
            self._claim_device(device, lane_id)
        if new.proxy_label != spec.proxy_label:
            self.store.set_exit_ip(lane_id, None)
        self._event(lane_id, "device_changed", {"from": spec.device, "to": device, "by": by,
                                                "proxy_from": spec.proxy_label, "proxy_to": new.proxy_label,
                                                "warned": plan["warnings"]})
        log.info("%s: device %s -> %s (by %s)", lane_id, spec.device or "own", device or "own", by)
        return {**plan, "done": True}

    def _location_warning(self, data: dict[str, Any], proxy_label: str | None) -> str | None:
        """The lane audit's location check (timezone, country, locale against the proxy's exit,
        from the exit-geo cache) for a pin about to be used again. None: it matches, or unknown."""
        if not proxy_label:
            return None
        from agentos.stealth.stealth import _exit_geo, location_check

        try:
            check = location_check(data, _exit_geo(self.config.home).get(proxy_label), at=self.now())
        except Exception:  # noqa: BLE001 - a warning only; the move itself is planned without it
            return None
        return check.detail.rstrip(".") if check is not None and check.level == "warn" else None

    def _refuse_device_proxy_change(self, spec: LaneSpec, label: str | None) -> None:
        if spec.device and label != spec.proxy_label:
            raise InvalidRequest(f"{spec.id} is on device {spec.device}, so it uses the device's proxy. Move it off "
                                 f"the device first (agentos lane set-device {spec.id} none)")

    def repair_device_pins(self) -> None:
        """At start: every device lane's copy matches its device, and nothing is overwritten
        silently. A missing or torn copy gets the device's pin. A readable copy that differs (a
        restore, a hand edit, an older agentos after a rollback) is compared by the pin's version,
        then by when it was written: the newer one is the device's pin, the older one is kept
        beside it as `fingerprint.json.replaced*.bak`, an event says so on the lane, and `agentos
        lane audit` lists the kept file until the owner removes it. Newest copies that disagree
        with each other are left as they are, for the owner (the audit fails them)."""
        for device in self.store.devices():
            try:
                self._repair_device(device.name)
            except OSError as e:
                log.warning("device %s: could not check its lanes' pin copies: %s", device.name, type(e).__name__)

    def _repair_device(self, name: str) -> None:
        state = self.device_pin_state(name)
        if state.status in ("unreadable", "conflict"):
            log.warning("%s; its lanes are not launched until the right pin is back", state.detail)
            return
        if not state.pinned:
            return
        device_pin = self.config.device_pin_path(name)
        if not device_pin.exists() and state.raw is not None:  # the copies agree: the device's pin is back
            _private_dir(device_pin.parent)
            _write_private(device_pin, state.raw)
            log.warning("device %s had no pin; took it back from its lanes' copies", name)
        current = pin_state(device_pin)
        if not current.pinned or current.raw is None:
            return
        newer: dict[bytes, list[str]] = {}
        for spec in self._device_lanes(name):
            copy = self.config.fingerprint_path(spec.id)
            got = pin_state(copy)
            if got.pinned and got.raw != current.raw and _newness(copy, got) > _newness(device_pin, current):
                newer.setdefault(got.raw or b"", []).append(spec.id)
        if len(newer) > 1:
            groups = "; ".join(", ".join(lanes) for lanes in newer.values())
            log.warning("device %s: its lanes hold newer pins that disagree (%s); left as they are for the owner "
                        "(agentos lane audit)", name, groups)
            for lanes in newer.values():
                for lane_id in lanes:
                    self._event(lane_id, "device_pin_conflict", {"device": name, "groups": groups})
            return
        if newer:
            [(raw, lanes)] = newer.items()
            kept = _keep_replaced(device_pin)
            _write_private(device_pin, raw)
            log.warning("device %s: %s held a newer pin than the device; it is the device's pin now, and the "
                        "older one is kept at %s", name, ", ".join(lanes), kept)
            for lane_id in lanes:
                self._event(lane_id, "device_pin_replaced", {"device": name, "kept": str(kept)})
            current = pin_state(device_pin)
        for spec in self._device_lanes(name):
            copy = self.config.fingerprint_path(spec.id)
            if not copy.parent.exists():
                continue
            got = pin_state(copy)
            if got.raw == current.raw:
                continue
            kept = _keep_replaced(copy) if got.pinned else None
            _write_private(copy, current.raw or b"")
            if kept is not None:
                log.warning("%s: its copy of device %s's pin was older than the device's; refreshed, and the old "
                            "copy is kept at %s", spec.id, name, kept)
                self._event(spec.id, "device_pin_copy_replaced", {"device": name, "kept": str(kept)})
            else:
                log.info("device %s: refreshed the pin copy of %s", name, spec.id)


REPLACED = "replaced"  # fingerprint.json.replaced*.bak: a pin the start-up repair replaced (agentos lane audit)


def _newness(path: Path, state: PinState) -> tuple[int, float]:
    meta = (state.data or {}).get(PIN_META)
    version = meta.get("version") if isinstance(meta, dict) else None
    try:
        written = path.stat().st_mtime
    except OSError:
        written = 0.0
    return (version if isinstance(version, int) else 0, written)


def _keep_replaced(path: Path) -> Path:
    from agentos.node.engine_camoufox import backup_pin

    return backup_pin(path, 0, label=REPLACED)
