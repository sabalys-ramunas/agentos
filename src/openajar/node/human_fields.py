"""Human input on form fields, built on openajar.node.human: focus a field the way a person does,
clear it, pick an option from a native select with the keyboard, and decide whether typos fit
the field. The scripts only read the field's state.
"""

from __future__ import annotations

import random
from typing import Any

from openajar.core.errors import InvalidRequest
from openajar.node.human import _pause, click, press, quietly

Page = Any    # playwright.async_api.Page
Target = Any  # playwright.async_api.Locator | playwright.async_api.ElementHandle


_IS_FOCUSED_JS = "(el) => el.matches(':focus')"

_EDITABLE_JS = """
(el) => {
  const tag = el.tagName.toLowerCase();
  if (tag === 'textarea') return 'multiline';
  if (tag === 'input') {
    const t = (el.type || 'text').toLowerCase();
    return ['checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'image', 'range', 'color'].includes(t)
      ? null : 'line';
  }
  if (el.isContentEditable) return 'multiline';
  return null;
}
"""

_HAS_CONTENT_JS = """
(el) => ('value' in el && typeof el.value === 'string') ? el.value.length > 0
      : (el.isContentEditable ? (el.textContent || '').length > 0 : false)
"""

_SELECT_INFO_JS = """
(el) => ({
  tag: el.tagName.toLowerCase(),
  multiple: !!el.multiple,
  size: el.size || 0,
  selectedIndex: el.selectedIndex,
  options: Array.from(el.options || [], (o) => ({label: (o.label || o.textContent || '').trim(), disabled: o.disabled})),
})
"""


async def is_focused(target: Target) -> bool:
    return bool(await quietly(target.evaluate(_IS_FOCUSED_JS), False))


async def editable_kind(target: Target) -> str | None:
    """'line' for single-line inputs, 'multiline' for textareas and contenteditable, else None."""
    return await target.evaluate(_EDITABLE_JS)


# What decides whether a field may see a typo (typos_fit). Read here, decided in Python.
_TYPO_FACTS_JS = """
(el) => {
  const attr = (name) => (el.getAttribute(name) || '').trim().toLowerCase();
  return {
    maxLength: typeof el.maxLength === 'number' ? el.maxLength : -1,
    type: el.tagName.toLowerCase() === 'input' ? (el.type || 'text').toLowerCase() : '',
    combobox: !!el.closest('[role~="combobox" i]'),
    ariaAutocomplete: attr('aria-autocomplete'),
    autocomplete: attr('autocomplete'),
    inputmode: attr('inputmode'),
    suggestions: el.tagName.toLowerCase() === 'input' && el.hasAttribute('list'),
  };
}
"""


def typos_allowed(facts: Any) -> bool:
    """Whether a field with these facts (_TYPO_FACTS_JS) may see a typo that is fixed a moment later.
    Not when a slip can do harm before the Backspace: a length limit (near it the letters after the
    slip are dropped, and the Backspaces then erase text the caller asked for); a password; a field
    that acts on every key (a combobox or autocomplete that picks a suggestion, a list of
    suggestions, a one-time code that submits once full, a numeric field)."""
    if not isinstance(facts, dict):
        return False
    try:
        if int(facts.get("maxLength", -1)) >= 0:
            return False
    except (TypeError, ValueError):
        return False
    if facts.get("type") == "password" or facts.get("combobox") or facts.get("suggestions"):
        return False
    if facts.get("ariaAutocomplete") not in ("", "none", None):
        return False
    if "one-time-code" in str(facts.get("autocomplete") or "").split():
        return False
    return facts.get("inputmode") not in ("numeric", "decimal", "tel")


async def typos_fit(target: Target) -> bool:
    """Whether type_text may slip in this field (typos_allowed). Unreadable counts as unfit."""
    return typos_allowed(await quietly(target.evaluate(_TYPO_FACTS_JS), None))


