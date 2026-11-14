"""The diff without a browser: synthetic manifests and images drawn with Pillow, kept in a real
studio store. Covers what the fixture pages do not plant: skipped screens, keys that move to
another element, stretched siblings, inherited restyles, a move nothing explains, masks."""

from __future__ import annotations

import io

import pytest

from agentos.core.errors import NotFound
from agentos.studio.contracts import Capture, ComponentRef, Element, Rect, Screen, Shot, Target
from agentos.studio.diff import NoiseMask, ScreenMask, diff_captures, noise_mask
from agentos.studio.diff.structure import compare, match
from agentos.studio.diff.words import hex_color, label, quote
from agentos.studio.store import Manifest, StudioStore, iso_now, new_capture_id

pil = pytest.importorskip("PIL.Image")
from PIL import ImageDraw

W, H = 400, 600
FILL: dict[int, object] = {}


def el(key, x, y, w, h, *, parent=None, tag="div", role=None, name=None, text=None, styles=None, fill=None,
       component=None):
    e = Element(key=key, tag=tag, role=role, name=name, text=text, rect=Rect(x=x, y=y, w=w, h=h),
                styles=styles or {}, parent=parent, component=ComponentRef(name=component) if component else None)
    FILL[id(e)] = fill   # test only: what to paint
    return e


def png(elements, *, extra=(), size=(W, H)) -> bytes:
    im = pil.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(im)
    for e in elements:
        fill = FILL.get(id(e))
        if fill:
            r = e.rect
            d.rectangle((r.x, r.y, r.x + r.w - 1, r.y + r.h - 1), fill=fill)
    for (x, y, w, h), fill in extra:
        d.rectangle((x, y, x + w - 1, y + h - 1), fill=fill)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def keep(store: StudioStore, shots: dict[str, tuple[list[Element], bytes] | None], *, height=900) -> Capture:
    out = []
    for key, data in shots.items():
        width = int(key.split("@")[1].split("-")[0])
        screen = Screen(width=width, height=height)
        if data is None:
            out.append(Shot(screen=screen, ok=False, error="blank: the page came back blank"))
            continue
        elements, image = data
        m = Manifest(elements=elements, total=len(elements), cap=2000, page_width=W, page_height=H)
        out.append(Shot(screen=screen, image=store.put_blob(image, "image/png"),
                        manifest=store.put_blob(m.model_dump_json().encode(), "application/json")))
    return store.save(Capture(id=new_capture_id(), target=Target(url="https://app.example/"), shots=out,
                              created_at=iso_now()))


@pytest.fixture
def store(tmp_path):
    s = StudioStore(tmp_path)
    yield s
    s.close()


S = "default@400-light"


def page(heading_h=40, *, title="Welcome", button_bg="#2563eb", extra_el=None):
    """A main with a heading, two cards in a row and a footer, laid out by hand."""
    y = 16 + heading_h + 16
    els = [el("main", 0, 0, W, y + 120 + 16, role="main"),
           el(f'main > heading "{title}"', 16, 16, 300, heading_h, parent="main", tag="h1", role="heading",
              name=title, text=title, styles={"line-height": "40px", "font-size": "32px"}, fill=(30, 30, 30)),
           el("testid=a", 16, y, 180, 120, parent="main", fill=(220, 220, 220)),
           el('testid=a > button "Save"', 24, y + 80, 80, 32, parent="testid=a", tag="button", role="button",
              name="Save", text="Save", styles={"background-color": button_bg}, fill=button_bg),
           el("testid=b", 204, y, 180, 120, parent="main", fill=(200, 200, 200)),
           el("contentinfo", 0, y + 136, W, 40, role="contentinfo", fill=(240, 240, 240))]
    if extra_el is not None:
        els.append(extra_el)
    return els


def test_a_heading_that_grew_is_the_cause_and_what_it_pushed_points_at_it(store):
    a, b = page(), page(80, title="Welcome to a much longer heading")
    sd = diff_captures(store, keep(store, {S: (a, png(a))}).id, keep(store, {S: (b, png(b))}).id).screens[0]
    cause = sd.changes[0]
    assert cause.kind == "text" and cause.caused_by is None
    assert cause.before["key"] == 'main > heading "Welcome"'   # paired although its key changed
    assert cause.moved_with == 4   # two cards, the button inside one, the footer
    assert {c.key: c.caused_by for c in sd.changes[1:]} == {
        "main": cause.key, "testid=a": cause.key, "testid=b": cause.key, "contentinfo": cause.key}
    assert sd.lines == [(f"Heading 'Welcome' text changed to 'Welcome to a much longer heading' and wrapped "
                        f"from 1 to 2 lines (+40 px), which moved {cause.moved_with} elements down by 40 px.")]
    assert sd.unexplained == []


