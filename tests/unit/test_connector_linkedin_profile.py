"""LinkedIn profile edits (brief B2), end to end on fake pages built from the structure the
operations assume (connectors/linkedin/fixtures/profile_*, SYNTHESIZED, NOT CAPTURED): an edit to
the value the page already shows stops in the preflight with nothing saved, an edit is proved only
by the public value on a fresh load of the profile page, and a photo or cover only by a new src on
LinkedIn's media host. Nothing here was checked against LinkedIn: the canary replaces the guesses."""

from __future__ import annotations

import pytest
from test_connector_social_ops import LI_AS, LI_LANE, LINKEDIN, ME_URL, _harness, _me_redirects

from openajar.core.models import RunStatus
from openajar.runner.connectors.compile import compile_path
from openajar.runner.fakes import El, FakeSite

OLD_PHOTO = "https://media.licdn.com/dms/image/v2/D4D03AQOLD0000/profile-displayphoto-shrink_200_200/0/1"
OLD_COVER = "https://media.licdn.com/dms/image/v2/D4D16AQOLD0000/profile-displaybackgroundimage-shrink_350_1400/0/1"
IMAGE = "in_3f9a2c71"


def profile_site(*, applies: bool = True, media_host: str = "https://media.licdn.com/dms/image/v2/",
                 about: str | None = "Notes on the analytical engine.", photo: bool = True,
                 headings: int = 1, resized: bool = False) -> FakeSite:
    """The member's own profile: a top card with the photo (a button around its img), the name, the
    headline and Edit intro; a cover img with its camera button; an About section with its pencil.
    Each pencil opens a dialog (outside main) that Save closes. `applies` False: Save closes the
    dialog but the public profile keeps its old values (the edit did not take). `photo` False: the
    top card shows no photo img yet. `headings` 2: the top card has a second heading. `resized`:
    a saved image comes back as the old asset at another size and signature (nothing changed)."""
    site = FakeSite()
    site.identity = None
    site.state = {"first": "Ada", "last": "Lovelace", "headline": "Analyst of engines", "about": about,
                  "photo": OLD_PHOTO, "cover": OLD_COVER, "dialog": None, "saves": 0, "n": 0}

    def opener(name):
        def click(s, el, d):
            s.state["dialog"] = name
        return click

    def save(s, el, d):
        s.state["saves"] += 1
        kind = s.state["dialog"]
        s.state["dialog"] = None
        if not applies:
            return
        if kind in ("photo", "cover"):
            if d.values.get("upload") and resized:
                s.state[kind] = s.state[kind].replace("shrink_", "shrink_800_") + "?e=2&v=beta&t=new"
            elif d.values.get("upload"):
                s.state["n"] += 1
                s.state[kind] = f"{media_host}D4D03AQNEW{s.state['n']:04d}/{kind}/0/1"
            return
        for key in ("first", "last", "headline", "about"):
            if key in d.values:
                s.state[key] = d.values[key]

    def page(s, d):
        st = s.state
        pencil = El("button", "Edit profile photo", on_click=opener("photo"))
        if photo or st["n"]:
            pencil.add(El("img", "Ada Lovelace", attrs={"class": "pv-top-card-profile-picture__image--show evi-image",
                                                        "src": st["photo"]}))
        top = El("generic", "top card", css=(".pv-top-card",)).add(
            pencil,
            El("img", "", attrs={"id": "profile-background-image-target-image", "src": st["cover"]}),
            El("button", "Edit background photo", on_click=opener("cover")),
            El("heading", f"{st['first']} {st['last']}", text=f"{st['first']} {st['last']}"),
            *[El("heading", "Contact info", text="Contact info") for _ in range(headings - 1)],
            El("generic", "", text=st["headline"], css=(".text-body-medium",)),
            El("button", "Edit intro", on_click=opener("intro")))
        main = El("main").add(top)
        if st["about"] is not None:
            main.add(El("generic", "About", css=("section:has(#about)",)).add(
                El("heading", "About", text="About"), El("button", "Edit about", on_click=opener("about")),
                El("generic", "", text=st["about"], css=('.inline-show-more-text span[aria-hidden="true"]',))))
        doc = El("document", "profile").add(main)
        kind = st["dialog"]
        if kind == "intro":
            doc.add(El("dialog", "Edit intro").add(
                El("textbox", "First name*", key="first", attrs={"value": st["first"]}),
                El("textbox", "Last name*", key="last", attrs={"value": st["last"]}),
                El("textbox", "Headline*", key="headline", attrs={"value": st["headline"]}),
                El("button", "Save", on_click=save)))
        elif kind == "about":
            doc.add(El("dialog", "Edit about").add(
                El("textbox", "Summary", key="about", attrs={"value": st["about"] or ""}),
                El("button", "Save", on_click=save)))
        elif kind in ("photo", "cover"):
            box = El("dialog", "Profile photo" if kind == "photo" else "Background photo")
            if d.values.get("upload"):
                box.add(El("img", "Crop your photo", attrs={"src": "blob:https://www.linkedin.com/0000"}))
            box.add(El("button", "Upload photo" if kind == "photo" else "Change photo", key="upload"),
                    El("button", "Save photo" if kind == "photo" else "Apply", on_click=save))
            doc.add(box)
        return doc

    site.route(ME_URL, page)
    return site


