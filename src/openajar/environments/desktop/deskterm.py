"""The agent's commands window (desk.py imports it from beside itself): the transcript of the commands the
agent runs, written to a file a read-only terminal window follows with tail. Pure text, no X.

What a person reads there must be something they can trust, and a command's output comes from cloned
repositories and fetched pages, not only from the agent. So the output cannot drive the terminal, and it
cannot pass for the helper's own lines:

- Filter keeps state between the chunks of one stream, so an escape sequence split across two reads (at
  any byte) is still seen whole. It lets through printable text, newlines, tabs, carriage returns and only
  the colour sequences the design uses (SGR foreground colours, bold, faint, italic, underline, strike,
  and their resets). Everything else goes: cursor moves, erase, scroll regions, modes, titles, the
  clipboard, device strings, charset switches, resets, C1 controls and Unicode format characters
  (bidirectional overrides, zero width characters).
- Every output line starts with a gutter the helper draws ("│ "). The command line ("❯ ...") and the
  exit line ("✓ exit 0") start at the first column, which output never reaches: it cannot move the
  cursor, the window does not wrap by itself (the helper wraps at WIDTH and draws the gutter again), and
  a carriage return redraws the gutter before the text after it.
- Output cannot set a background or reverse video, so it cannot paint a band like the helper's.

The command line and the exit line are drawn from the node's own record of the command (its begin and
end messages), never from the output stream.
"""

import os
import unicodedata

ESC = "\x1b"
WIDTH = 80        # the text's columns: the window is 84 wide, less the gutter; it does not wrap by itself
STRING_MAX = 4096  # an OSC or device string that never ends stops being swallowed after this much
CSI_MAX = 32       # a CSI's parameters past this are not a colour: the rest is skipped to its final byte
LOG_MAX = 4 * 1024 * 1024

# SGR parameters output may use: reset, bold, faint, italic, underline, strike, their resets, the 16
# foreground colours and the default; 38;5;n and 38;2;r;g;b are checked apart. No background, no reverse,
# no conceal, no blink.
SGR_OK = frozenset([0, 1, 2, 3, 4, 9, 22, 23, 24, 29, 39] + list(range(30, 38)) + list(range(90, 98)))
DROP_CATEGORIES = ("Cc", "Cf", "Cs", "Zl", "Zp")


def sgr_ok(params):
    """Is `ESC [ params m` only the colours the design uses?"""
    if params == "":
        return True
    parts = params.split(";")
    if not all(p.isdigit() or p == "" for p in parts) or len(parts) > 16:
        return False
    nums = [int(p) if p else 0 for p in parts]
    i = 0
    while i < len(nums):
        n = nums[i]
        if n == 38:
            rest = nums[i + 1:]
            if len(rest) >= 2 and rest[0] == 5 and rest[1] < 256:
                i += 3
                continue
            if len(rest) >= 4 and rest[0] == 2 and all(v < 256 for v in rest[1:4]):
                i += 5
                continue
            return False
        if n not in SGR_OK:
            return False
        i += 1
    return True


def _keep_char(ch):
    o = ord(ch)
    if ch in "\n\t\r":
        return True
    if o < 0x20 or 0x7F <= o <= 0x9F:
        return False
    return unicodedata.category(ch) not in DROP_CATEGORIES


class Filter:
    """One output stream made safe to show, chunk by chunk: the state between chunks is kept, so a
    sequence split anywhere is judged whole. feed() returns printable text, newlines, tabs, carriage
    returns and allowed SGR sequences only."""

    def __init__(self, colours=True):
        self.colours = colours
        self.state = "text"  # text, esc, inter (ESC and intermediates), csi, skip (a long CSI), string, string_esc
        self.buf = ""
        self.n = 0

    def feed(self, text):
        out = []
        for ch in text:
            self._step(ch, out)
        return "".join(out)

    def _step(self, ch, out):
        getattr(self, "_" + self.state)(ch, ord(ch), out)

    def _again(self, ch, out):
        """The sequence was never whole: it goes, and `ch` is read as text."""
        self.state = "text"
        self._step(ch, out)

    def _text(self, ch, _o, out):
        if ch == ESC:
            self.state = "esc"
        elif _keep_char(ch):
            out.append(ch)

    def _esc(self, ch, o, out):
        if ch == "[":
            self.state, self.buf = "csi", ""
        elif ch in "]PX^_":  # OSC, DCS, SOS, PM, APC: a string up to ST or BEL
            self.state, self.n = "string", 0
        elif 0x20 <= o <= 0x2F:
            self.state = "inter"
        elif ch == ESC:
            pass
        elif 0x30 <= o <= 0x7E:
            self.state = "text"  # a two byte escape (ESC c, ESC 7, ESC =): dropped
        else:
            self._again(ch, out)

    def _inter(self, ch, o, out):
        if 0x30 <= o <= 0x7E:
            self.state = "text"  # ESC ( 0 and the like: dropped
        elif not 0x20 <= o <= 0x2F:
            self._again(ch, out)

    def _csi(self, ch, o, out):
        if 0x20 <= o <= 0x3F:
            self.buf += ch
            if len(self.buf) > CSI_MAX:
                self.state = "skip"
        elif 0x40 <= o <= 0x7E:
            if ch == "m" and self.colours and sgr_ok(self.buf):
                out.append(ESC + "[" + self.buf + "m")
            self.state = "text"
        else:  # a control character or ESC inside
            self._again(ch, out)

    def _skip(self, ch, o, out):
        if 0x40 <= o <= 0x7E:
            self.state = "text"
        elif not 0x20 <= o <= 0x3F:
            self._again(ch, out)

    def _string(self, ch, _o, _out):
        self.n += 1
        if ch == "\x07":
            self.state = "text"
        elif ch == ESC:
            self.state = "string_esc"
        elif self.n > STRING_MAX:
            self.state = "text"

    def _string_esc(self, ch, _o, out):
        if ch == "\\":
            self.state = "text"
        else:
            self.state = "esc"
            self._step(ch, out)


