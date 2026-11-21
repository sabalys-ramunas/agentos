"""The openajar web app's design tokens (Dawn, on Mono's bones): the one place their values live.

Copied from the style-mono prototype's build-tokens.py (removed with the classic panel), the
approved Mono take (docs/design/style-style-mono.md). Changes from that copy are marked "web". Dawn
(2026-11-20, "W4 dawn" below) is the desktop's light brought to the app: an ink-blue cast on the
neutrals, the night navy in dark, one violet (the agent's own colour), glass over the Dawn picture,
a night sidebar, Full's larger type and less round corners (the owner: cards 10, not 14).

Writes:
  web/tokens/tokens.json       Design Tokens (DTCG 2025.10) as a themes map: base, light, dark
  web/src/styles/tokens.css    the same set as CSS custom properties (light default, dark by
                               system or data-theme)
  web/src/styles/theme.css     web: the Tailwind theme, every class name onto a token (tokens/theme.py)
  web/tokens/theme-keys.json   web: the same class names as data, for the design rules
and checks every intended text and background pair against WCAG AA, and that the kind colours stay
apart from each other and from status for protanopia, deuteranopia and tritanopia. It fails (exit 1)
if a pair falls short.

Run:   python3 web/tokens/build-tokens.py           write both files and check
       python3 web/tokens/build-tokens.py --check   write nothing; fail if the files on disk differ
                                                    from what this script would write, or a check fails
Edit the values here, never the two outputs. web/DESIGN.md says why each one is what it is.
"""
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent
# web: the CSS goes where the app imports it.
CSS_OUT = OUT.parent / "src" / "styles" / "tokens.css"
THEME_OUT = OUT.parent / "src" / "styles" / "theme.css"
CHECK = "--check" in sys.argv[1:]
DRIFT = []


def emit(path, text):
    """web: write a generated file, or with --check only compare it with what is on disk."""
    if CHECK:
        if not path.exists() or path.read_text() != text:
            DRIFT.append(str(path))
        return
    path.write_text(text)

# ---- neutral ramp: 13 steps, by luminance (0 = lightest in light, darkest in dark) ----
# W2 (the app design, docs/design/app-design.md): Workbench's warm paper in light and graphite in
# dark replace D1's pure grey. Surfaces carry a faint warmth, text stays a cool ink, so the status
# colours and the accent still read clean against it.
# W3 mono: true white and true black with a cool, hue-free ramp in between (Vercel's grey scale is
# the model: #fafafa page, #ebebeb hairline, #171717 ink; #000 page and #0a0a0a cards in dark).
# W4 dawn: the same ramp with a trace of ink blue (the desktop's night sky), and dark is Dawn's night
# navy, not black. Page #f6f6fa (Full), card white; night #090b16, card #0f1222.
N_LIGHT = ["#ffffff", "#f6f6fa", "#f3f3f7", "#eeeef4", "#e7e7ee", "#dcdce5", "#cfcfdb",
           "#a3a3b5", "#85859a", "#67677d", "#4f4f63", "#2d2f45", "#15172b"]
N_DARK = ["#090b16", "#0f1222", "#141729", "#191d31", "#1e2238", "#22263d", "#353a55",
          "#4e5373", "#6f7494", "#9095b2", "#b0b4cc", "#d6d8e6", "#eef0f7"]

# W4 dawn: the violet, one hue from the agent's pointer on the desktop. A scale, the same in both
# themes; roles below pick from it. What it is for: the agent (its pointer, its tag, an agent at
# work), what is selected, the keyboard's focus, the primary button, the mark. Never a status, a
# large background or decoration (web/DESIGN.md, "Violet").
VIOLET = {"50": "#f4f1ff", "100": "#ebe5ff", "200": "#d9cfff", "300": "#bfaeff", "400": "#a08aff",
          "500": "#8466ff", "600": "#6d4aff", "700": "#5b37e6", "800": "#4a2bbf", "900": "#3a2394",
          "950": "#221559"}


def vi(step):
    return VIOLET[step]

# semantic colours: name -> (light, dark, note). "{n.5}" aliases a ramp step.
def n(i):
    return "{color.neutral.%d}" % i

