"""The Tailwind side of the tokens: which class names exist, and which token each one is.

build-tokens.py imports this and writes two more generated files from it:
  web/src/styles/theme.css      the `@theme inline` block app.css imports, so `bg-card` is var(--surface-card)
  web/tokens/theme-keys.json    the same keys as data, which the design rules (src/test/design-rules.ts)
                                read to tell a token class from a raw one and to name the fix

Every Tailwind colour, size, radius, shadow, ease and animation the app has is listed here; Tailwind's own
palette, radii and shadows are cleared, so `bg-red-500` does not exist. LEGACY_* are the names the screens
used before Mono: they still work, each says the Mono name to use instead, and the design rules let their
count only shrink. Edit here, never theme.css.
"""

# Mono colour keys: the class word -> the CSS variable it reads (from tokens.css or aliases.css).
COLORS = {
    "transparent": "transparent",
    "current": "currentColor",
    # surfaces
    "page": "surface-page",
    "card": "surface-card",
    "inset": "surface-inset",
    "raised": "surface-raised",
    "inverse": "surface-inverse",
    "on-inverse": "text-inverse",
    "overlay": "overlay-scrim",
    "hover": "fill-hover",
    "pressed": "fill-pressed",
    "fill-strong": "fill-strong",
    # lines
    "line": "line-default",
    "line-strong": "line-strong",
    "control": "line-control",
    "focus": "focus-ring",
    # text
    "fg": "text-primary",
    "muted": "text-secondary",
    "faint": "text-tertiary",
    "disabled": "text-disabled",
    # the primary is ink
    "primary": "accent-fill",
    "primary-hover": "neutral-11",
    "on-primary": "text-inverse",
    "accent-soft": "accent-soft",
    "accent-line": "accent-line",
    "brand": "brand-mark",
    # an agent acting now (ink in Mono, with the ringed, pulsing dot)
    "live": "status-live-text",
    "live-soft": "status-live-fill",
    "live-dot": "status-live-dot",
    "live-line": "status-live-line",
    "status-ink": "status-ink",
    # the board
    "board": "board",
    "board-raised": "board-raised",
    "board-line": "board-line",
    "board-line-strong": "board-line-strong",
    # the terminal, dark in both themes
    "terminal": "terminal-bg",
    "terminal-text": "terminal-text",
    "terminal-dim": "terminal-dim",
    **{f"ansi-{c}": f"ansi-{c}" for c in ("red", "green", "yellow", "blue", "magenta", "cyan")},
    # diff lines
    "diff-add": "diff-add-fill",
    "diff-add-mark": "diff-add-mark",
    "diff-del": "diff-del-fill",
    "diff-del-mark": "diff-del-mark",
    # charts
    "chart-line": "chart-line",
    "chart-area": "chart-area",
    "chart-bar": "chart-bar",
    "chart-bar-strong": "chart-bar-strong",
    "chart-grid": "chart-grid",
    # people and businesses: fills on small marks only
    **{f"identity-{i}": f"identity-{i}" for i in range(1, 9)},
    "identity-ink": "identity-ink",
}
# Status families: the word, the mark, the tint, the edge.
for _tone in ("ok", "warn", "bad", "idle"):
    COLORS[_tone] = f"status-{_tone}-text"
    COLORS[f"{_tone}-dot"] = f"status-{_tone}-dot"
    COLORS[f"{_tone}-fill"] = f"status-{_tone}-fill"
    COLORS[f"{_tone}-line"] = f"status-{_tone}-line"
# The kind of work: never a status (a kind is a tile with its icon).
for _kind in ("test", "design", "build", "server"):
    COLORS[f"kind-{_kind}"] = f"kind-{_kind}-text"
    for _part in ("mark", "soft", "line"):
        COLORS[f"kind-{_kind}-{_part}"] = f"kind-{_kind}-{_part}"

# Pre-Mono colour keys: old word -> (the variable, the Mono word to write instead).
LEGACY_COLORS = {
    "bg": ("surface-card", "card"),
    "subtle": ("surface-inset", "inset"),
    "fill-1": ("fill-hover", "hover"),
    "fill-2": ("fill-pressed", "pressed"),
    "fill-3": ("fill-strong", "fill-strong"),
    "green": ("status-ok-text", "ok"),
    "amber": ("status-warn-text", "warn"),
    "red": ("status-bad-text", "bad"),
    # A link: ink with an underline. `text-blue` on an <a> still gets the underline (app.css).
    "blue": ("accent-text", "fg with the link class"),
}

# Type roles (the `text-<role>` classes). The mono roles also take Geist Mono in app.css.
ROLES = ["display", "page-title", "page-title-compact", "section-title", "lede", "card-title", "table-header", "label",
         "body", "secondary", "caption", "number", "stat", "code", "code-caption", "eyebrow", "micro"]
# The old size scale: size -> (line height, tracking var or None, the role to write instead).
LEGACY_TEXT = {
    11: (16, None, "micro or eyebrow"),
    12: (16, None, "caption"),
    13: (20, None, "secondary (or number, code)"),
    14: (20, None, "body (or label, card-title)"),
    16: (24, "tracking-16", "section-title or lede"),
    20: (28, "tracking-20", "section-title"),
    24: (32, "tracking-24", "page-title-compact"),
    32: (40, "tracking-32", "page-title or display"),
}

