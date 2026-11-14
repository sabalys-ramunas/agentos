# Studio: check the pages you build

Studio gives an AI agent a real browser to look at the web pages it builds. The agent changes
the code, asks studio whether the page is all right, and fixes what studio measured, before you
ever see it. In an agentos environment ([ENVIRONMENTS.md](ENVIRONMENTS.md)), studio checks the
environment's dev server: `ui_check(url="http://localhost:3000")` measures the page on the VM.
On its own, studio runs on your computer and needs no node and no account.

Studio is new. This page says what it does today and, just as plainly, what it does not.

## Start it

```
uv sync --extra studio      # once: the browser library and the image library
uv run agentos studio up    # starts studio on this computer, at http://127.0.0.1:7460
```

It prints one `claude mcp add ...` line. Run that line once, and Claude Code can use studio in
every new session. The same line, its token and the studio's pid are also kept in
`~/.agentos/studio/up.json` (readable by you only), so a script can read them. One studio runs
per home: a second `studio up` with the same home refuses and names the first. Two checkouts on
one computer each need their own HOME and port. Stop a studio with Ctrl-C or `kill <its pid>`,
never by name: another one may be running. Studio uses the Google Chrome on your computer, or Playwright's Chromium
(`python -m playwright install chromium`). Firefox and WebKit are optional; `studio up` prints
the command that adds each.

The `agentos-studio` skill teaches an agent the loops below. `studio up` puts it in Claude Code's
skills folder when Claude Code is on this computer (`--no-skill` leaves it out), and says where.
`agentos connect` installs it with the other agentos skills; from a checkout,
`integrations/claude-code/install.sh` does the same.

Then ask your agent, for example: "change the pricing page so the cards stack on a phone, and
check it with ui_check on http://localhost:3000/pricing".

## The three questions it answers

**Is this page all right?** (`ui_check`)
Studio opens the page at three widths (360, 768 and 1280 px) in light and dark, and measures:
text that is cut off, overflows or overlaps, controls hidden under something else, text too
faint to read, tap targets too small, buttons and images without a name, console errors, broken
images and requests, layout that jumps while loading, and colours that leak between themes. The
agent gets a short verdict, the worst problems with a suggested fix for each, and what could not
be checked. After a fix it asks again and reads only what changed: fixed, new, worse, still.

**What did my change do?** (`ui_diff`)
Studio keeps a picture of the page as it was (a baseline) and compares it with the page now. It
says the cause in one line, such as "the heading wrapped to two lines, which moved 17 elements
down by 40 px", or "the Save button's background changed from blue 600 to blue 700". It learns
what changes on its own (a random number, a rotating banner) and leaves that out.

**Is it on the design system?** (`system_check`, and part of `ui_check` with a profile)
Studio checks colours, spacing, corner radius and type against your design tokens, and says how
much of the page uses them and which values only look like a token.

**Another product inside your page**
A page that shows another product (a drawn or embedded shop page, a failing example shown on
purpose) can mark it with `data-studio="ignore"` (and `data-studio-why="a drawn shop page"`).
An agent can name more with `ignore` selectors, and a product's brand can keep them
(`brand.ignore`). No check judges what is inside, and the result's coverage names every region
left out and how much of the page it is. It still takes up room: if it covers your own text or
buttons, that is reported.

## Your brand, in a profile

A product profile holds a product's brand: tokens, components, the widths that matter and how it
turns dark. With one, every check uses it without the agent being told. An agent can draft a
brand from your running site (`profile_learn_brand`) or propose a change to it
(`profile_propose`), but only you approve. On a laptop:

```
uv run agentos studio profile list
uv run agentos studio profile show personal/shop --version 2
uv run agentos studio profile approve personal/shop --version 2
```

`agentos profile ...` works too when this computer runs no node: it finds the profiles studio
keeps here.

`docs/PROFILES.md` has the profile format.

## What it does not do

Studio measures. It does not judge, and it does not test what the page does.

- It does not check that the page works: logic, data, wording, whether a button does the right
  thing. Your tests do that.
- It does not have taste. A page with no findings can still look plain or wrong for your brand.
- It checks the states it is shown. Menus, dialogs, error states and pages behind a sign-in need
  the agent to name them (steps that open them) or their own address.
- Text in the review browser uses this computer's fonts, so another computer can wrap text a
  little differently. Results say so.
- Some findings can still be false alarms. Values Tailwind writes with no theme variable
  (`leading-none`, the `line-height: 1.5` its base styles put on the page) can show as off
  the design system or hard-coded.
- What studio leaves out on purpose, and says so in coverage: text inside a picture (marked
  hidden from screen readers, `inert`, or `role="img"`), text drawn under 4 px tall, the labels
  of disabled controls, lone separators such as "·", and text under a fade at the end of a list.
  Content scrolled out of a scroll box, and two layers stacked on purpose (a before and after
  view), are out of view, not covered. A colour that stays the same in both themes together
  with what is under it (white initials on a coloured avatar) is not a theme leak.
- Without `data-component` markers, findings are grouped by the nearest landmark or titled
  section, or a class that names a part, and the result says which.
- A layout shift that came right after a web font arrived is named as the font swapping in.
  It depends on the font cache, so it can come and go between runs.
- Contrast on text over a picture, a gradient or a blur uses the text's real colour and the
  pixels behind it. When the screenshot does not match the page (a filter on the text, or a
  live page that changed while it was measured), that text is reported as not judged.
- On a laptop, studio has no browser lanes: it cannot check a page inside a logged-in account
  lane. That is a later step.

## For people working on studio

The agent experience eval runs small tasks ("check this page and fix what is measured", "find
why the card moved") with the studio tools and the skill only, and records whether each task
was finished, how many calls it took, how much it read and its wrong turns. See
[tests/studio_eval/README.md](../tests/studio_eval/README.md). The design is in
[docs/design/studio.md](design/studio.md) and the rules for its tools in
[docs/design/studio-ax.md](design/studio-ax.md).
