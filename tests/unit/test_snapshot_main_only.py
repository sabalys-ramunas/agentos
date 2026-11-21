"""QA 2026-11-10 #11 and #15: images keep a ref for hover, and main_only folds the page's frame."""

from __future__ import annotations

from openajar.core.models import SnapshotResult
from openajar.node.snapshot import build_outline, fold_landmarks
from openajar.node.tools import main_only_snap

HOVERS = """\
- main [ref=e1]:
  - heading "Hovers" [level=3] [ref=e2]
  - generic [ref=e3]:
    - img "User Avatar" [ref=e4]
  - link "Home" [ref=e5] [cursor=pointer]:
    - img [ref=e6]
"""

PAGE = """\
- banner [ref=e1]:
  - link "Logo" [ref=e2] [cursor=pointer]
  - navigation "Top" [ref=e3]:
    - link "Popular" [ref=e4] [cursor=pointer]
- main [ref=e5]:
  - heading "u/test" [level=1] [ref=e6]
  - button "Follow" [ref=e7] [cursor=pointer]
- complementary "Sidebar" [ref=e8]:
  - link "Rules" [ref=e9] [cursor=pointer]
- contentinfo [ref=e10]:
  - text: footer words
"""


def test_an_image_outside_a_control_keeps_its_ref_for_hover():
    text, refs, _ = build_outline(HOVERS)
    assert 'img "User Avatar" [ref=e4]' in text
    assert "img [ref=e6]" not in text  # an icon inside a link: the link's ref is the one to use
    assert 'link "Home" [ref=e5]' in text and refs == 2


def test_fold_landmarks_leaves_one_line_each_with_a_count():
    outline, _, _ = build_outline(PAGE)
    folded, lines, refs = fold_landmarks(outline)
    assert "- banner (folded: 3 lines, 2 refs)" in folded
    assert '- complementary "Sidebar" (folded: 1 line, 1 ref)' in folded
    assert "- contentinfo (folded: 1 line)" in folded
    assert 'button "Follow" [ref=e7]' in folded and "Popular" not in folded and "footer words" not in folded
    assert (lines, refs) == (5, 3)


def test_main_only_snap_says_what_it_left_out_and_lowers_the_ref_count():
    outline, count, _ = build_outline(PAGE)
    snap = SnapshotResult(url="https://example.com/u/test", title="t", text=outline, ref_count=count, truncated=False)
    view = main_only_snap(snap)
    assert view.ref_count == count - 3 == 1
    assert "main content only" in view.text and "snapshot without main_only" in view.text
    assert "—" not in view.text
    plain = SnapshotResult(url="u", title="t", text='- main:\n  - button "Go" [ref=e1]', ref_count=1, truncated=False)
    assert main_only_snap(plain) is plain