def clean(text, colours=True):
    """One whole piece of text made safe (a command line, a folder, a note): what Filter keeps."""
    return Filter(colours).feed(text)


def width(ch):
    """The columns a character takes in the window: 0 for a combining mark, 2 for a wide one."""
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def wrap(text, cols):
    """Plain text (no escapes) cut into pieces of at most `cols` columns."""
    pieces, cur, w = [], "", 0
    for ch in text:
        cw = width(ch)
        if w + cw > cols and cur:
            pieces.append(cur)
            cur, w = "", 0
        cur += ch
        w += cw
    pieces.append(cur)
    return pieces


def _c(rgb, text, bold=False, italic=False):
    r, g, b = rgb
    style = ("1;" if bold else "") + ("3;" if italic else "")
    return f"\x1b[{style}38;2;{r};{g};{b}m{text}\x1b[0m"


RESET = "\x1b[0m"
DIM = (115, 122, 137)
PROMPT = (139, 147, 167)
BRIGHT = (255, 255, 255)
GREEN = (74, 222, 128)
RED = (248, 113, 113)
AMBER = (251, 191, 36)
ERR = (252, 165, 165)
JOB = (167, 139, 250)
GUTTER = {"o": _c(DIM, "│ "), "e": _c(RED, "│ ")}
ERR_SGR = "\x1b[38;2;{};{};{}m".format(*ERR)


def _secs(s):
    if s is None:
        return ""
    return f"{s * 1000:.0f} ms" if s < 1 else (f"{s:.1f} s" if s < 60 else f"{int(s // 60)} min {int(s % 60)} s")


class _Stream:
    def __init__(self):
        self.filter = Filter()
        self.sgr = []     # its colours since its last reset, drawn again after each gutter
        self.cr = False   # a carriage return waits: a newline after it is one newline


