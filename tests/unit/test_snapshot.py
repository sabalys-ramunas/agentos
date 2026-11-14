from agentos.node.snapshot import build_outline, find_refs, parse_line, render_line

FEED = """\
- generic [active] [ref=e1] [box=244,0,792,908]:
  - banner [ref=e2] [box=260,37,760,89]:
    - heading "Feed" [level=1] [ref=e3] [box=260,37,760,45]
    - navigation "Pages" [ref=e4] [box=260,104,760,22]:
      - link "Home" [ref=e5] [cursor=pointer] [box=260,105,43,19]:
        - /url: /index.html
  - main [ref=e7] [box=260,142,760,734]:
    - article "Post by Ada Lovelace" [ref=e8] [box=260,142,760,537]:
      - paragraph [ref=e10] [box=277,229,726,22]: The engine weaves algebraic patterns.
      - generic [ref=e11] [box=277,267,726,43]:
        - button "Like 12" [pressed] [ref=e12] [cursor=pointer] [box=277,271,74,35]
        - button "Comments" [ref=e13] [cursor=pointer] [box=359,271,99,35]
      - region "Comments" [ref=e14] [box=277,318,726,340]:
        - article "Comment by Grace Hopper" [ref=e15] [box=285,318,718,227]:
          - button "Like 12" [ref=e17] [cursor=pointer] [box=300,380,74,35]
          - article "Reply by Alan Turing" [ref=e18] [box=308,427,683,105]:
            - button "Like 4" [ref=e20] [cursor=pointer] [box=323,490,66,35]
      - generic [ref=e30] [cursor=pointer] [box=277,660,40,20]: Share
    - article "Post by Charles Babbage" [ref=e24] [box=260,695,760,181]:
      - button "Like 5" [ref=e28] [cursor=pointer] [box=277,824,66,35]
"""


def test_parse_line_reads_role_name_and_attributes():
    line = parse_line('    - button "Like 12" [pressed] [ref=e12] [cursor=pointer] [box=277,271,74,35]')
    assert line.depth == 2
    assert line.role == "button" and line.name == "Like 12"
    assert line.attrs == ("pressed", "ref=e12", "cursor=pointer", "box=277,271,74,35")
    assert line.ref == "e12" and line.box == (277, 271, 74, 35)
    assert line.actionable


def test_parse_line_text_props_and_plain_values():
    assert parse_line("  - text: hello").role is None
    assert parse_line("    - /url: /index.html").role is None
    para = parse_line("- paragraph [ref=e3]: Some [ref=e9] text")
    assert para.role == "paragraph" and para.ref == "e3" and not para.actionable


def test_parse_line_handles_quoted_keys_and_tricky_names():
    line = parse_line("""- 'button "it''s [ref=e99]: ok" [ref=e4]': tail""")
    assert line.role == "button"
    assert line.name == "it's [ref=e99]: ok"
    assert line.ref == "e4"
    escaped = parse_line(r'- link "say \"hi\"" [ref=f2e7]')
    assert escaped.name == 'say "hi"' and escaped.ref == "f2e7"


def test_render_line_keeps_refs_only_on_actionable_nodes():
    assert render_line(parse_line('- button "Like 12" [pressed] [ref=e12] [cursor=pointer] [box=1,2,3,4]')) == \
        '- button "Like 12" [pressed] [ref=e12] [cursor=pointer]'
    assert render_line(parse_line('  - heading "Feed" [level=1] [ref=e3] [box=1,2,3,4]')) == \
        '  - heading "Feed" [level=1]'
    assert render_line(parse_line("- paragraph [ref=e10] [box=1,2,3,4]: Some [ref=e9] text")) == \
        "- paragraph: Some [ref=e9] text"
    assert render_line(parse_line("- generic [ref=e30] [cursor=pointer] [box=1,2,3,4]: Share")) == \
        "- generic [ref=e30] [cursor=pointer]: Share"
    assert render_line(parse_line("""- 'button "a: b" [ref=e4] [box=1,2,3,4]'""")) == """- 'button "a: b" [ref=e4]'"""


def test_build_outline_small_page_is_complete():
    text, refs, truncated = build_outline(FEED, viewport_height=900)
    assert not truncated
    assert refs == 7  # Home, Like 12, Comments, Like 12, Like 4, Share, Like 5
    assert "[box=" not in text
    assert "- main:" in text and "[ref=e7]" not in text
    assert '- link "Home" [ref=e5] [cursor=pointer]:' in text
    assert "- /url: /index.html" in text


def test_find_refs_scopes_to_the_post_not_its_comments():
    text, _, _ = build_outline(FEED)
    assert find_refs(text, role="button", name="Like 12") == ["e12", "e17"]
    assert find_refs(text, role="button", name="Like 12", within="Post by Ada Lovelace") == ["e12"]
    assert find_refs(text, role="button", name="Like 12", within="Comment by Grace Hopper") == ["e17"]
    assert find_refs(text, role="button", within="Comment by Grace Hopper") == ["e17"]
    assert find_refs(text, role="button", within="Post by Charles Babbage") == ["e28"]
    assert find_refs(text, role="link") == ["e5"]


def _tall_page(items: int, item_height: int = 100) -> str:
    lines = ["- generic [ref=e1] [box=0,-5000,800,20000]:",
             '  - banner [ref=e2] [box=0,0,800,60]:',
             '    - link "Home" [ref=e3] [cursor=pointer] [box=0,0,60,20]']
    for i in range(items):
        y = -5000 + i * item_height
        lines.append(f'  - article "Item {i}" [ref=e{100 + 2 * i}] [box=0,{y},800,{item_height}]:')
        lines.append(f'    - button "Open item {i}" [ref=e{101 + 2 * i}] [cursor=pointer] [box=0,{y + 10},80,30]')
    return "\n".join(lines)


def test_build_outline_truncates_around_the_viewport():
    raw = _tall_page(200)  # items 50..58 sit in a 900 px viewport scrolled 5000 px down
    text, refs, truncated = build_outline(raw, viewport_height=900, max_chars=3000)
    assert truncated
    assert len(text) <= 3000 + 200
    assert 'button "Open item 50"' in text and 'button "Open item 58"' in text
    assert 'button "Open item 0"' not in text and 'button "Open item 199"' not in text
    assert 'link "Home" [ref=e3]' in text  # a fixed header in view stays
    assert "lines not shown" in text
    assert refs == text.count("[ref=")
    first = text.splitlines()[0]
    assert first.startswith("- generic")  # ancestors of kept lines stay for structure


def test_build_outline_without_viewport_cuts_from_the_top():
    raw = _tall_page(200)
    text, refs, truncated = build_outline(raw, viewport_height=None, max_chars=2000)
    assert truncated
    assert 'button "Open item 0"' in text and 'button "Open item 199"' not in text
    assert text.splitlines()[-1].strip().startswith("- ... ")
    assert refs == text.count("[ref=")


def test_build_outline_iframe_children_follow_the_iframe_box():
    raw = "\n".join([
        "- generic [ref=e1] [box=0,0,800,3000]:",
        "  - paragraph [ref=e2] [box=0,0,800,50]: top",
        "  - iframe [ref=e3] [box=0,2500,300,80]:",
        '    - button "Verify" [ref=f1e2] [cursor=pointer] [box=0,10,80,30]',
        *(f'  - link "Footer {i}" [ref=e{10 + i}] [box=0,2600,80,20]' for i in range(20)),
    ])
    text, _, truncated = build_outline(raw, viewport_height=900, max_chars=300)
    assert truncated
    assert "- paragraph: top" in text
    assert "Verify" not in text  # its own box says y=10, but the frame sits far below the view
