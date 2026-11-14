"""What a studio tool call asks for, made concrete before anything is measured: the profile it
names (or the one its URL matches) and that profile's brand, rules and tokens, and the screens
to capture (widths, themes, states), at most MAX_SCREENS.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentos.core.errors import InvalidRequest, NotFound, ServiceUnavailable
from agentos.core.tools import ToolContext, covers_workspace
from agentos.studio.browser.devices import DEVICES, device, is_device
from agentos.studio.contracts import (
    DEFAULT_WIDTHS,
    ComponentRule,
    EngineName,
    Screen,
    Step,
    Target,
    Theme,
    ThemeSwitch,
    TokenSet,
)
from agentos.studio.review import Plan

MOTION = "reduced: prefers-reduced-motion is on and animations show their last frame"
DEFAULT_THEMES = (Theme.LIGHT, Theme.DARK)


MAX_SCREENS = 24


# A guideline's threshold, as the rule key the check reads (agentos.studio.checks.run.rule).
RULE_KEYS = {"contrast.text": "min_ratio", "target.size": "min_px", "health.layout_shift": "max_cls",
             "health.long_tasks": "max_blocking_ms", "layout.occluded": "min_share",
             "health.images": "oversize", "text.truncated": "min_hidden"}


def profiles_service(ctx: ToolContext) -> Any:
    return ctx.runtime.get("profiles") if hasattr(ctx.runtime, "get") else None


def sees_workspace(ctx: ToolContext, workspace: str, accounts: list[str]) -> bool:
    if covers_workspace(ctx, workspace):
        return True
    check = getattr(ctx, "can_access_lane", None)
    return callable(check) and any(check(s) for s in accounts)


def resolve_profile(ctx: ToolContext, ref: str | None, url: str) -> tuple[str | None, Any, str | None]:
    """(ref, approved Profile, how it was chosen) or (None, None, None). Named: it must be
    approved and in the caller's reach. Not named: the first approved profile the caller reaches
    with an environment whose url the page's url starts with."""
    svc = profiles_service(ctx)
    if ref:
        if svc is None:
            raise ServiceUnavailable("profiles are not on this node, so profile cannot be used; call without it")
        from agentos.profiles.service import split_ref

        ws, pid = split_ref(ref.strip())
        active = svc.store.active(ws, pid)
        if active is None or not sees_workspace(ctx, ws, [a.surface for a in active.definition.accounts]):
            raise NotFound(f"no approved profile {ref.strip()[:80]} you can read (profile_list lists them; the owner "
                           "approves a draft first)")
        return f"{ws}/{pid}", active.definition, "named"
    if svc is None:
        return None, None, None
    best: dict[tuple[str, str], Any] = {}
    try:
        approved = svc.store.approved()
    except Exception:  # noqa: BLE001 - no profiles is an answer
        return None, None, None
    for pv in approved:
        p = pv.definition
        key = (p.workspace, p.id)
        if key not in best or pv.version > best[key].version:
            best[key] = pv
    for (ws, pid), pv in sorted(best.items()):
        p = pv.definition
        envs = getattr(p, "environments", None) or []
        if any(url.startswith(e.url.rstrip("/")) for e in envs) and \
                sees_workspace(ctx, ws, [a.surface for a in p.accounts]):
            return f"{ws}/{pid}", p, "its environment url matches"
    return None, None, None


def brand_of(profile: Any) -> Any:
    return getattr(profile, "brand", None) if profile is not None else None


def component_rules(brand: Any) -> list[ComponentRule]:
    """A profile brand's components as studio's rules (none without a brand)."""
    if brand is None:
        return []
    return [ComponentRule(name=c.name, attr=c.attr, class_pattern=c.class_pattern) for c in brand.components]


def brand_theme_switch(brand: Any) -> ThemeSwitch:
    """How a profile brand's product turns dark, as studio's ThemeSwitch (media when unset)."""
    theme = brand.theme if brand is not None else None
    return ThemeSwitch(**(theme.model_dump() if theme else {}))


def ignored_checks(given: list[str] | None, brand: Any) -> list[str]:
    """The regions to leave out: the call's selectors, then the brand's (brand.ignore)."""
    out: list[str] = []
    for sel in [*(given or []), *(getattr(brand, "ignore", None) or [])]:
        if isinstance(sel, str) and sel.strip() and sel.strip() not in out:
            out.append(sel.strip()[:200])
    if len(out) > 20:
        raise InvalidRequest("ignore holds at most 20 selectors")
    return out


def rules_of(profile: Any) -> dict[str, Any]:
    if profile is None:
        return {}
    out: dict[str, Any] = {}
    for check, threshold in profile.check_rules().items():
        key = RULE_KEYS.get(check)
        if key and threshold is not None:
            out[check] = {key: threshold}
    return out


def profile_tokens(ref: str | None, profile: Any) -> TokenSet | None:
    brand = brand_of(profile)
    if brand is None or not brand.tokens:
        return None
    from agentos.studio.tokens import load_tokens

    ts = load_tokens(brand.tokens)
    return ts.model_copy(update={"source": f"profile:{ref}"})


