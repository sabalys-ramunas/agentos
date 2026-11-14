"""What runs inside a review page: the theme switch, the pins (random numbers, motion, caret), the
read back of the theme, the blank page test and the font fallback report.

The review browser is not a lane, so the lane stealth rules (no init scripts, no main-world
evaluation, docs/design/stealth.md) do not apply here: these scripts run in the page itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from agentos.core.errors import InvalidRequest
from agentos.studio.contracts import Theme, ThemeSwitch

# Every page sees this time when it loads (Date.now starts here and runs on), so a date or a
# "2 hours ago" on the page renders the same in every capture.
PINNED_TIME = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
RANDOM_SEED = 20260115

# Motion off and caret hidden. Finite animations end on their last frame, endless ones are cancelled
# (see FINISH_ANIMATIONS). Set before the page's own styles load, so nothing starts moving.
# Transitions take 1 ms, not 0 s: a transition of 0 s never runs, so its transitionend never fires,
# and a sheet or a dialog that waits for that event to fill itself would never open.
PIN_CSS = """
*, *::before, *::after {
  animation-duration: 0s !important; animation-delay: 0s !important;
  animation-iteration-count: 1 !important;
  transition-duration: 1ms !important; transition-delay: 0s !important;
  caret-color: transparent !important; scroll-behavior: auto !important;
}
"""

_PINS = """
(() => {
  let seed = %(seed)d >>> 0;
  Math.random = function () {
    seed = (seed + 0x6D2B79F5) >>> 0;
    let t = seed;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  // When web fonts load: a layout shift while they load, or just after, is a font swapping in.
  try {
    if (document.fonts) {
      const spans = window.__agentosStudioFontSpans = [];
      document.fonts.addEventListener('loading', () => spans.push([performance.now(), null]));
      document.fonts.addEventListener('loadingdone', () => {
        const open = spans.filter((x) => x[1] === null);
        if (open.length) for (const x of open) x[1] = performance.now(); else spans.push([performance.now(), performance.now()]);
      });
    }
  } catch (e) { /* no FontFaceSet here */ }
  const css = %(css)s;
  const add = () => {
    if (document.getElementById('agentos-studio-pins')) return;
    const style = document.createElement('style');
    style.id = 'agentos-studio-pins';
    style.textContent = css;
    (document.head || document.documentElement).appendChild(style);
  };
  if (document.documentElement) add();
  document.addEventListener('DOMContentLoaded', add, { once: true });
})();
"""

FINISH_ANIMATIONS = """
() => {
  let finished = 0, cancelled = 0;
  for (const a of document.getAnimations ? document.getAnimations() : []) {
    try { a.finish(); finished++; } catch (e) { a.cancel(); cancelled++; }
  }
  return { finished, cancelled };
}
"""

# Two animation frames: what the last style change did is laid out and painted.
NEXT_FRAMES = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"

FONTS_READY = "() => document.fonts ? document.fonts.ready.then(f => f.status) : 'unsupported'"


def pins_script(seed: int = RANDOM_SEED) -> str:
    return _PINS % {"seed": seed, "css": json.dumps(PIN_CSS)}


def theme_value(switch: ThemeSwitch, theme: Theme) -> str | None:
    return switch.dark if theme is Theme.DARK else switch.light


def check_switch(switch: ThemeSwitch) -> None:
    """A switch that cannot work is the caller's mistake, said before anything opens."""
    if switch.how in ("attr", "storage") and not switch.name:
        raise InvalidRequest(f"theme_switch how={switch.how} needs `name` (the {'attribute' if switch.how == 'attr' else 'storage key'}).")
    if switch.how == "attr" and switch.dark is None and switch.light is None:
        raise InvalidRequest("theme_switch how=attr needs `light` or `dark`: the attribute's value for each theme.")
    if switch.how == "storage" and switch.dark is None and switch.light is None:
        raise InvalidRequest("theme_switch how=storage needs `light` or `dark`: the stored value for each theme.")


def _class_names(switch: ThemeSwitch) -> tuple[str, str | None]:
    """(dark class, light class or None). `class` with no names is Tailwind's `dark` class."""
    return switch.dark or "dark", switch.light


def theme_script(switch: ThemeSwitch, theme: Theme) -> str | None:
    """The script that puts the page in `theme` before its own scripts run, and again when its DOM
    is parsed (a framework that rebuilds <html> would drop it). None for `media`: the browser's
    colour scheme emulation does that alone."""
    if switch.how == "media":
        return None
    args = json.dumps({"how": switch.how, "name": switch.name, "value": theme_value(switch, theme),
                       "dark_class": _class_names(switch)[0], "light_class": _class_names(switch)[1],
                       "dark": theme is Theme.DARK})
    return """
(() => {
  const s = __SWITCH__;
  if (s.how === 'storage') {
    try { s.value === null ? localStorage.removeItem(s.name) : localStorage.setItem(s.name, s.value); } catch (e) {}
    return;
  }
  const apply = () => {
    const root = document.documentElement;
    if (!root) return;
    if (s.how === 'attr') {
      s.value === null ? root.removeAttribute(s.name) : root.setAttribute(s.name, s.value);
    } else {
      root.classList.toggle(s.dark_class, s.dark);
      if (s.light_class) root.classList.toggle(s.light_class, !s.dark);
    }
  };
  apply();
  document.addEventListener('DOMContentLoaded', apply, { once: true });
})();
""".replace("__SWITCH__", args)


# Reads the theme back after settling. For `media` dark, the browser's emulation alone proves
# nothing: the page must also have styles for it (a prefers-color-scheme rule, a color-scheme
# that includes dark). Stylesheets from another origin cannot be read; then it is "unverified".
_READ_THEME = """
(s) => {
  const root = document.documentElement;
  const out = { how: s.how, wanted: s.theme };
  out.media = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  if (s.how === 'attr') { out.read = root.getAttribute(s.name); out.applied = out.read === s.value; return out; }
  if (s.how === 'class') {
    const dark = root.classList.contains(s.dark_class);
    const light = s.light_class ? root.classList.contains(s.light_class) : !dark;
    out.read = root.className;
    out.applied = s.theme === 'dark' ? (dark && !(s.light_class && light)) : (!dark && light);
    return out;
  }
  if (s.how === 'storage') {
    let v = null; try { v = localStorage.getItem(s.name); } catch (e) {}
    out.read = v; out.applied = v === s.value; return out;
  }
  // media
  if (out.media !== s.theme) { out.applied = false; return out; }
  if (s.theme === 'light') { out.applied = true; return out; }
  const schemes = [root, document.body].filter(Boolean).map(e => getComputedStyle(e).colorScheme || '');
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta && /dark/.test(meta.content || '')) schemes.push(meta.content);
  let unreadable = 0, rule = false;
  const walk = (rules) => {
    for (const r of rules) {
      if (rule) return;
      if (r.media && /prefers-color-scheme\\s*:\\s*dark/.test(r.media.mediaText || '')) { rule = true; return; }
      if (r.conditionText && /prefers-color-scheme\\s*:\\s*dark/.test(r.conditionText)) { rule = true; return; }
      if (r.cssRules) walk(r.cssRules);
    }
  };
  for (const sheet of document.styleSheets) {
    if (sheet.media && /prefers-color-scheme\\s*:\\s*dark/.test(sheet.media.mediaText || '')) { rule = true; break; }
    let rules = null;
    try { rules = sheet.cssRules; } catch (e) { unreadable++; continue; }
    if (rules) walk(rules);
    if (rule) break;
  }
  out.read = rule ? 'a prefers-color-scheme: dark rule' : schemes.find(x => /dark/.test(x)) || null;
  out.applied = rule || schemes.some(x => /dark/.test(x)) ? true : (unreadable ? null : false);
  if (out.applied === null) out.note = unreadable + ' stylesheet(s) from another origin could not be read';
  return out;
}
"""


def read_theme_args(switch: ThemeSwitch, theme: Theme) -> dict[str, Any]:
    dark_class, light_class = _class_names(switch)
    return {"how": switch.how, "name": switch.name, "value": theme_value(switch, theme), "theme": theme.value,
            "dark_class": dark_class, "light_class": light_class}


READ_THEME = _READ_THEME

# Is anything visible? Text, an image, a control or a painted box inside the page's area. Shadow
# roots are entered (a web component app renders there).
BLANK_TEST = """
() => {
  const W = Math.max(document.documentElement.scrollWidth, innerWidth);
  const H = Math.max(document.documentElement.scrollHeight, innerHeight);
  const REPLACED = new Set(['IMG','SVG','CANVAS','VIDEO','INPUT','TEXTAREA','SELECT','BUTTON','IFRAME','OBJECT','EMBED','PICTURE']);
  const bodyBg = document.body ? getComputedStyle(document.body).backgroundColor : '';
  let checked = 0;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || r.right <= 0 || r.bottom <= 0 || r.left >= W || r.top >= H) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.visibility === 'collapse' || parseFloat(cs.opacity) === 0) return false;
    return cs;
  };
  const shows = (el) => {
    checked++;
    const cs = visible(el);
    if (!cs) return false;
    if (REPLACED.has(el.tagName.toUpperCase())) return true;
    if (cs.backgroundImage && cs.backgroundImage !== 'none') return true;
    for (const n of el.childNodes) {
      if (n.nodeType === 3 && n.textContent.trim()) {
        const range = document.createRange(); range.selectNodeContents(n);
        const r = range.getBoundingClientRect();
        if (r.width >= 1 && r.height >= 1 && cs.color !== 'rgba(0, 0, 0, 0)') return true;
      }
    }
    const bg = cs.backgroundColor;
    if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent' && bg !== bodyBg && el !== document.body) {
      const r = el.getBoundingClientRect();
      if (r.width * r.height >= 64) return true;
    }
    return false;
  };
  const walk = (root) => {
    for (const el of root.querySelectorAll('*')) {
      if (el.tagName === 'HTML' || el.tagName === 'HEAD' || el.closest('head')) continue;
      if (shows(el)) return true;
      if (el.shadowRoot && walk(el.shadowRoot)) return true;
    }
    return false;
  };
  return { content: walk(document), checked, title: (document.title || '').slice(0, 80) };
}
"""

# Which named fonts the page asks for are not there. A family is there when text set in it
# measures differently from two generic fallbacks (it was loaded as a web font, or the system has
# it). Generic names (sans-serif, system-ui) are not checked: they always resolve to something.
FONT_FALLBACKS = """
() => {
  const GENERIC = new Set(['serif','sans-serif','monospace','cursive','fantasy','system-ui','ui-serif',
    'ui-sans-serif','ui-monospace','ui-rounded','math','emoji','fangsong','-apple-system','blinkmacsystemfont',
    'inherit','initial','unset']);
  const stacks = new Map();
  let n = 0;
  for (const el of document.querySelectorAll('body, body *')) {
    if (++n > 4000) break;
    let text = false;
    for (const c of el.childNodes) if (c.nodeType === 3 && c.textContent.trim()) { text = true; break; }
    if (!text) continue;
    const f = getComputedStyle(el).fontFamily;
    stacks.set(f, (stacks.get(f) || 0) + 1);
  }
  const ctx = document.createElement('canvas').getContext('2d');
  const sample = 'mmmmmmmmmlli1WQ@#&gyAaBbZz';
  const width = (font) => { ctx.font = '72px ' + font; return ctx.measureText(sample).width; };
  const base = { monospace: width('monospace'), serif: width('serif') };
  const cache = new Map();
  const present = (family) => {
    if (cache.has(family)) return cache.get(family);
    const q = '"' + family.replace(/"/g, '') + '"';
    const yes = width(q + ', monospace') !== base.monospace || width(q + ', serif') !== base.serif;
    cache.set(family, yes);
    return yes;
  };
  const out = [];
  for (const [stack, count] of stacks) {
    const names = stack.split(',').map(s => s.trim().replace(/^["']|["']$/g, '')).filter(Boolean);
    const first = names[0];
    if (!first || GENERIC.has(first.toLowerCase())) continue;
    if (present(first)) continue;
    const used = names.slice(1).find(x => GENERIC.has(x.toLowerCase()) || present(x)) || 'the browser default';
    out.push({ family: first, used, elements: count });
  }
  return out.sort((a, b) => b.elements - a.elements).slice(0, 12);
}
"""
