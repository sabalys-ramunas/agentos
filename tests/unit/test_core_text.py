"""agentos.core.text.first_line, pinned against the private copies it replaced."""

from __future__ import annotations

import pytest

from agentos.core.text import first_line


def copy_200(e):  # webbotauth/route.py, node/egress/helpers.py
    return (str(e).strip() or type(e).__name__).splitlines()[0][:200]


def copy_300(e):  # node/interaction.py, node/flow_driver.py, node/typist.py
    text = str(e).strip() or type(e).__name__
    return text.splitlines()[0][:300]


def copy_lines_first(e):  # stealth/stealthcheck.py
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:200]


ERRORS = [ValueError(""), ValueError("   "), RuntimeError("one"), OSError("first\nsecond"),
          TimeoutError("  padded \n next  "), KeyError("k"), Exception("x" * 500), Exception("\n\nlate")]


@pytest.mark.parametrize("e", ERRORS, ids=lambda e: type(e).__name__ + repr(str(e))[:12])
def test_first_line_matches_every_copy(e):
    assert first_line(e) == copy_200(e) == copy_lines_first(e)
    assert first_line(e, 300) == copy_300(e)