COLORS = {
    # surfaces
    # W3 mono: dark cards sit one step above a true black page; inset is a well, not a header band.
    "surface.page": (n(1), n(0), "The page behind everything."),
    "surface.card": (n(0), n(1), "Cards, tables, panels: one step off the page."),
    "surface.inset": (n(2), n(2), "Wells: code, pictures waiting to load, card footers. Never a table header."),
    "surface.raised": (n(0), n(3), "Menus, popovers, dialogs, sheets. Always with a shadow."),
    "surface.inverse": (n(12), n(11), "Tooltips and toasts: the one inverted surface."),
    # W4 dawn: the stage a live screen sits on, dark in both themes (the control pill sits on it).
    "surface.stage": ("#0b0d1a", "#070914", "Behind the live screen and pictures of it. Dark in both themes."),
    # web: a control on the inverse surface (the selection bar): text.inverse at 35, 14 and 22 percent.
    # Were color-mix() in button.tsx; text.inverse is white in light and black in dark.
    "inverse.line": ("rgba(255,255,255,0.35)", "rgba(0,0,0,0.35)", "A hairline on the inverse surface."),
    "inverse.hover": ("rgba(255,255,255,0.14)", "rgba(0,0,0,0.14)", "Hover on the inverse surface."),
    "inverse.pressed": ("rgba(255,255,255,0.22)", "rgba(0,0,0,0.22)", "Pressed on the inverse surface."),
    # translucent fills so they work on any surface
    "fill.hover": ("rgba(21,23,43,0.04)", "rgba(255,255,255,0.05)", "Row and control hover."),
    "fill.pressed": ("rgba(21,23,43,0.07)", "rgba(255,255,255,0.08)", "Pressed, open menu trigger."),
    # lines
    # W3 mono: the hairline almost disappears (#ebebeb on #fafafa, #262626 on #000).
    "line.default": (n(4), n(5), "Every hairline: cards, rows, header bottom."),
    "line.strong": (n(6), n(6), "Hovered control edge, input edge, a divider that must hold."),
    "line.control": (n(8), n(8), "Checkbox, radio and switch edges: 3:1 against any surface (WCAG 1.4.11)."),
    # text
    "text.primary": (n(12), n(12), "Titles, cell values, anything read first."),
    "text.secondary": (n(10), n(10), "Descriptions, table headers, second lines."),
    "text.tertiary": (n(9), n(9), "Metadata, captions, timestamps, placeholders."),
    "text.disabled": (n(7), n(7), "Disabled controls only. Exempt from contrast, never for something to read."),
    "text.inverse": (n(0), n(0), "Text on surface.inverse and on solid fills."),
    # the one accent
    # W3 mono: the accent is ink. What is selected is black in light and white in dark; colour is
    # kept for meaning (the kinds of work, and status). Focus stays blue, the one colour that only
    # ever means "the keyboard is here".
    # W4 dawn: what is selected is violet again (the selected tab's word, a selected nav item, a
    # checked box, a switch on). Links stay ink with an underline. Focus is the violet ring.
    "accent.text": (vi("700"), vi("300"), "The selected tab's word, a selected item's word. Violet."),
    "accent.fill": (vi("600"), vi("500"), "Checked checkbox, switch on, selected chip, progress that is not a status."),
    "accent.soft": (vi("50"), "rgba(132,102,255,0.14)", "A selected row or item: tint under text."),
    "accent.line": (vi("200"), "#3d3380", "A selected item's edge, a drop target."),
    "focus.ring": (vi("600"), vi("400"), "The keyboard focus ring. 3:1 against every surface. Violet."),
    # W4 dawn: Full's primary is the violet pill (white words 5.2:1). Dark keeps violet 600 under its
    # words: violet 500 would carry white at only 4:1.
    "action.primary": (vi("600"), vi("600"), "The one primary button on a screen."),
    "action.primary-hover": (vi("700"), vi("700"), ""),
    "action.on-primary": ("#ffffff", "#ffffff", "Words on the primary."),
    "link.text": (n(12), n(12), "A link: ink with an underline, never a hue."),
    # status: text reads as words, dot is the mark, fill tints a badge or a row, line edges it
    "status.ok.text": ("#14733f", "#4cc27a", ""),
    "status.ok.dot": ("#1e9a52", "#3fb46e", ""),
    "status.ok.fill": ("#ebf7ef", "#0f2418", ""),
    "status.ok.line": ("#b3e0c4", "#1d4a2f", ""),
    "status.warn.text": ("#984800", "#f5a524", ""),
    "status.warn.dot": ("#cf7c00", "#f5a524", ""),
    "status.warn.fill": ("#fff5e5", "#2a1d07", ""),
    "status.warn.line": ("#f5d29c", "#5a3c0b", ""),
    "status.bad.text": ("#c0282e", "#ff6b70", ""),
    "status.bad.dot": ("#e5484d", "#e5484d", ""),
    "status.bad.fill": ("#fdeeee", "#2d1213", ""),
    "status.bad.line": ("#f3c2c3", "#5c2224", ""),
    # W3 mono: running took ink and a moving ring. W4 dawn: an agent at work is the agent's violet,
    # as a ring that never pulses (Starting is the same ring dashed). Green, amber and red keep their
    # meaning, and the test kind moved off violet to plum so no kind shares the agent's hue.
    "status.live.text": (vi("700"), "#c4b8ff", "An agent at work right now. Violet; a ringed dot that never pulses."),
    "status.live.dot": (vi("600"), vi("400"), ""),
    "status.live.fill": (vi("50"), "rgba(132,102,255,0.14)", ""),
    "status.live.line": (vi("200"), "#4a3f99", ""),
    "status.idle.text": (n(10), n(10), "Queued, sleeping, skipped: no colour, a hollow mark."),
    "status.idle.dot": (n(8), n(8), ""),
    "status.idle.fill": (n(2), n(3), ""),
    "status.idle.line": (n(6), n(6), ""),
    # terminal and ANSI: themed, every colour 4.5:1 on terminal.bg
    # W3 mono: a terminal is black in both themes. Output is the one thing that looks like a screen.
    "terminal.bg": ("#0b0d1a", "#070914", "A terminal or log block. Dark in both themes (the stage's night)."),
    "terminal.text": ("#e5e5e5", "#e5e5e5", ""),
    "terminal.dim": ("#8a8a8a", "#8a8a8a", "Timestamps, the prompt, faint (ANSI 2) output."),
    # web: a divider inside a terminal (was `border-white/10`, a class that does not exist here).
    "terminal.line": ("rgba(255,255,255,0.1)", "rgba(255,255,255,0.1)", "A hairline inside a terminal."),
    "ansi.red": ("#ff7b7f", "#ff7b7f", ""),
    "ansi.green": ("#5fd28b", "#5fd28b", ""),
    "ansi.yellow": ("#f2c14e", "#f2c14e", ""),
    "ansi.blue": ("#6cb6ff", "#6cb6ff", ""),
    "ansi.magenta": ("#e58cf5", "#e58cf5", ""),
    "ansi.cyan": ("#5fd0dc", "#5fd0dc", ""),
    # diff lines
    "diff.add.fill": ("#e8f6ed", "#0f2418", "An added line's band."),
    "diff.add.mark": ("#14733f", "#4cc27a", "The + sign and the gutter edge."),
    "diff.del.fill": ("#fcecec", "#2a1112", "A removed line's band."),
    "diff.del.mark": ("#c0282e", "#ff6b70", "The - sign and the gutter edge."),
    # charts
    "chart.line": (n(10), n(10), "A sparkline's stroke: data reads in text colour."),
    "chart.area": ("rgba(21,23,43,0.05)", "rgba(255,255,255,0.06)", "Under a sparkline."),
    "chart.bar": (n(7), n(7), "A bar in a small chart."),
    "chart.bar-strong": (n(12), n(12), "The bar that is the point (now, this run). Ink."),
    "chart.grid": (n(4), n(4), "Chart gridlines and the bar track."),
    # overlay
    "overlay.scrim": ("rgba(11,13,26,0.36)", "rgba(3,4,10,0.72)", "Behind a dialog or sheet."),
    # shadow colours
    "shadow.color-1": ("rgba(21,23,43,0.04)", "rgba(0,0,0,0.40)", ""),
    "shadow.color-2": ("rgba(21,23,43,0.12)", "rgba(0,0,0,0.60)", ""),
    # W3 mono: a card's lift. Light: a 1 px shadow under the hairline, so a card sits on the page.
    # Dark: no shadow can show on black, so a 1 px light on the card's top edge does it instead.
    "shadow.color-card": ("rgba(21,23,43,0.05)", "rgba(0,0,0,0)", "Under a card, light only."),
    "highlight.card": ("rgba(255,255,255,0)", "rgba(255,255,255,0.05)", "The top edge of a card, dark only."),
    # W4 dawn: glass, only over the Dawn picture or the stage (Home's window, chips on a screen's
    # picture). Never on a plain page, where it would only look grey. A 22 px blur behind it.
    "glass.fill": ("rgba(255,255,255,0.78)", "rgba(16,19,38,0.72)", "Glass over the Dawn picture: Home's cards."),
    "glass.line": ("rgba(255,255,255,0.6)", "rgba(255,255,255,0.12)", "Glass's edge."),
    "glass.highlight": ("rgba(255,255,255,0.8)", "rgba(255,255,255,0.1)", "Glass's top light."),
    "glass.shadow": ("rgba(10,14,40,0.35)", "rgba(0,0,0,0.6)", "Under glass."),
    "glass.dark-fill": ("rgba(10,13,30,0.55)", "rgba(10,13,30,0.6)", "Dark glass on a picture: a chip on a screen, the sidebar's foot."),
    "glass.dark-line": ("rgba(255,255,255,0.16)", "rgba(255,255,255,0.14)", ""),
    # web: Dawn's one-off mixes, named (were color-mix() in the screens). Hover on dark glass; the white
    # pill on the night and its press; the hairline inside a violet or white pill; the ring of a screen
    # you control; the window edge of the sign-in card on the Dawn picture.
    "glass.dark-hover": ("rgba(52,55,74,0.62)", "rgba(52,55,74,0.66)", "Hover on dark glass."),
    "night.pill-hover": ("rgba(255,255,255,0.86)", "rgba(255,255,255,0.86)", "The white pill on the night, hovered."),
    "night.pill-pressed": ("rgba(255,255,255,0.76)", "rgba(255,255,255,0.76)", "The white pill on the night, pressed."),
    "night.ring": ("rgba(255,255,255,0.22)", "rgba(255,255,255,0.22)", "The ring round a screen a person controls, at rest."),
    "pill.edge-on-primary": ("rgba(255,255,255,0.4)", "rgba(255,255,255,0.4)", "A hairline inside the violet pill (its count, its key)."),
    "pill.edge-on-light": ("rgba(11,13,26,0.3)", "rgba(7,9,20,0.3)", "A hairline inside the white pill on the night."),
    "window.edge": ("rgba(21,23,43,0.08)", "rgba(255,255,255,0.08)", "The edge of a window on the Dawn picture (sign-in)."),
    # web: soft halos round a field or a chosen choice (outlines, so studio sees them)
    "halo.focus": ("rgba(109,74,255,0.22)", "rgba(160,138,255,0.22)", "The halo round a focused field."),
    "halo.bad": ("rgba(229,72,77,0.18)", "rgba(229,72,77,0.18)", "The halo round a focused field in error."),
    "halo.accent": ("rgba(109,74,255,0.14)", "rgba(132,102,255,0.14)", "The halo round a chosen choice."),
    "accent.half": ("rgba(109,74,255,0.5)", "rgba(132,102,255,0.5)", "The violet at half strength: a rung of the ladder on its way."),
    # W4 dawn: the scrim that keeps white words readable on the Dawn picture, darkest where they sit;
    # dark mode dims the whole picture by about a third.
    "dawn.scrim": ("rgba(8,12,34,0.55)", "rgba(5,7,20,0.66)", "Under the words on the Dawn picture."),
    "dawn.dim": ("rgba(9,11,22,0)", "rgba(9,11,22,0.36)", "Over the Dawn picture at night."),
    # W4 dawn: the night sidebar, the same night sky in both themes with the sunrise at its foot.
    # Its own words, lines and fills: white on the night, every one AA where it sits.
    "sidebar.sky-1": ("#0a1030", "#060a1f", "The sidebar's sky, top to foot."),
    "sidebar.sky-2": ("#121a42", "#0b1130", ""),
    "sidebar.sky-3": ("#1b2452", "#121a42", ""),
    "sidebar.sky-4": ("#2e3566", "#252c58", ""),
    "sidebar.glow-1": ("#6c5e86", "#4b4470", "The sunrise at the sidebar's foot."),
    "sidebar.glow-2": ("#b98e8c", "#7c6680", ""),
    "sidebar.text": ("#ffffff", "#ffffff", "Words on the night sidebar."),
    "sidebar.text-secondary": ("rgba(255,255,255,0.8)", "rgba(255,255,255,0.78)", "A place's name at rest."),
    "sidebar.text-tertiary": ("rgba(255,255,255,0.64)", "rgba(255,255,255,0.6)", "Group headings, counts, keys."),
    "sidebar.line": ("rgba(255,255,255,0.12)", "rgba(255,255,255,0.1)", ""),
    "sidebar.fill-hover": ("rgba(255,255,255,0.07)", "rgba(255,255,255,0.06)", ""),
    "sidebar.fill-selected": ("rgba(255,255,255,0.14)", "rgba(255,255,255,0.12)", "The place you are on."),
    "sidebar.focus": (vi("300"), vi("300"), "The focus ring on the night: violet 300 holds 3:1 there."),
    "sidebar.foot": ("rgba(8,11,32,0.62)", "rgba(6,8,24,0.6)", "The glass the org switcher sits on, over the glow."),
    # web: the hover lift of a card that opens something (--shadow-hover).
    "shadow.color-hover": ("rgba(0,0,0,0.16)", "rgba(0,0,0,0.7)", "The soft drop under a card that lifts on hover."),
    # web: one step stronger than pressed, for a skeleton's band and a few bars. Was hand-written in aliases.css.
    "fill.strong": ("rgba(0,0,0,0.09)", "rgba(255,255,255,0.13)", "A skeleton's band, a quiet bar on a well."),
    # web: the board, the drafting sheet instruments sit on (instrument.css). Wires are hairlines a step
    # off the surface; marks are the registration crosses. Were hand-written in aliases.css.
    "board.wire": ("rgba(0,0,0,0.13)", "rgba(255,255,255,0.11)", "A wire on a board: a hairline a step off the surface."),
    "board.wire-strong": ("rgba(0,0,0,0.3)", "rgba(255,255,255,0.26)", "A wire that carries something now."),
    "board.mark": ("rgba(0,0,0,0.2)", "rgba(255,255,255,0.2)", "Registration crosses on a board's corners."),
    "board.dot": ("rgba(0,0,0,0.07)", "rgba(255,255,255,0.055)", "The dotted grid of a board or a picture stage."),
    # web: a Site API's fields, one hue per field so a column on the page and in the data read as one.
    # Marks, outlines and wires only, never text and never a surface. No pink or red in the set.
    "field.1": ("#2a78d6", "#3987e5", "A Site API field's hue: marks and wires only."),
    "field.2": ("#eb6834", "#d95926", ""),
    "field.3": ("#1baf7a", "#199e70", ""),
    "field.4": ("#eda100", "#c98500", ""),
    "field.5": ("#6250d6", "#9085e9", ""),
    "field.6": ("#008300", "#2f9e2f", ""),
    # web: the hours a person was asleep, a band on the day line.
    "night": ("#3b4cca", "#8f9bff", "The night band on a day line: the hours the person was asleep."),
}