async def _profile_harness(tmp_path, **kw):
    site = profile_site(**kw)
    h = _harness(tmp_path, site, LINKEDIN, LI_LANE, LI_AS)
    _me_redirects(h.driver)

    def fresh(op, target, d):           # a navigation is a fresh load: no dialog is open on it
        if op == "navigate":
            site.state["dialog"] = None
    h.driver.hooks.append(fresh)
    h.driver.after_hooks.append(lambda op, target, d: d._render() if op == "upload" else None)
    h.runner.surfaces.file_digest = lambda lane, ref: "sha256:" + "ab" * 32
    return h


TEXT_OPS = {
    "profile_name": ({"first_name": "Augusta", "last_name": "King"}, {"first_name": "Ada", "last_name": "Lovelace"},
                     lambda st: (st["first"], st["last"]), ("Augusta", "King")),
    "profile_headline": ({"headline": "Mathematician, first programmer"}, {"headline": "Analyst of engines"},
                         lambda st: st["headline"], "Mathematician, first programmer"),
    "profile_about": ({"about": "Notes on Babbage's engine,\nand what it could compute."},
                      {"about": "Notes on the analytical engine."}, lambda st: st["about"],
                      "Notes on Babbage's engine,\nand what it could compute."),
}


def test_the_profile_operations_compile_unverified_with_synthesized_fixtures():
    cc = compile_path(LINKEDIN, commit_gate=True)
    assert cc.connector.version == "0.3.0"
    for name in ("profile_name", "profile_headline", "profile_about", "profile_photo", "profile_cover"):
        op = cc.connector.operations[name]
        assert op.effect == "commit" and op.verified == "" and op.description.startswith("Not verified yet")
        assert cc.operations[name].installable and cc.operations[name].mark == ""
        assert (LINKEDIN / "fixtures" / name / "en.html").read_text().startswith("<!-- SYNTHESIZED, NOT CAPTURED.")
        assert op.commit.request.op.startswith("^com\\.linkedin\\.sdui\\.requests\\.profile\\.")
        assert op.pace.per_day == 2 and op.pace.per_week == 5
    props = {n: cc.connector.operations[n].input["properties"] for n in
             ("profile_name", "profile_headline", "profile_about")}
    assert props["profile_name"]["first_name"]["maxLength"] == 50 and props["profile_name"]["last_name"]["minLength"] == 1
    assert props["profile_headline"]["headline"]["maxLength"] == 220
    assert props["profile_about"]["about"]["maxLength"] == 2600


