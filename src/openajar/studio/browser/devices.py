"""Named phones and tablets for a review page: a width an agent can name ("iphone", "pixel")
instead of a number. A device sets the viewport, the scale, touch, the mobile flag and the user
agent from Playwright's device descriptors, and renders on the engine the real device uses (WebKit
for an iPhone or iPad, Chromium for an Android phone).

The pool honours a device or refuses the call; it never opens a plain page in its place. The
viewport and scale are kept here too, so a screen can be named before a browser starts (a capture
names its screens first); the descriptor read at page time wins, and a difference is reported.
"""

from __future__ import annotations

from dataclasses import dataclass

from openajar.core.errors import InvalidRequest
from openajar.studio.contracts import EngineName, Screen, Theme


@dataclass(frozen=True)
class Device:
    name: str            # what an agent passes: "iphone"
    descriptor: str      # Playwright's device name: "iPhone 15"
    engine: EngineName   # the engine the real device renders with
    width: int
    height: int
    scale: float

    @property
    def label(self) -> str:
        return f"{self.name} ({self.descriptor}, {self.width} px, {self.engine.value})"

    def screen(self, theme: Theme | str = Theme.LIGHT, state: str = "default") -> Screen:
        return Screen(width=self.width, height=self.height, theme=Theme(theme), state=state, scale=self.scale)


DEVICES: dict[str, Device] = {d.name: d for d in (
    Device("iphone", "iPhone 15", EngineName.WEBKIT, 393, 659, 3.0),
    Device("iphone-se", "iPhone SE", EngineName.WEBKIT, 320, 568, 2.0),
    Device("pixel", "Pixel 7", EngineName.CHROMIUM, 412, 839, 2.625),
    Device("ipad", "iPad Mini", EngineName.WEBKIT, 768, 1024, 2.0),
)}


def device(name: str) -> Device:
    """The preset named `name` (case and spaces do not matter), or InvalidRequest naming them all."""
    key = str(name).strip().lower().replace(" ", "-").replace("_", "-")
    found = DEVICES.get(key)
    if found is None:
        raise InvalidRequest(f"no device named {str(name)[:40]!r}: use one of {', '.join(DEVICES)}, or a width "
                             "in px (240 to 3840)")
    return found


def is_device(value: object) -> bool:
    return isinstance(value, str) and not value.strip().isdigit()


def check_device(found: Device, engine: EngineName, screen: Screen) -> None:
    """Refuse a page the device cannot honour: another engine, or a screen of another size."""
    if found.engine is not engine:
        raise InvalidRequest(f"device {found.name} renders on {found.engine.value}, not {engine.value}: open it "
                             f"with engine {found.engine.value}")
    if screen.width != found.width or abs(screen.scale - found.scale) > 0.01:
        raise InvalidRequest(f"device {found.name} is {found.width} px at scale {found.scale:g}; the screen asked "
                             f"for {screen.width} px at scale {screen.scale:g}. Build the screen with "
                             "Device.screen().")


def context_options(found: Device, descriptors: dict) -> dict:
    """The new_context options for `found`, from Playwright's descriptor (refused when this
    Playwright does not have it). The viewport and scale stay the preset's, so the page matches its
    screen; `notes` says when the descriptor differs."""
    desc = descriptors.get(found.descriptor) if isinstance(descriptors, dict) else None
    if not desc:
        raise InvalidRequest(f"this Playwright has no device {found.descriptor!r}, so {found.name} cannot be "
                             "rendered as that device; name a width in px instead")
    return {"user_agent": desc.get("user_agent"), "is_mobile": bool(desc.get("is_mobile")),
            "has_touch": bool(desc.get("has_touch"))}


def descriptor_note(found: Device, descriptors: dict) -> str | None:
    desc = descriptors.get(found.descriptor) or {}
    vp = desc.get("viewport") or {}
    if vp and (vp.get("width") != found.width or abs(float(desc.get("device_scale_factor") or 0) - found.scale) > 0.01):
        return (f"Playwright's {found.descriptor} is {vp.get('width')} px at scale "
                f"{desc.get('device_scale_factor')}; rendered at the preset's {found.width} px at {found.scale:g}")
    return None