# W3 mono: the kind of work. Each kind has a mark (a glyph, the card's top edge, a timeline tick),
# text (its label), soft (the mark's tile) and line. Hues stay out of status's way: no green, amber
# or red, and running is ink. Picked for distance under protanopia, deuteranopia and tritanopia
# (checked below), and always drawn with the kind's own icon and word, never colour alone.
KINDS = {
    #          mark (L, D)              text (L, D)              soft (L, D)              line (L, D)
    # W4 dawn: tests are plum, off the agent's violet.
    "test":   (("#7a2f7d", "#c66cc6"), ("#7a2f7d", "#d98ad8"), ("#f8eff8", "#22102a"), ("#e8cde8", "#542a5a"), "Tests: a suite run with openajar run."),
    "design": (("#c8337f", "#f06bb0"), ("#ad2a6d", "#f58cc2"), ("#fcf0f6", "#26101c"), ("#f3c9de", "#5c2443"), "Design: ui_check, ui_diff, a brand change."),
    "build":  (("#0b6e88", "#3fc6dc"), ("#0b6e88", "#5fd3e6"), ("#ebf6f9", "#09201f"), ("#b9dde7", "#18505b"), "Build and CI: a build, an install, lint and types."),
    "server": (("#1e40af", "#4c7dff"), ("#1e40af", "#86a8ff"), ("#eef1fb", "#0e1730"), ("#c6cff0", "#243d73"), "Dev server: a service that stays on."),
}
for k, (mk, tx, so, ln, note) in KINDS.items():
    COLORS[f"kind.{k}.mark"] = (mk[0], mk[1], note)
    COLORS[f"kind.{k}.text"] = (tx[0], tx[1], "")
    COLORS[f"kind.{k}.soft"] = (so[0], so[1], "")
    COLORS[f"kind.{k}.line"] = (ln[0], ln[1], "")