def test_a_restyle_names_tokens_only_when_a_token_set_is_given(store):
    from agentos.studio.tokens import from_css
    tokens = from_css(":root { --color-blue-600: #2563eb; --color-blue-700: #1d4ed8; }")
    a, b = page(), page(button_bg="#1d4ed8")
    ca, cb = keep(store, {S: (a, png(a))}), keep(store, {S: (b, png(b))})
    sd = diff_captures(store, ca.id, cb.id, tokens=tokens).screens[0]
    assert [c.kind for c in sd.changes] == ["restyled"]
    assert sd.changes[0].tokens == {"background-color": ["color.blue.600", "color.blue.700"]}
    assert sd.lines == ["Button 'Save' background-color color.blue.600 -> color.blue.700 (#2563eb -> #1d4ed8)."]
    off = from_css(":root { --color-blue-600: #2563eb; }")
    line = diff_captures(store, ca.id, cb.id, tokens=off).screens[0].lines[0]
    assert "color.blue.600 -> #1d4ed8 (#2563eb -> #1d4ed8, the new value is on no token)" in line


def test_screens_missing_failed_or_of_another_size_are_skipped_with_the_reason(store):
    a = page()
    before = keep(store, {S: (a, png(a)), "default@360-light": (a, png(a)), "default@768-light": None})
    after = keep(store, {S: (a, png(a)), "default@768-light": (a, png(a)), "default@1280-light": (a, png(a))})
    by = {s.screen: s for s in diff_captures(store, before.id, after.id).screens}
    assert by[S].status == "same"
    assert by["default@360-light"].status == "skipped" and "after capture has no screen" in by["default@360-light"].reason
    assert by["default@1280-light"].status == "skipped" and "before capture" in by["default@1280-light"].reason
    assert by["default@768-light"].reason == "the before screen failed to capture: blank: the page came back blank"
    tall = keep(store, {S: (a, png(a))}, height=700)
    sd = diff_captures(store, before.id, tall.id, screens=[S]).screens[0]
    assert sd.status == "skipped" and "differ in size" in sd.reason
    wide = keep(store, {S: (a, png(a, size=(W + 20, H)))})
    sd = diff_captures(store, before.id, wide.id, screens=[S]).screens[0]
    assert sd.status == "skipped" and "differ in width" in sd.reason
    with pytest.raises(NotFound):
        diff_captures(store, before.id, after.id, screens=["default@999-light"])
    with pytest.raises(NotFound):
        diff_captures(store, before.id, "cap_00000000000nothere")


def test_a_positional_key_that_moved_to_another_element_is_matched_by_content():
    def cards(names):
        out = [el("list", 0, 0, 300, 40 * len(names), role="list")]
        for i, n in enumerate(names):
            out.append(el(f"list > div[{i + 1}]", 0, 40 * i, 300, 40, parent="list"))
            out.append(el(f'list > div[{i + 1}]/p[1] ~"{n}"', 0, 40 * i, 300, 40, parent=f"list > div[{i + 1}]",
                          tag="p", text=n))
        return out
    before, after = cards(["Free", "Pro", "Team"]), cards(["Pro", "Team"])
    pairs = match(before, after)
    assert pairs["list > div[2]"] == "list > div[1]" and pairs["list > div[3]"] == "list > div[2]"
    st = compare(before, after)
    roots = [i for i in st.items if i.cause is None]
    assert [(r.kind, r.key) for r in roots] == [("removed", "list > div[1]")]
    assert roots[0].inside == 1 and len([e for e in roots[0].effects if e.moved]) == 4


def test_a_sibling_stretched_beside_a_card_that_grew_is_an_effect():
    row = el("row", 0, 0, 400, 100)
    a1 = el("testid=a", 0, 0, 190, 100, parent="row", styles={"padding": "16px"})
    b1 = el("testid=b", 210, 0, 190, 100, parent="row")
    a2 = el("testid=a", 0, 0, 190, 116, parent="row", styles={"padding": "24px"})
    b2 = el("testid=b", 210, 0, 190, 116, parent="row")
    row2 = el("row", 0, 0, 400, 116)
    st = compare([row, a1, b1], [row2, a2, b2])
    by = {i.key: i for i in st.items}
    assert by["testid=a"].cause is None and by["testid=a"].kind == "restyled"
    assert by["testid=b"].cause is by["testid=a"] and by["row"].cause is by["testid=a"]


def test_an_inherited_colour_change_is_one_cause_and_same_restyles_are_one_sentence(store):
    def tree(color):
        return [el("main", 0, 0, W, 200, role="main", text="Intro", styles={"color": color}),
                el('main > p[1] ~"One"', 0, 40, 200, 20, parent="main", tag="p", text="One", styles={"color": color}),
                el('main > p[2] ~"Two"', 0, 60, 200, 20, parent="main", tag="p", text="Two", styles={"color": color})]
    st = compare(tree("rgb(0, 0, 0)"), tree("rgb(17, 17, 17)"))
    roots = [i for i in st.items if i.cause is None]
    assert [r.key for r in roots] == ["main"] and len(roots[0].effects) == 2
    # the same restyle on unrelated elements: listed apart, said once
    def flat(bg):
        return [el(f'button "B{i}"', 10, 10 + 40 * i, 80, 30, tag="button", role="button", name=f"B{i}",
                   styles={"background-color": bg}, fill=bg) for i in range(5)]
    x, y = flat("#ff0000"), flat("#00ff00")
    sd = diff_captures(store, keep(store, {S: (x, png(x))}).id, keep(store, {S: (y, png(y))}).id).screens[0]
    assert len(sd.changes) == 5 and all(c.caused_by is None for c in sd.changes)
    assert sd.lines == [("5 elements changed background-color #ff0000 -> #00ff00: Button 'B0', Button 'B1', "
                        "Button 'B2' and 2 more.")]