async def focus_field(page: Page, target: Target, *, caret_to_end: bool = True) -> None:
    """Click into a text field unless it already has focus. After a click the caret sits where
    the pointer landed, so (like a person would) move it to the end before typing."""
    if await is_focused(target):
        return
    await click(page, target)
    if caret_to_end and await target.evaluate(_HAS_CONTENT_JS):
        kind = await editable_kind(target)
        await press(page, "ControlOrMeta+End" if kind == "multiline" else "End")


async def clear_field(page: Page, target: Target) -> None:
    """Select everything in the focused field and delete it, the way a person would."""
    if not await target.evaluate(_HAS_CONTENT_JS):
        return
    await press(page, "ControlOrMeta+a")
    await _pause(page, 80, 220)
    await press(page, "Backspace")


def match_option(options: list[dict], label: str) -> int | None:
    """Index of the option whose label matches: exact first, then case and space insensitive."""
    wanted = label.strip()
    for i, opt in enumerate(options):
        if opt["label"] == wanted:
            return i
    folded = " ".join(wanted.split()).casefold()
    for i, opt in enumerate(options):
        if " ".join(opt["label"].split()).casefold() == folded:
            return i
    return None


def arrow_steps(options: list[dict], current: int, wanted: int) -> int:
    """Arrow presses from `current` to `wanted` on a closed select (disabled options are skipped
    by the browser). Positive means ArrowDown. With nothing selected, the first ArrowDown lands on
    the first enabled option."""
    enabled = [i for i, opt in enumerate(options) if not opt.get("disabled")]
    if wanted not in enabled:
        raise InvalidRequest("that option is disabled")
    if current not in enabled:
        return enabled.index(wanted) + 1
    return enabled.index(wanted) - enabled.index(current)


async def select_option(page: Page, target: Target, label: str) -> None:
    """Pick an option of a native <select> by its visible label, with real input: a list box
    gets a click on the option; a dropdown gets a click to focus it, then the arrows, and if they
    land nowhere the start of the label typed into it.

    The arrows are how a dropdown moves on Windows and Linux, but on macOS a click opens a native
    menu that takes the keys itself, so ArrowDown can arrive and move nothing (measured: headless
    and headful, before and after the click, with and without humanize). Typing a few letters is
    how a person moves that menu too, so it is a fair stand-in when the arrows do not land."""
    info = await target.evaluate(_SELECT_INFO_JS)
    if info.get("tag") != "select":
        raise InvalidRequest("select works on a native dropdown; for a custom one, click it and then the option")
    options = info["options"]
    index = match_option(options, label)
    if index is None:
        shown = ", ".join(repr(o["label"]) for o in options[:20])
        raise InvalidRequest(f"no option labelled {label!r}. Options: {shown}")
    if index == info["selectedIndex"]:
        return
    if info["multiple"] or info["size"] > 1:
        option = await target.evaluate_handle("(el, i) => el.options[i]", index)
        await click(page, option.as_element())
        return
    if not await is_focused(target):
        await click(page, target)
        await _pause(page, 250, 600)
    current = await target.evaluate("(el) => el.selectedIndex")
    steps = arrow_steps(options, current, index)
    key = "ArrowDown" if steps > 0 else "ArrowUp"
    for _ in range(abs(steps)):
        await press(page, key)
    await press(page, "Enter")  # closes the dropdown if the click opened it
    if await target.evaluate("(el) => el.selectedIndex") != index:
        await _type_into(page, target, options[index]["label"])


async def _type_into(page: Page, target: Target, label: str) -> None:
    """Type the option's opening letters at the select, which is how type-ahead picks it. Long
    enough to tell two similar labels apart, short enough that a disabled first match does not
    hold it back."""
    await target.focus()
    await _pause(page, 120, 300)
    for ch in label[: min(len(label), 8)]:
        await page.keyboard.press(ch)
        await page.wait_for_timeout(random.randint(40, 110))