# identity and brand: the same in both themes (fills on small marks, never surfaces)
FIXED_COLORS = {
    "identity.1": "#8f6833", "identity.2": "#3f76a0", "identity.3": "#33796f", "identity.4": "#8c5fa8",
    "identity.5": "#b84a33", "identity.6": "#527a32", "identity.7": "#b0477a", "identity.8": "#62697a",
    "identity.ink": "#ffffff",
    "status.ink": "#111111",
    "brand.mark": "#6d4aff",
}
for _k, _v in VIOLET.items():
    FIXED_COLORS[f"violet.{_k}"] = _v
# W4 dawn: the openajar mark "Window a" (components/ui/misc.tsx Logo): a white tile with a violet a.
FIXED_COLORS.update({
    "mark.tile-top": "#ffffff", "mark.tile-bottom": "#e7e3fb", "mark.edge": "rgba(10,10,20,0.12)",
    "mark.a-top": "#8a6dff", "mark.a-bottom": "#4b2fe0",
    # A switch's thumb: white on the control grey and on the violet, in both themes.
    "control.thumb": "#ffffff",
})
# W4 dawn: the wrapper tile, the dock's squircle with a soft top light, for the app's own things (a kind
# of ask, a channel, a settings section). Never for an action. Each is a gradient top to bottom with a
# white glyph; the white holds 3:1 on the lower stop (a graphic, WCAG 1.4.11).
TILES = {
    "violet": ("#9a82ff", "#5b37e6"), "blue": ("#7cc4ff", "#2563eb"), "pink": ("#ff9cc7", "#d6337f"),
    "amber": ("#ffd877", "#d97706"), "green": ("#7be3a6", "#16a05a"), "ink": ("#4a505e", "#15171d"),
    "teal": ("#6fdde6", "#0b8aa0"),
    # The danger zone's tile: what cannot be undone. A mark beside red words, never a status of its own.
    "red": ("#ff9a8f", "#d4372a"),
}
for _k, (_top, _bottom) in TILES.items():
    FIXED_COLORS[f"tile.{_k}-top"] = _top
    FIXED_COLORS[f"tile.{_k}-bottom"] = _bottom

SPACE = {"0": 0, "px": 1, "1": 4, "2": 8, "3": 12, "4": 16, "5": 20, "6": 24, "8": 32, "10": 40, "12": 48, "16": 64}
# W3 mono: crisper corners (cards 10, not 12); a display size for Home's sentence.
# W4 dawn: the mockup's 14 px cards were too round for the owner; cards stay 10 and the rest steps
# with them: 4 marks and keys, 6 fields and menu items, 8 pictures and menus, 10 cards and dialogs,
# 14 the stage and Home's window, full for every button, chip and the control pill.
RADIUS = {"none": 0, "sm": 4, "md": 6, "lg": 8, "xl": 10, "2xl": 14, "full": 999}
SIZE = {"font.11": 11, "font.12": 12, "font.13": 13, "font.14": 14, "font.15": 15, "font.16": 16, "font.17": 17, "font.18": 18,
        "font.20": 20, "font.24": 24, "font.26": 26, "font.28": 28, "font.32": 32, "font.34": 34, "font.40": 40, "font.44": 44}