class Transcript:
    """The commands window's file. Helper lines (the command, the exit, a note) start at the first column;
    output lines start after the gutter. self._open is the stream whose output line the cursor is on, or
    None when the cursor is at the start of a line."""

    def __init__(self, path):
        self.path = path
        self.agent = ""  # the agent's name, for the window's first line
        self._f = None
        self._streams = {}
        self._open = None
        self._col = 0

    def _file(self):
        if self._f is None:
            new = not os.path.exists(self.path)
            self._f = open(self.path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115 - kept open
            if new:
                who = self.agent or "the agent"
                self._f.write(_c(DIM, f"Read only. The commands {who} runs show here.", italic=True) + "\n")
                self._f.flush()
        return self._f

    def write(self, text):
        f = self._file()
        f.write(text)
        f.flush()
        if f.tell() > LOG_MAX:  # a fresh file: the window's tail -F follows the name
            f.close()
            self._f = None
            os.replace(self.path, self.path + ".1")
            self._file()  # the new file at once, so the window follows it
            self._open = None

    # ---- the helper's own lines, from the node's record of the command
    def _own(self, text):
        """A line of the helper's own: on a line of its own, whatever output left the cursor on."""
        head = RESET + "\n" if self._open is not None else ""
        self._open = None
        self.write(head + text)

    def begin(self, m):
        cmd = clean(str(m.get("cmd") or ""), colours=False).replace("\t", "  ").replace("\r", "")
        cwd = clean(str(m.get("cwd") or ""), colours=False).replace("\n", " ").replace("\r", "")
        rows = []
        for ln in cmd.split("\n") or [""]:
            rows += wrap(ln, WIDTH - 2)
        first = _c(PROMPT, "❯ ") + _c(BRIGHT, rows[0])
        tail = []
        if cwd:
            if sum(width(c) for c in rows[0]) + 2 + sum(width(c) for c in cwd) <= WIDTH - 2:
                first += _c(DIM, f"  {cwd}")
            else:
                tail = ["  " + _c(DIM, piece) for piece in wrap(cwd, WIDTH - 2)]
        lines = [first] + ["  " + _c(BRIGHT, r) for r in rows[1:]] + tail
        self._own("\n" + "\n".join(lines) + "\n")

    def end(self, m):
        dur = _secs(m.get("dur"))
        dropped = int(m.get("dropped") or 0)
        more = (_c(DIM, f"… {dropped // 1024 or 1} KB more output, not shown here (the agent got all of it)")
                + "\n") if dropped else ""
        if m.get("job"):
            job = clean(str(m["job"]), colours=False).replace("\n", " ").replace("\r", "")[:40]
            line = _c(JOB, "▶") + _c(DIM, f" runs in the background as job {job}")
        elif m.get("cancelled"):
            line = _c(AMBER, "■ stopped") + _c(DIM, f" after {dur}" if dur else "")
        elif m.get("timeout"):
            line = _c(AMBER, "⏱ timed out") + _c(DIM, f" after {dur}" if dur else "")
        elif m.get("exit") == 0:
            line = _c(GREEN, "✓") + _c(DIM, f" exit 0 · {dur}")
        else:
            code = m.get("exit")
            what = f"exit {int(code)}" if isinstance(code, int) else (clean(str(m.get("sig") or "ended"), False)[:20])
            line = _c(RED, "✗") + _c(DIM, f" {what} · {dur}")
        for key in [k for k in self._streams if k[0] == m.get("id")]:
            del self._streams[key]  # a partial sequence it left is dropped with it
        self._own(f"{more}\n{line}\n" if not more else f"{more}{line}\n")

    def note(self, m):
        text = clean(str(m.get("text") or ""), colours=False).replace("\r", "")
        rows = []
        for ln in text.split("\n"):
            rows += wrap(ln.replace("\t", " "), WIDTH)
        self._own("\n" + "\n".join(_c(DIM, r) for r in rows) + "\n")

    # ---- output
    def out(self, m):
        key = (m.get("id"), "e" if m.get("s") == "e" else "o")
        st = self._streams.get(key)
        if st is None:
            st = self._streams[key] = _Stream()
        safe = st.filter.feed(str(m.get("d") or ""))
        if safe:
            self.write(self._lay_out(key, st, safe))

    def _gutter(self, key, st):
        base = ERR_SGR if key[1] == "e" else ""
        return GUTTER[key[1]] + base + "".join(st.sgr)

    def _start(self, key, st, parts):
        """The cursor onto an output line of this stream: a new line with its gutter unless it is there."""
        if self._open == key:
            return
        if self._open is not None:
            parts.append(RESET + "\n")
        parts.append(self._gutter(key, st))
        self._open, self._col = key, 0

    def _lay_out(self, key, st, safe):
        parts = []
        i, n = 0, len(safe)
        while i < n:
            ch = safe[i]
            if ch == ESC:  # an allowed SGR (Filter let only those through)
                j = safe.index("m", i) + 1
                seq = safe[i:j]
                i = j
                if seq in ("\x1b[m", "\x1b[0m"):
                    st.sgr = []
                    seq += ERR_SGR if key[1] == "e" else ""
                else:
                    st.sgr = (st.sgr + [seq])[-8:]
                if self._open == key:
                    parts.append(seq)
                continue
            i += 1
            if st.cr:
                st.cr = False
                if ch != "\n" and self._open == key:  # back to the line's start: the gutter again
                    parts.append("\r" + RESET + "\x1b[K" + self._gutter(key, st))
                    self._col = 0
            if ch == "\r":
                st.cr = True
                continue
            if ch == "\n":
                self._start(key, st, parts)
                parts.append(RESET + "\n")
                self._open = None
                continue
            self._start(key, st, parts)
            text = " " * (8 - self._col % 8) if ch == "\t" else ch
            for c in text:
                cw = width(c)
                if cw == 0 and self._col == 0:
                    continue  # a combining mark would join the gutter
                if self._col + cw > WIDTH:
                    parts.append(RESET + "\n" + self._gutter(key, st))
                    self._col = 0
                parts.append(c)
                self._col += cw
        return "".join(parts)