def token_source(tokens: str | dict[str, Any], where: str) -> TokenSet | None:
    """system_check's tokens: 'page' (None here: read from the page), DTCG JSON or CSS text, or a
    file path on this machine (a laptop only: a node never reads a path an agent names). A client
    that parses JSON text into an object on the way in sends a dict: that is DTCG too."""
    from agentos.studio.tokens import load_tokens

    if isinstance(tokens, dict):
        ts = load_tokens(tokens)
        if not ts.tokens and not ts.scales:
            raise InvalidRequest("tokens held no token studio can use (colours, spacing, radius, fonts, shadows, "
                                 "borders)")
        return ts
    text = tokens.strip()
    if text == "page":
        return None
    looks_like_text = "\n" in text or "{" in text
    if not looks_like_text and where != "local":
        raise InvalidRequest("tokens is 'page', or the tokens as Design Tokens JSON or CSS text; a file path is read "
                             "only by a local studio (agentos studio up)")
    ts = load_tokens(text if looks_like_text else Path(text).expanduser())
    if not ts.tokens and not ts.scales:
        raise InvalidRequest("tokens held no token studio can use (colours, spacing, radius, fonts, shadows, borders)")
    return ts


def parse_theme_switch(value: Any) -> ThemeSwitch | None:
    """theme_switch as given (the registry validates it; a direct caller may pass a dict)."""
    return value if value is None or isinstance(value, ThemeSwitch) else ThemeSwitch.model_validate(value)


def parse_states(value: Any) -> dict[str, list[Step]] | None:
    if not value:
        return None
    if not isinstance(value, dict):
        raise InvalidRequest('states maps a state name to its steps, like {"menu": [{"do": "click", "selector": "#m"}]}')
    return {str(k): [s if isinstance(s, Step) else Step.model_validate(s) for s in v] for k, v in value.items()}


def target_for(url: str, *, engine: EngineName, switch: ThemeSwitch, wait_for: str | None,
            states: dict[str, list[Step]] | None) -> Target:
    return Target(url=url, engine=engine, theme_switch=switch, wait_for=wait_for, states=dict(states or {}))


def _width(w: int | str) -> int | str:
    """A width as px (240 to 3840), or a device's name."""
    if isinstance(w, str) and w.strip().isdigit():
        w = int(w.strip())
    if is_device(w):
        return device(str(w)).name
    if isinstance(w, int) and not isinstance(w, bool) and 240 <= w <= 3840:
        return w
    raise InvalidRequest(f"a width is a number of px from 240 to 3840, or a device ({', '.join(DEVICES)}); "
                         f"not {str(w)[:40]!r}")


def _plan(url: str, w: int | str, theme: Theme, state: str, *, engine: EngineName, switch: ThemeSwitch,
          wait_for: str | None, states: dict[str, list[Step]] | None) -> Plan:
    """One screen: a width in px on the call's engine, or a device on its own engine."""
    if isinstance(w, str):
        d = device(w)
        target = target_for(url, engine=d.engine, switch=switch, wait_for=wait_for, states=states)
        return Plan(screen=d.screen(theme, state), target=target, device=d.name)
    target = target_for(url, engine=engine, switch=switch, wait_for=wait_for, states=states)
    return Plan(screen=Screen(width=w, theme=theme, state=state), target=target)


def plan_screens(url: str, *, widths: list[int | str] | None, themes: list[Theme] | None,
           states: dict[str, list[Step]] | None, engine: EngineName, switch: ThemeSwitch, wait_for: str | None,
           brand_widths: list[int] | None, system: bool, system_only: bool = False) -> tuple[list[Plan], dict]:
    """The screens of a call: every state at every width (or device) in every theme. The design
    system check runs on the widest width in each theme of the default state."""
    raw = list(widths) if widths else (list(brand_widths) if brand_widths else list(DEFAULT_WIDTHS))
    ws = [_width(w) for w in raw]
    ws = list(dict.fromkeys(ws))
    ts = list(dict.fromkeys(Theme(t) for t in (themes or DEFAULT_THEMES)))
    names = ["default", *[s for s in (states or {}) if s != "default"]]
    if not ws or not ts:
        raise InvalidRequest("name at least one width and one theme")
    numeric = [w for w in ws if isinstance(w, int)]
    system_width: int | str = max(numeric) if numeric else ws[0]
    if system_only:
        ws = [system_width]
    if len(ws) * len(ts) * len(names) > MAX_SCREENS:
        raise InvalidRequest(f"{len(ws) * len(ts) * len(names)} screens is more than {MAX_SCREENS}: ask for fewer "
                             "widths, themes or states")
    plans: list[Plan] = []
    for state in names:
        for w in ws:
            for theme in ts:
                plan = _plan(url, w, theme, state, engine=engine, switch=switch, wait_for=wait_for, states=states)
                plan.system = system and state == "default" and w == system_width
                plans.append(plan)
    return plans, _used(ws, ts, names, engine, switch)


def _used(ws: list[int | str], ts: list[Theme], names: list[str], engine: EngineName,
          switch: ThemeSwitch) -> dict[str, Any]:
    """What the call used, as the result says it."""
    used: dict[str, Any] = {"widths": ws, "themes": [t.value for t in ts], "states": names, "engine": engine.value,
                            "motion": MOTION}
    devs = [w for w in ws if isinstance(w, str)]
    if devs:
        used["devices"] = [device(d).label for d in devs]
    if switch.how != "media":
        used["theme_switch"] = " ".join(str(v) for v in switch.model_dump(exclude_none=True).values())
    return used