LEADING = {"16": 16, "18": 18, "20": 20, "22": 22, "24": 24, "26": 26, "28": 28, "32": 32, "36": 36, "40": 40, "44": 44, "50": 50}
WEIGHT = {"regular": 400, "medium": 500, "semibold": 600}
# W2: IBM Plex, Workbench's choice (an instrument's character, not the face every dev tool uses).
# Geist stays next in line, so a page that ships only Geist (foundations.html) still renders in it.
# W3 mono: Geist and Geist Mono, back to web/DESIGN.md's faces.
# web: the app registers the faces from @fontsource-variable under these names (src/main.tsx).
SANS = ["Geist Variable", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"]
MONO = ["Geist Mono Variable", "ui-monospace", "SF Mono", "Menlo", "monospace"]

# role -> (family, size, weight, leading, tracking em, note)
# W3 mono: tight headings (Geist at -0.03 to -0.045 em), 14 px body like every Vercel surface,
# table heads at 13 medium in the tertiary ink with no band, numbers in Geist Mono 13.
# W4 dawn: Full's scale, one step larger for the reading roles (a person who sits with it all day):
# body 15/22, a row's second line 14/20, the lede 17/26, sections 18/26, the page title 34/40 and
# Home's sentence 44/50. Data roles (table heads, numbers, code, captions) keep Mono's sizes.
TYPE = {
    "display": ("sans", 44, "semibold", 50, -0.04, "Home's sentence. One per app, at most."),
    "page-title": ("sans", 34, "semibold", 40, -0.032, "One per page, the h1."),
    "section-title": ("sans", 18, "semibold", 26, -0.015, "A section of a page, a dialog title."),
    "page-title-compact": ("sans", 26, "semibold", 32, -0.03, "The page title, and Home's sentence, on a phone."),
    "lede": ("sans", 17, "regular", 26, -0.01, "The sentence under a page title. text.secondary."),
    "card-title": ("sans", 15, "semibold", 22, -0.01, "The title of a card, a sheet section, a list group."),
    "headline": ("sans", 32, "semibold", 40, -0.035, "The sentence that opens a focused page: a sign-in, a list's summary, an amount to pay."),
    "heading": ("sans", 20, "semibold", 28, -0.02, "A heading inside a wide page, a step's question, a figure on a card."),
    "table-header": ("sans", 13, "medium", 20, -0.005, "Column heads. text.tertiary, sentence case, no band."),
    "label": ("sans", 14, "medium", 20, -0.006, "Buttons, tabs, form labels."),
    "body": ("sans", 15, "regular", 22, -0.006, "Everything read: cells, paragraphs."),
    "secondary": ("sans", 14, "regular", 20, -0.003, "A row's second line, descriptions. text.secondary."),
    "caption": ("sans", 12, "regular", 16, 0, "Timestamps, footnotes, help under a field. text.tertiary."),
    "number": ("mono", 13, "regular", 20, 0, "Numbers in tables and lists: tabular figures, right aligned."),
    "stat": ("sans", 28, "semibold", 32, -0.035, "The big number of a stat tile. Tabular figures."),
    "code": ("mono", 13, "regular", 20, 0, "Ids, hashes, branches, commands, terminal output."),
    "code-caption": ("mono", 12, "regular", 16, 0, "A command, branch or id on a row's second line."),
    "eyebrow": ("mono", 11, "medium", 16, 0.04, "A kind's word over a card title, a section's label. The one caps style."),
    "micro": ("sans", 11, "medium", 16, 0.01, "Avatar initials and keyboard keys only."),
}
TRACKING = {"tight": -0.024, "snug": -0.012, "body": -0.006, "none": 0, "wide": 0.01}

# web: settle (280 ms on ease-settle) is a row or panel coming to rest; it was hand-written in aliases.css.
DURATION = {"fast": 100, "base": 150, "slow": 200, "settle": 280}
EASE = {"standard": [0.2, 0, 0, 1], "enter": [0, 0, 0.2, 1], "exit": [0.4, 0, 1, 1], "settle": [0.16, 1, 0.3, 1]}
Z = {"base": 0, "raised": 1, "sticky": 10, "header": 20, "dropdown": 30, "overlay": 40, "dialog": 50, "toast": 60, "tooltip": 70}
# W4 dawn: Full's controls are a step taller (a 36 button, 40 large, fields 40).
CONTROL = {"sm": 30, "md": 36, "lg": 40}
# web: the shell's sizes and the page's measure. Were hand-written in aliases.css.
LAYOUT = {"topbar-height": 56, "rail-width": 248, "tabbar-height": 56, "page-max": 1200}  # W4 dawn: 248, 1200
# web: the widths a layout changes at (Tailwind's sm to 2xl, the same values, now named here).
BREAKPOINT = {"sm": 640, "md": 768, "lg": 1024, "xl": 1280, "2xl": 1536}
ROW = {"compact": 40, "default": 52, "relaxed": 60, "header": 40}


# ---------------------------------------------------------------------------------------------
def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def parse(v):
    v = v.strip()
    if v.startswith("#"):
        return (*hex_rgb(v), 1.0)
    inner = v[v.index("(") + 1:-1].split(",")
    r, g, b, a = (float(x) for x in inner)
    return (r, g, b, a)


def dtcg_color(v):
    if v.startswith("{"):
        return v
    r, g, b, a = parse(v)
    out = {"colorSpace": "srgb", "components": [round(r / 255, 6), round(g / 255, 6), round(b / 255, 6)]}
    if a != 1:
        out["alpha"] = a
    out["hex"] = "#%02x%02x%02x" % (int(r), int(g), int(b))
    return out


def set_path(tree, path, node):
    cur = tree
    for p in path[:-1]:
        cur = cur.setdefault(p, {})
    cur[path[-1]] = node


def css_name(path):
    return "--" + "-".join(path)


def tok(type_, value, desc=None, cssvar=None):
    d = {"$type": type_, "$value": value}
    if desc:
        d["$description"] = desc
    if cssvar:
        d["$extensions"] = {"openajar": {"cssVar": cssvar}}
    return d


def px(v):
    return {"value": v, "unit": "px"}


base, light, dark = {}, {}, {}
for i, (lv, dv) in enumerate(zip(N_LIGHT, N_DARK)):
    set_path(light, ["color", "neutral", str(i)], tok("color", dtcg_color(lv), cssvar="--neutral-%d" % i))
    set_path(dark, ["color", "neutral", str(i)], tok("color", dtcg_color(dv), cssvar="--neutral-%d" % i))
for name, (lv, dv, note) in COLORS.items():
    path = ["color", *name.split(".")]
    set_path(light, path, tok("color", dtcg_color(lv), note or None, css_name(name.split("."))))
    set_path(dark, path, tok("color", dtcg_color(dv), note or None, css_name(name.split("."))))
for name, v in FIXED_COLORS.items():
    set_path(base, ["color", *name.split(".")], tok("color", dtcg_color(v), cssvar=css_name(name.split("."))))
for k, v in SPACE.items():
    set_path(base, ["space", k], tok("dimension", px(v)))
for k, v in RADIUS.items():
    set_path(base, ["radius", k], tok("dimension", px(v)))
for k, v in SIZE.items():
    set_path(base, k.split("."), tok("dimension", px(v)))
for k, v in LEADING.items():
    set_path(base, ["line-height", k], tok("dimension", px(v)))
for k, v in WEIGHT.items():
    set_path(base, ["font-weight", k], tok("fontWeight", v))
set_path(base, ["font-family", "sans"], tok("fontFamily", SANS))
set_path(base, ["font-family", "mono"], tok("fontFamily", MONO))
for role, (fam, size, weight, lead, track, note) in TYPE.items():
    set_path(base, ["type", role], tok("typography", {
        "fontFamily": "{font-family.%s}" % fam,
        "fontSize": "{font.%d}" % size,
        "fontWeight": "{font-weight.%s}" % weight,
        "lineHeight": "{line-height.%d}" % lead,
        "letterSpacing": px(round(track * size, 2)),
    }, note))
for k, v in DURATION.items():
    set_path(base, ["duration", k], tok("duration", {"value": v, "unit": "ms"}))
for k, v in EASE.items():
    set_path(base, ["ease", k], tok("cubicBezier", v))
for k, v in Z.items():
    set_path(base, ["z", k], tok("number", v))
for k, v in CONTROL.items():
    set_path(base, ["control-height", k], tok("dimension", px(v)))
for k, v in ROW.items():
    set_path(base, ["row-height", k], tok("dimension", px(v)))
for k, v in LAYOUT.items():
    set_path(base, ["layout", k], tok("dimension", px(v)))
for k, v in BREAKPOINT.items():
    set_path(base, ["breakpoint", k], tok("dimension", px(v)))
set_path(base, ["border-width", "hairline"], tok("dimension", px(1)))
set_path(base, ["border-width", "focus"], tok("dimension", px(2)))
set_path(base, ["border-width", "mark"], tok("dimension", px(1.5), "The ring of a hollow status mark."))
set_path(base, ["focus", "offset"], tok("dimension", px(2), "Outline offset of the focus ring; inset rings use -2px."))


def shadow(layers):
    return [{"color": "{color.shadow.%s}" % c, "offsetX": px(x), "offsetY": px(y), "blur": px(b), "spread": px(s)}
            for c, x, y, b, s in layers]


SHADOWS = {
    "card": [("color-card", 0, 1, 2, 0)],
    "hover": [("color-card", 0, 1, 2, 0), ("color-hover", 0, 12, 32, -12)],
    "popover": [("color-1", 0, 1, 2, 0), ("color-2", 0, 8, 24, -4)],
    "dialog": [("color-1", 0, 1, 2, 0), ("color-2", 0, 16, 40, -8)],
}
for k, v in SHADOWS.items():
    set_path(base, ["shadow", k], tok("shadow", shadow(v)))

doc = {
    "$schema": "https://www.designtokens.org/schemas/2025.10/format.json",
    "$description": "openajar foundations. Source of truth for tokens.css. Themes map: base holds what both themes share, light and dark lay over it.",
    "base": base, "light": light, "dark": dark,
}
emit(OUT / "tokens.json", json.dumps(doc, indent=2) + "\n")

# ---------------------------------------------------------------------------------------------
# CSS
def cssval_color(v):
    if v.startswith("{"):
        inner = v[1:-1].split(".")
        return "var(%s)" % css_name(inner[1:] if inner[0] == "color" else inner)
    if v.startswith("rgba"):
        r, g, b, a = parse(v)
        return "rgb(%d %d %d / %s)" % (r, g, b, ("%g" % a))
    return v


def color_var(name):
    return css_name(name.split("."))


def theme_block(idx):
    lines = []
    ramp = N_LIGHT if idx == 0 else N_DARK
    lines.append("  /* Neutral ramp, 0 to 12 by luminance. Use the roles below, not steps. */")
    for i, v in enumerate(ramp):
        lines.append("  --neutral-%d: %s;" % (i, v))
    group = None
    for name, vals in COLORS.items():
        g = name.split(".")[0]
        if g != group:
            lines.append("")
            group = g
        lines.append("  %s: %s;" % (color_var(name), cssval_color(vals[idx])))
    return lines


css = []
css.append("""/*
 * openajar foundations: design tokens.
 *
 * GENERATED by web/tokens/build-tokens.py, with web/tokens/tokens.json. Do not edit: change the
 * script and run it. src/styles/tokens.test.ts fails if the two drift.
 *
 * Light is the default. Dark comes from the system (prefers-color-scheme) unless the page
 * pins a theme with data-theme="light" or data-theme="dark" on <html>. The dark values are
 * written twice for that reason, once per route; they are identical.
 *
 * Rules for every value: web/DESIGN.md. Old names the screens still use: aliases.css.
 */
""")
css.append(":root {")
css.append("  color-scheme: light;")
css += theme_block(0)
css.append("")
css.append("  /* Identity hues: a fill on a small mark (avatar, workspace), never a surface or text. White initials pass 4.5:1. */")
css.append("  /* status-ink: a number or glyph drawn on a status dot colour (a finding pin). 4.5:1 on ok, warn and bad dots. */")
for k, v in FIXED_COLORS.items():
    css.append("  %s: %s;" % (color_var(k), cssval_color(v)))
css.append("")
css.append("  /* Type */")
css.append("  --font-family-sans: %s;" % ", ".join("'%s'" % f if " " in f else f for f in SANS))
css.append("  --font-family-mono: %s;" % ", ".join("'%s'" % f if " " in f else f for f in MONO))
for k, v in SIZE.items():
    css.append("  --%s: %dpx;" % (k.replace(".", "-"), v))
for k, v in LEADING.items():
    css.append("  --line-height-%s: %dpx;" % (k, v))
for k, v in WEIGHT.items():
    css.append("  --font-weight-%s: %d;" % (k, v))
css.append("")
css.append("  /* Type roles. Use the role, never a raw size: font-size: var(--type-body-font-size). */")
for role, (fam, size, weight, lead, track, note) in TYPE.items():
    p = "--type-%s" % role
    css.append("  /* %s */" % note)
    css.append("  %s-font-family: var(--font-family-%s);" % (p, fam))
    css.append("  %s-font-size: var(--font-%d);" % (p, size))
    css.append("  %s-font-weight: var(--font-weight-%s);" % (p, weight))
    css.append("  %s-line-height: var(--line-height-%d);" % (p, lead))
    css.append("  %s-tracking: %spx;" % (p, "%g" % round(track * size, 2)))
css.append("")
css.append("  /* Space: the 4 px grid. Nothing in between. space-px is the hairline gap between tiles. */")
for k, v in SPACE.items():
    css.append("  --space-%s: %dpx;" % (k, v))
css.append("")
css.append("  /* Radius */")
for k, v in RADIUS.items():
    css.append("  --radius-%s: %dpx;" % (k, v))
css.append("")
css.append("  /* Sizes */")
for k, v in CONTROL.items():
    css.append("  --control-height-%s: %dpx;" % (k, v))
for k, v in ROW.items():
    css.append("  --row-height-%s: %dpx;" % (k, v))
for k, v in LAYOUT.items():
    css.append("  --%s: %dpx;" % (k, v))
for k, v in BREAKPOINT.items():
    css.append("  --breakpoint-%s: %dpx;" % (k, v))
css.append("  --border-width-hairline: 1px;")
css.append("  --border-width-focus: 2px;")
css.append("  --border-width-mark: 1.5px;")
css.append("  --focus-offset: 2px;")
css.append("")
css.append("  /* Elevation: a card's 1 px lift (--shadow-card, with --highlight-card in dark), and floating layers. */")
for k, v in SHADOWS.items():
    css.append("  --shadow-%s: %s;" % (k, ", ".join("%dpx %dpx %dpx %dpx var(--shadow-%s)" % (x, y, b, s, c) for c, x, y, b, s in v)))
css.append("")
css.append("  /* Motion: only where it explains. Reduced motion sets every duration to 0. */")
for k, v in DURATION.items():
    css.append("  --duration-%s: %dms;" % (k, v))
for k, v in EASE.items():
    css.append("  --ease-%s: cubic-bezier(%s);" % (k, ", ".join("%g" % x for x in v)))
css.append("")
css.append("  /* Layers */")
for k, v in Z.items():
    css.append("  --z-%s: %d;" % (k, v))
css.append("}")
css.append("")
dark_lines = theme_block(1)
css.append("@media (prefers-color-scheme: dark) {")
css.append("  :root:not([data-theme='light']) {")
css.append("    color-scheme: dark;")
css += ["  " + l if l else "" for l in dark_lines]
css.append("  }")
css.append("}")
css.append("")
css.append(":root[data-theme='dark'] {")
css.append("  color-scheme: dark;")
css += dark_lines
css.append("}")
css.append("")
css.append("@media (prefers-reduced-motion: reduce) {")
css.append("  :root { %s }" % " ".join("--duration-%s: 0ms;" % k for k in DURATION))
css.append("}")
emit(CSS_OUT, "\n".join(css) + "\n")

# web: the Tailwind theme (class name -> token) and the same keys as data for the design rules.
import theme  # noqa: E402  (tokens/theme.py, beside this script)

emit(THEME_OUT, theme.theme_css(BREAKPOINT, Z, DURATION))
emit(OUT / "theme-keys.json", json.dumps(theme.theme_keys(BREAKPOINT, Z, DURATION), indent=2) + "\n")

# ---------------------------------------------------------------------------------------------
# contrast
def resolve(v, idx):
    if v.startswith("{"):
        name = v[1:-1]
        if name.startswith("color.neutral."):
            return (N_LIGHT if idx == 0 else N_DARK)[int(name.split(".")[-1])]
        return resolve(COLORS[name[6:]][idx], idx)
    return v


def rgb_of(name, idx, under=None):
    if name in FIXED_COLORS:
        v = FIXED_COLORS[name]
    else:
        v = resolve(COLORS[name][idx], idx)
    r, g, b, a = parse(v)
    if a < 1:
        ur, ug, ub = rgb_of(under, idx) if under else (255, 255, 255)
        return (r * a + ur * (1 - a), g * a + ug * (1 - a), b * a + ub * (1 - a))
    return (r, g, b)


def lum(c):
    def ch(x):
        x /= 255
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    r, g, b = c
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


SURF = ["surface.page", "surface.card", "surface.inset", "surface.raised"]
PAIRS = []  # (fg, bg, min, label)
for t in ["text.primary", "text.secondary", "text.tertiary", "accent.text"]:
    for s in SURF:
        PAIRS.append((t, s, 4.5))
    PAIRS.append((t, "accent.soft", 4.5))
for t in ["text.primary", "text.secondary", "text.tertiary"]:
    PAIRS.append((t, "fill.pressed@surface.card", 4.5))
for st in ["ok", "warn", "bad", "live", "idle"]:
    for s in ["surface.page", "surface.card", "surface.inset", f"status.{st}.fill"]:
        PAIRS.append((f"status.{st}.text", s, 4.5))
    PAIRS.append((f"status.{st}.dot", "surface.card", 3.0))
PAIRS.append(("text.inverse", "surface.inverse", 4.5))
PAIRS.append(("text.inverse", "accent.fill", 4.5))
PAIRS.append(("text.inverse", "text.primary", 4.5))  # primary button
for t in ["terminal.text", "terminal.dim", "ansi.red", "ansi.green", "ansi.yellow", "ansi.blue", "ansi.magenta", "ansi.cyan"]:
    PAIRS.append((t, "terminal.bg", 4.5))
for t in ["text.primary", "text.secondary", "diff.add.mark"]:
    PAIRS.append((t, "diff.add.fill", 4.5))
for t in ["text.primary", "text.secondary", "diff.del.mark"]:
    PAIRS.append((t, "diff.del.fill", 4.5))
for s in SURF:
    PAIRS.append(("line.control", s, 3.0))
    PAIRS.append(("focus.ring", s, 3.0))
PAIRS.append(("accent.fill", "surface.card", 3.0))
PAIRS.append(("chart.line", "surface.card", 3.0))
PAIRS.append(("chart.bar-strong", "surface.card", 3.0))
for i in range(1, 9):
    PAIRS.append(("identity.ink", f"identity.{i}", 4.5))
PAIRS.append(("identity.ink", "brand.mark", 4.5))
for st in ["ok", "warn", "bad"]:
    PAIRS.append(("status.ink", f"status.{st}.dot", 4.5))
for _k in TILES:
    PAIRS.append(("identity.ink", f"tile.{_k}-bottom", 3.0))

# W3 mono: every kind's word reads on every surface and on its own tile; its mark holds 3:1.
for k in KINDS:
    for s_ in ["surface.page", "surface.card", "surface.inset", f"kind.{k}.soft"]:
        PAIRS.append((f"kind.{k}.text", s_, 4.5))
    PAIRS.append((f"kind.{k}.mark", "surface.card", 3.0))
    PAIRS.append((f"kind.{k}.mark", "surface.page", 3.0))

# W4 dawn: the primary pill's words, and the new kinds of surface.
PAIRS.append(("action.on-primary", "action.primary", 4.5))
PAIRS.append(("action.on-primary", "action.primary-hover", 4.5))
PAIRS.append(("text.inverse", "surface.inverse", 4.5))
for t in ["text.primary", "text.secondary", "text.tertiary", "accent.text"]:
    PAIRS.append((t, "status.live.fill", 4.5))

rows, fails = [], 0
for fg, bg, need in PAIRS:
    res = []
    for idx, th in ((0, "light"), (1, "dark")):
        if "@" in bg:
            b, under = bg.split("@")
            bgc = rgb_of(b, idx, under)
        else:
            bgc = rgb_of(bg, idx, "surface.card")
        r = ratio(rgb_of(fg, idx, "surface.card"), bgc)
        res.append(r)
    ok = all(r >= need for r in res)
    fails += (not ok)
    rows.append((fg, bg, need, res, ok))
for fg, bg, need, res, ok in rows:
    print("%-4s %-24s on %-28s need %.1f  light %5.2f  dark %5.2f" % ("ok" if ok else "FAIL", fg, bg, need, res[0], res[1]))


# W4 dawn: layered surfaces. Each is drawn bottom to top: a solid colour, then translucent layers, then
# the words (themselves translucent on the night sidebar). Checked in both themes like the pairs.
def over(top, under_rgb):
    r, g, b, a = parse(top)
    return (r * a + under_rgb[0] * (1 - a), g * a + under_rgb[1] * (1 - a), b * a + under_rgb[2] * (1 - a))


def layered(idx, stack):
    """stack: a colour name or literal first (opaque), then names or literals laid over it."""
    def val(x):
        if x.startswith("#") or x.startswith("rgba"):
            return x
        return resolve(FIXED_COLORS[x] if x in FIXED_COLORS else COLORS[x][idx], idx)
    base_rgb = parse(val(stack[0]))[:3]
    for layer in stack[1:]:
        base_rgb = over(val(layer), base_rgb)
    return base_rgb


# The Dawn picture's darkest and lightest skies, and its mountains, where glass and words sit on it.
DAWN = ["#0d1430", "#33487c", "#7284ab", "#c6a89e", "#fff0d2"]
LAYERED = []  # (fg stack, bg stack, need, label)
SIDE_BG = ["sidebar.sky-1", "sidebar.sky-2", "sidebar.sky-3", "sidebar.sky-4"]
for bg in SIDE_BG:
    for t, need in (("sidebar.text", 4.5), ("sidebar.text-secondary", 4.5), ("sidebar.text-tertiary", 4.5), ("sidebar.focus", 3.0)):
        LAYERED.append(([bg, t], [bg], need, f"{t} on {bg}"))
    LAYERED.append(([bg, "sidebar.fill-selected", "sidebar.text"], [bg, "sidebar.fill-selected"], 4.5, f"selected place on {bg}"))
    # On the selected place a count steps up to the secondary words (the rail does it).
    LAYERED.append(([bg, "sidebar.fill-selected", "sidebar.text-secondary"], [bg, "sidebar.fill-selected"], 4.5, f"a count on the selected place on {bg}"))
for bg in ["sidebar.glow-1", "sidebar.glow-2"]:
    for t in ("sidebar.text", "sidebar.text-secondary", "sidebar.text-tertiary"):
        LAYERED.append(([bg, "sidebar.foot", t], [bg, "sidebar.foot"], 4.5, f"{t} on the foot over {bg}"))
for sky in DAWN:
    for t in ("text.primary", "text.secondary"):
        LAYERED.append(([sky, "dawn.dim", "glass.fill", t], [sky, "dawn.dim", "glass.fill"], 4.5, f"{t} on glass over {sky}"))
    LAYERED.append(([sky, "glass.dark-fill", "#ffffff"], [sky, "glass.dark-fill"], 4.5, f"white on dark glass over {sky}"))
# Home's words sit on the left of the picture, over the scrim, where the sky is dark to mid.
for sky in DAWN[:4]:
    LAYERED.append(([sky, "dawn.dim", "dawn.scrim", "rgba(255,255,255,0.86)"], [sky, "dawn.dim", "dawn.scrim"], 4.5, f"the lede on the picture over {sky}"))
for fg, bg, need, label in LAYERED:
    res = [ratio(layered(idx, fg), layered(idx, bg)) for idx in (0, 1)]
    ok = all(r >= need for r in res)
    fails += (not ok)
    rows.append((label, "", need, res, ok))
    print("%-4s %-62s need %.1f  light %5.2f  dark %5.2f" % ("ok" if ok else "FAIL", label, need, res[0], res[1]))
print("pairs", len(rows), "fails", fails)

# W3 mono: colour-blind check. The kind marks against each other and against the status dots, in
# OKLab after simulating protanopia, deuteranopia and tritanopia (Machado 2009, severity 1). A
# distance under 0.04 is too close to tell apart at a glance; the check fails there.
CVD = {"normal": None,
       "protanopia": [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
       "deuteranopia": [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.011820, 0.042940, 0.968881]],
       "tritanopia": [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602], [0.004733, 0.691367, 0.303900]]}