@pytest.mark.parametrize("name", list(TEXT_OPS))
async def test_a_text_edit_is_proved_by_the_public_value_on_a_fresh_load(tmp_path, name):
    params, _, read, want = TEXT_OPS[name]
    h = await _profile_harness(tmp_path)
    run = await h.run(f"connector.linkedin.{name}", params, lane=LI_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert read(h.site.state) == want and h.site.state["saves"] == 1
    assert run.output["data"] == {"proof_url": ME_URL}
    assert h.driver.log.count(("navigate", ME_URL)) >= 1          # the public page, loaded again


@pytest.mark.parametrize("name", list(TEXT_OPS))
async def test_a_text_edit_to_the_value_already_shown_ends_already_current(tmp_path, name):
    _, same, read, _ = TEXT_OPS[name]
    h = await _profile_harness(tmp_path)
    before = read(h.site.state)
    run = await h.run(f"connector.linkedin.{name}", same, lane=LI_LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "already_current"
    assert "already shows the value" in run.error["message"]
    assert h.site.state["saves"] == 0 and read(h.site.state) == before


async def test_a_text_edit_whose_element_is_not_alone_is_a_mismatch_not_current(tmp_path):
    """Two headings in the top card: the name cannot be read, so it is not taken as current."""
    _, same, _, _ = TEXT_OPS["profile_name"]
    h = await _profile_harness(tmp_path, headings=2)
    run = await h.run("connector.linkedin.profile_name", same, lane=LI_LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "preflight_mismatch"
    assert "2 matches" in run.error["message"] and h.site.state["saves"] == 0


async def test_text_differs_refuses_several_elements_even_with_nth(tmp_path):
    """A connector that sets nth on the text_differs locator still needs exactly one element."""
    import json
    import shutil

    from test_connector_social_ops import _harness

    root = tmp_path / "linkedin"
    shutil.copytree(LINKEDIN, root)
    doc = json.loads((root / "connector.json").read_text())
    [check] = [c for c in doc["operations"]["profile_name"]["commit"]["preflight"] if c["kind"] == "text_differs"]
    check["locator"]["nth"] = 0
    (root / "connector.json").write_text(json.dumps(doc, indent=2))
    site = profile_site(headings=2)
    h = _harness(tmp_path / "h", site, root, LI_LANE, LI_AS)
    _me_redirects(h.driver)
    h.driver.hooks.append(lambda op, target, d: site.state.update(dialog=None) if op == "navigate" else None)
    _, same, _, _ = TEXT_OPS["profile_name"]
    run = await h.run("connector.linkedin.profile_name", same, lane=LI_LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "preflight_mismatch", run.error
    assert h.site.state["saves"] == 0


def test_the_profile_operations_set_no_nth_and_read_the_public_address_first():
    import json

    doc = json.loads((LINKEDIN / "connector.json").read_text())
    for f in ("name", "headline", "about", "photo", "cover"):
        op = doc["operations"][f"profile_{f}"]
        assert '"nth"' not in json.dumps(op), f
        steps = op["steps"]
        assert [s.get("url") for s in steps[:3]] == ["https://www.linkedin.com/in/me/", None,
                                                    "https://www.linkedin.com/in/${acts_as_id}/"]
        assert steps[1]["type"] == "assert_identity"
        assert all(p.get("readOnly") is True for p in op["input"]["properties"].values())


@pytest.mark.parametrize("name", list(TEXT_OPS))
async def test_a_text_edit_the_public_page_does_not_show_is_not_proved(tmp_path, name):
    params, _, _, _ = TEXT_OPS[name]
    h = await _profile_harness(tmp_path, applies=False)
    run = await h.run(f"connector.linkedin.{name}", params, lane=LI_LANE)
    assert run.status == RunStatus.PARTIAL and run.error["code"] == "readback_failed"
    assert "equals" in run.error["message"] and run.output is None


async def test_an_about_edit_on_a_profile_with_no_about_section_fails_before_typing(tmp_path):
    h = await _profile_harness(tmp_path, about=None)
    run = await h.run("connector.linkedin.profile_about", {"about": "Hello."}, lane=LI_LANE)
    assert run.status == RunStatus.FAILED and h.site.state["saves"] == 0


@pytest.mark.parametrize("name", ["profile_photo", "profile_cover"])
async def test_an_image_edit_is_proved_by_a_new_src_on_linkedins_media_host(tmp_path, name):
    h = await _profile_harness(tmp_path)
    run = await h.run(f"connector.linkedin.{name}", {"image": IMAGE}, lane=LI_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    field = name.removeprefix("profile_")
    assert h.site.state[field].startswith("https://media.licdn.com/dms/image/v2/D4D03AQNEW")
    assert h.site.state["photo" if field == "cover" else "cover"] == (OLD_PHOTO if field == "cover" else OLD_COVER)


@pytest.mark.parametrize("name", ["profile_photo", "profile_cover"])
async def test_an_image_edit_needs_a_changed_src(tmp_path, name):
    """Save closed the dialog, and the public page still shows the old image: not proved."""
    h = await _profile_harness(tmp_path, applies=False)
    run = await h.run(f"connector.linkedin.{name}", {"image": IMAGE}, lane=LI_LANE)
    assert run.status == RunStatus.PARTIAL and run.error["code"] == "readback_failed"
    assert "differs from the one saved as before" in run.error["message"]


@pytest.mark.parametrize("name", ["profile_photo", "profile_cover"])
async def test_an_image_edit_is_read_before_and_after_on_the_public_address(tmp_path, name):
    h = await _profile_harness(tmp_path)
    run = await h.run(f"connector.linkedin.{name}", {"image": IMAGE}, lane=LI_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    navs = [t for op, t in h.driver.log if op == "navigate"]
    assert navs[0].endswith("/in/me/") and navs[1:] == [ME_URL, ME_URL]      # before and after on /in/<id>/


@pytest.mark.parametrize("name", ["profile_photo", "profile_cover"])
async def test_the_same_asset_at_another_size_and_signature_is_not_a_new_image(tmp_path, name):
    h = await _profile_harness(tmp_path, resized=True)
    run = await h.run(f"connector.linkedin.{name}", {"image": IMAGE}, lane=LI_LANE)
    field = name.removeprefix("profile_")
    assert h.site.state[field] != (OLD_PHOTO if field == "photo" else OLD_COVER)      # the raw src did change
    assert run.status == RunStatus.PARTIAL and run.error["code"] == "readback_failed", run.error


async def test_a_photo_edit_with_no_photo_to_note_first_does_nothing(tmp_path):
    """The before read waits for exactly one photo with a src; none there: nothing is uploaded or saved."""
    h = await _profile_harness(tmp_path, photo=False)
    run = await h.run("connector.linkedin.profile_photo", {"image": IMAGE}, lane=LI_LANE)
    assert run.status == RunStatus.FAILED and run.error["code"] == "readback_failed", run.error
    assert run.error["acted"] is False and h.site.state["saves"] == 0
    assert not any(op == "upload" for op, _ in h.driver.log)


async def test_an_image_edit_served_from_another_host_is_not_proved(tmp_path):
    h = await _profile_harness(tmp_path, media_host="https://cdn.example.com/")
    run = await h.run("connector.linkedin.profile_photo", {"image": IMAGE}, lane=LI_LANE)
    assert run.status == RunStatus.PARTIAL and run.error["code"] == "readback_failed"
    assert "differs from the one saved as before" in run.error["message"]
    assert h.site.state["photo"].startswith("https://cdn.")


async def test_an_image_edit_stops_before_saving_when_no_image_is_in_the_dialog(tmp_path):
    h = await _profile_harness(tmp_path)
    h.driver.after_hooks.clear()                       # the upload never renders its preview
    h.driver.after_hooks.append(lambda op, target, d: d._render() if op in ("type", "fill") else None)
    _me_redirects(h.driver)
    run = await h.run("connector.linkedin.profile_photo", {"image": IMAGE}, lane=LI_LANE)
    assert run.status == RunStatus.FAILED and h.site.state["saves"] == 0 and h.site.state["photo"] == OLD_PHOTO


async def test_a_profile_run_declares_only_its_own_edit(tmp_path):
    h = await _profile_harness(tmp_path)
    seen: list = []

    async def scope_commit(flow_id, request):
        seen.append((flow_id, request))
    h.driver.scope_commit = scope_commit
    await h.run("connector.linkedin.profile_headline", {"headline": "New"}, lane=LI_LANE)
    assert seen[0][0] == "connector.linkedin.profile_headline"
    assert "Headline" in seen[0][1]["op"] and seen[0][1]["url"].endswith("server-request$")


def test_text_differs_takes_a_template_of_shown_params_only():
    import json

    from pydantic import ValidationError

    from openajar.runner.connectors.compile import compile_connector
    from openajar.runner.connectors.models import PreflightCheck
    from openajar.runner.models import PreflightCheck as RunnerCheck

    loc = {"role": "heading", "nth": 0, "within": {"role": "main"}}
    assert PreflightCheck.model_validate({"kind": "text_differs", "locator": loc, "text": "${headline}"}).text
    for bad in ({"kind": "text_differs", "locator": loc}, {"kind": "text_differs", "locator": loc, "text": "x",
                                                          "param": "headline"},
                {"kind": "count", "locator": loc, "equals": 0, "text": "x"}):
        with pytest.raises(ValidationError):
            PreflightCheck.model_validate(bad)
        with pytest.raises(ValidationError):
            RunnerCheck.model_validate({**bad, "locator": {"role": "heading"}})
    doc = json.loads((LINKEDIN / "connector.json").read_text())
    doc["operations"]["profile_headline"]["input"]["properties"]["headline"]["show"] = False
    with pytest.raises(Exception, match="not a shown input"):
        compile_connector(doc, commit_gate=True)


async def test_an_about_edit_compares_line_breaks(tmp_path):
    """The same words on other lines are another About: the edit runs, and is proved with its breaks."""
    h = await _profile_harness(tmp_path, about="Line one.\nLine two.")
    run = await h.run("connector.linkedin.profile_about", {"about": "Line one.\nLine two."}, lane=LI_LANE)
    assert run.error["code"] == "already_current" and h.site.state["saves"] == 0
    h = await _profile_harness(tmp_path / "again", about="Line one.\nLine two.")
    run = await h.run("connector.linkedin.profile_about", {"about": "Line one. Line two."}, lane=LI_LANE)
    assert run.status == RunStatus.SUCCEEDED_VERIFIED, run.error
    assert h.site.state["about"] == "Line one. Line two." and h.site.state["saves"] == 1