# Spacing steps that are tokens (others keep Tailwind's 4 px multiplier, so the grid is unchanged).
SPACING = ["0", "px", "1", "2", "3", "4", "5", "6", "8", "10", "12", "16"]
RADII = ["sm", "md", "lg", "xl"]
SHADOWS = {"popover": "shadow-popover", "dialog": "shadow-dialog", "lift": "lift", "card": "shadow-card", "hover": "shadow-hover"}
EASES = ["standard", "enter", "exit", "settle"]
ANIMATIONS = {
    "fade-in": "fade-in var(--duration-base) var(--ease)",
    "fade-out": "fade-out var(--duration-fast) var(--ease)",
    "pop-in": "pop-in var(--duration-base) var(--ease)",
    "pop-out": "pop-out var(--duration-fast) var(--ease)",
    "drawer-in": "drawer-in var(--duration-slow) var(--ease)",
    "drawer-out": "drawer-out var(--duration-base) var(--ease)",
    "sheet-in": "sheet-in var(--duration-slow) var(--ease)",
    "sheet-out": "sheet-out var(--duration-base) var(--ease)",
    "shimmer": "shimmer 1.6s linear infinite",
    "pulse-dot": "pulse-dot 1.8s var(--ease) infinite",
    "spin": "spin 0.7s linear infinite",
    "selection-rise": "selection-rise var(--duration-base) var(--ease-enter)",
}


def _var(name):
    return name if name in ("transparent", "currentColor") else f"var(--{name})"


def theme_css(breakpoints, z_layers, durations):
    """The `@theme inline` block, as the text of src/styles/theme.css."""
    out = ["""/*
 * The Tailwind theme: every class name the app has, each a reference to a token.
 *
 * GENERATED by web/tokens/build-tokens.py (the keys are in web/tokens/theme.py). Do not edit: change
 * the script and run it. Tailwind's default palette, radii and shadows are cleared, so a class like
 * bg-red-500 does not exist and cannot drift in. Rules for using them: web/DESIGN-SYSTEM.md.
 */
@theme inline {
  --color-*: initial;"""]
    for key, var in COLORS.items():
        out.append(f"  --color-{key}: {_var(var)};")
    out.append("")
    out.append("  /* Pre-Mono names, still working. New code writes the Mono word (theme-keys.json, legacy). */")
    for key, (var, _instead) in LEGACY_COLORS.items():
        out.append(f"  --color-{key}: {_var(var)};")
    out.append("")
    out.append("  --font-sans: var(--font-family-sans);")
    out.append("  --font-mono: var(--font-family-mono);")
    out.append("")
    out.append("  /* Type roles: one class sets size, line height, weight and tracking. */")
    out.append("  --text-*: initial;")
    for role in ROLES:
        p = f"--type-{role}"
        out.append(f"  --text-{role}: var({p}-font-size);")
        out.append(f"  --text-{role}--line-height: var({p}-line-height);")
        out.append(f"  --text-{role}--font-weight: var({p}-font-weight);")
        out.append(f"  --text-{role}--letter-spacing: var({p}-tracking);")
    out.append("  /* The old size scale (text-11 to text-32), on the Mono sizes. New code uses a role. */")
    for size, (lead, track, _instead) in LEGACY_TEXT.items():
        out.append(f"  --text-{size}: var(--font-{size});")
        out.append(f"  --text-{size}--line-height: var(--line-height-{lead});")
        if track:
            out.append(f"  --text-{size}--letter-spacing: var(--{track});")
    out.append("")
    out.append("  /* Spacing on the space tokens; steps that are not tokens keep Tailwind's 4 px multiplier. */")
    for step in SPACING:
        out.append(f"  --spacing-{step}: var(--space-{step});")
    out.append("")
    out.append("  --radius-*: initial;")
    for r in RADII:
        out.append(f"  --radius-{r}: var(--radius-{r});")
    out.append("  --radius-full: 9999px;")
    out.append("")
    out.append("  --shadow-*: initial;")
    for key, var in SHADOWS.items():
        out.append(f"  --shadow-{key}: var(--{var});")
    out.append("")
    out.append("  --ease-*: initial;")
    for e in EASES:
        out.append(f"  --ease-{e}: var(--ease-{e});")
    out.append("")
    out.append("  /* Media queries cannot read a variable, so the widths are written out, in rem as Tailwind has them. */")
    out.append("  --breakpoint-*: initial;")
    for key, value in breakpoints.items():
        out.append(f"  --breakpoint-{key}: {value / 16:g}rem;")
    out.append("")
    out.append("  --animate-*: initial;")
    for key, value in ANIMATIONS.items():
        out.append(f"  --animate-{key}: {value};")
    out.append("}")
    return "\n".join(out) + "\n"


def theme_keys(breakpoints, z_layers, durations):
    """The same keys as data, for the design rules and for an agent that wants the list."""
    return {
        "$description": "GENERATED by web/tokens/build-tokens.py. The Tailwind keys the app has. "
                        "web/src/test/design-rules.ts reads it. Edit web/tokens/theme.py.",
        "colors": sorted(COLORS),
        "legacyColors": {k: v[1] for k, v in LEGACY_COLORS.items()},
        "text": ROLES,
        "legacyText": {str(k): v[2] for k, v in LEGACY_TEXT.items()},
        "spacing": SPACING,
        "radius": [*RADII, "full", "none"],
        "shadow": [*SHADOWS, "none"],
        "ease": EASES,
        "breakpoint": list(breakpoints),
        "z": z_layers,
        "duration": {k: f"duration-(--duration-{k})" for k in durations},
        "animate": list(ANIMATIONS),
    }