def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _oklab(c):
    r, g, b = c
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s, 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def _seen(hexv, mat):
    c = [_lin(x) for x in hex_rgb(resolve(hexv, 0) if hexv.startswith("{") else hexv)]
    if mat:
        c = [max(0.0, min(1.0, sum(mat[i][j] * c[j] for j in range(3)))) for i in range(3)]
    return _oklab(c)


import itertools

cvd_fails = 0
for idx, th in ((0, "light"), (1, "dark")):
    marks = {f"kind.{k}": KINDS[k][0][idx] for k in KINDS}
    dots = {f"status.{st}": resolve(COLORS[f"status.{st}.dot"][idx], idx) for st in ["ok", "warn", "bad", "live"]}
    for mode, mat in CVD.items():
        worst = None
        for (a, x), (b, y) in itertools.combinations({**marks, **dots}.items(), 2):
            if a.startswith("status") and b.startswith("status"):
                continue
            dist = sum((p - q) ** 2 for p, q in zip(_seen(x, mat), _seen(y, mat))) ** 0.5
            if worst is None or dist < worst[0]:
                worst = (dist, a, b)
        bad = worst[0] < 0.04
        cvd_fails += bad
        print("%-4s kinds %-5s %-12s closest pair %s and %s, %.3f apart" % ("FAIL" if bad else "ok", th, mode, worst[1], worst[2], worst[0]))
fails += cvd_fails
names = set()


def leaves(t, path):
    for k, v in t.items():
        if k.startswith("$"):
            continue
        if isinstance(v, dict) and "$value" in v:
            names.add(".".join(path + [k]))
        elif isinstance(v, dict):
            leaves(v, path + [k])


for th in ("base", "light", "dark"):
    leaves(doc[th], [])
print("tokens", len(names))
if DRIFT:
    print("FAIL generated files are out of date, run python3 web/tokens/build-tokens.py:", ", ".join(DRIFT))
sys.exit(1 if fails or DRIFT else 0)