def test_a_move_nothing_explains_is_said_as_such(store):
    a = [el("testid=badge", 10, 10, 50, 20, fill=(200, 0, 0))]
    b = [el("testid=badge", 10, 22, 50, 20, fill=(200, 0, 0))]
    sd = diff_captures(store, keep(store, {S: (a, png(a))}).id, keep(store, {S: (b, png(b))}).id).screens[0]
    assert [(c.kind, c.caused_by) for c in sd.changes] == [("moved", None)]
    assert sd.lines == ["Box (testid=badge) moved down by 12 px, and nothing measured on the page explains it."]
    assert sd.unexplained == []


def test_pixels_no_element_explains_are_a_region_and_never_given_a_reason(store):
    a = page()
    ca = keep(store, {S: (a, png(a))})
    cb = keep(store, {S: (a, png(a, extra=[((300, 400, 40, 40), (0, 0, 0))]))})
    sd = diff_captures(store, ca.id, cb.id).screens[0]
    assert sd.changes == [] and len(sd.unexplained) == 1
    r = sd.unexplained[0].rect
    assert r.x <= 300 and r.y <= 400 and r.x + r.w >= 340 and r.y + r.h >= 440 and r.w <= 56
    assert sd.lines[0].startswith("Pixels changed in a ") and "outside every element" in sd.lines[0]
    assert sd.unexplained[0].evidence and sd.pixel_share == pytest.approx(1600 / (W * H), abs=1e-5)
    # a mask over the area leaves nothing
    mask = NoiseMask(screens={S: ScreenMask(rects=[Rect(x=296, y=396, w=48, h=48)])})
    assert diff_captures(store, ca.id, cb.id, mask=mask).screens[0].status == "same"


def test_a_noise_mask_hides_a_ticking_element_and_a_random_key(store):
    def noisy(t, rid):
        return page(extra_el=None) + [
            el('contentinfo > p[1] ~"Updated"', 10, 220, 120, 20, parent="contentinfo", tag="p", text=f"Updated {t}"),
            el(f'contentinfo > p[2]/code[1] ~"{rid}"', 10, 240, 120, 20, parent="contentinfo", tag="code", text=rid,
               fill=(0, 0, 0) if rid.endswith("x") else (60, 60, 60))]
    caps = [keep(store, {S: (e, png(e))}) for e in (noisy(1, "req-ax"), noisy(2, "req-bq"), noisy(3, "req-cx"))]
    assert diff_captures(store, caps[0].id, caps[2].id).screens[0].status == "changed"
    mask = noise_mask(store, caps[0].id, caps[1].id)
    assert 'contentinfo > p[1] ~"Updated"' in mask.screens[S].keys
    assert diff_captures(store, caps[0].id, caps[2].id, mask=mask).screens[0].status == "same"
    assert NoiseMask.model_validate_json(mask.model_dump_json()) == mask   # a caller can keep it


def test_words():
    assert hex_color("rgb(37, 99, 235)") == "#2563eb"
    assert hex_color("rgba(0, 0, 0, 0.5)") == "#00000080"
    assert hex_color("oklch(0.5 0.1 200)") == "oklch(0.5 0.1 200)"
    assert quote("a 'quoted' " + "x" * 60) == "'a ’quoted’ " + "x" * 28 + "…'"
    assert label(el("k", 1, 2, 3, 4, tag="button", role="button", name="Save", component="Button/primary")) == \
        "Button 'Save' (Button/primary)"
    assert label(el("k", 16, 300, 3, 4)) == "Box at 16,300"


def test_matching_works_out_each_elements_content_once(monkeypatch):
    """Many free siblings of one kind: every one is compared with every candidate, round after round,
    and each element's content is still worked out only once."""
    from agentos.studio.diff import structure

    calls = []
    real = structure._content
    monkeypatch.setattr(structure, "_content", lambda e, kids: calls.append(e.key) or real(e, kids))
    def word(n: int) -> str:     # digits are not content to the matcher, so the rows differ in letters
        return "row " + chr(97 + n // 26) + chr(97 + n % 26)

    root_b, root_a = el("main", 0, 0, 800, 4000), el("main", 0, 0, 800, 4000)
    before = [root_b] + [el(f"main > li[{i}]", 0, i * 20, 800, 20, parent="main", tag="li", text=word(i))
                         for i in range(150)]
    after = [root_a] + [el(f"main > li[{i}]", 0, i * 20, 800, 20, parent="main", tag="li", text=word(i + 1))
                        for i in range(150)]
    pairs = match(before, after)
    assert pairs["main"] == "main"
    assert pairs["main > li[1]"] == "main > li[0]"      # the rows moved up one, by content
    assert pairs["main > li[0]"] == "main > li[149]"    # the one left on each side, by order
    assert len(calls) <= len(before) + len(after)
