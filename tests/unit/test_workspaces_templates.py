"""Templates (built-in and user TOML), id and lane-name limits, and bad-file reporting."""

from __future__ import annotations

import pytest

from agentos.core.errors import InvalidRequest
from agentos.core.models import parse_lane_id
from agentos.workspaces.models import check_lane_name, check_workspace_id
from agentos.workspaces.templates import TemplateInvalid, builtin_templates, load_templates, parse_template

GOOD = '''
title = "Shop"
description = "Store and support"

[[surfaces]]
name = "store"
platform = "shopify"
home_url = "https://shop.example.com/"
login_url = "https://shop.example.com/admin"

[[surfaces]]
name = "support"
platform = "helpdesk"
engine = "camoufox"
'''


def problems_of(text: str, template_id: str = "shop") -> list[str]:
    with pytest.raises(TemplateInvalid) as info:
        parse_template(template_id, text)
    return info.value.problems


def test_builtin_social_6_has_six_surfaces_and_personal_has_its_own():
    templates = builtin_templates()
    social = templates["social-6"]
    assert [s.name for s in social.surfaces] == ["instagram", "facebook", "reddit", "medium", "blog", "linkedin"]
    assert all(s.engine == "camoufox" for s in social.surfaces)
    assert social.builtin and templates["personal"].builtin
    # A blog's address belongs to the company, so the built-in leaves it unset rather than guessing.
    blog = next(s for s in social.surfaces if s.name == "blog")
    assert blog.home_url is None and blog.login_url is None
    assert all(s.login_url and s.login_url.startswith("https://") for s in social.surfaces if s.name != "blog")
    assert "personal" in templates and len(templates["personal"].surfaces) == 4


def test_valid_user_template_parses_with_defaults():
    template = parse_template("shop", GOOD, source="shop.toml")
    assert template.id == "shop" and template.title == "Shop" and template.source == "shop.toml"
    assert [s.name for s in template.surfaces] == ["store", "support"]
    assert template.surfaces[1].home_url is None and template.surfaces[1].engine == "camoufox"
    assert template.surfaces[0].login_url == "https://shop.example.com/admin"


def test_duplicate_surface_names_are_refused_with_both_positions():
    text = GOOD + '\n[[surfaces]]\nname = "store"\nplatform = "other"\n'
    found = problems_of(text)
    assert found == ['surfaces #3: surface name \'store\' is already used by surfaces #1']


def test_surface_errors_are_listed_together_with_their_position():
    text = '''
[[surfaces]]
name = "Store Front"
platform = ""
engine = "vnc"
home_url = "http://insecure.example.com/"
extra = 1
'''
    found = problems_of(text)
    assert any("surfaces #1: name must be" in p for p in found)
    assert any("surfaces #1: platform must be" in p for p in found)
    assert any("surfaces #1: engine must be one of" in p for p in found)
    assert any("surfaces #1: home_url must be an https:// address" in p for p in found)
    assert any("surfaces #1: unknown key 'extra'" in p for p in found)


@pytest.mark.parametrize("url", [
    "http://example.com/",                 # not https
    "javascript:alert(1)",                 # not an address we open
    "https://user:secret@example.com/",    # credentials in a URL
    "https://example.com/a b",             # space
    "https://" + "a" * 600 + ".com/",      # too long
])
def test_urls_must_be_plain_https(url):
    text = f'[[surfaces]]\nname = "store"\nplatform = "shop"\nlogin_url = "{url}"\n'
    found = problems_of(text)
    assert found and "login_url" in found[0]
    assert "secret" not in " ".join(found)  # never echo the address back


def test_a_template_needs_surfaces_and_a_known_shape():
    assert problems_of('title = "x"\n') == ["no [[surfaces]] tables: a template needs at least one surface"]
    assert problems_of('[surfaces]\nname = "a"\nplatform = "b"\n') == [
        "surfaces must be written as [[surfaces]] tables"]
    assert "unknown top-level key 'colour'" in problems_of('colour = "red"\n' + GOOD)


def test_invalid_toml_is_reported_not_raised_as_a_crash():
    found = problems_of("[[surfaces\nname = ")
    assert found[0].startswith("not valid TOML:")


def test_template_id_must_look_like_a_file_name():
    found = problems_of(GOOD, template_id="Bad Name")
    assert found[0].startswith("the file name must be")


def test_lane_name_limit_is_41_and_workspace_limit_is_31():
    assert check_workspace_id("a" * 31) == "a" * 31
    assert check_lane_name("b" * 41) == "b" * 41
    with pytest.raises(InvalidRequest, match="workspace id must be 1 to 31"):
        check_workspace_id("a" * 32)
    with pytest.raises(InvalidRequest, match="lane name must be 1 to 41"):
        check_lane_name("b" * 42)
    # Combining the longest valid parts still gives a valid lane id, so apply can never make an
    # id the node refuses for length alone.
    workspace, name = parse_lane_id(f"{'a' * 31}/{'b' * 41}")
    assert (len(workspace), len(name)) == (31, 41)
    # A surface name one character over the limit is refused at template time.
    text = f'[[surfaces]]\nname = "{"b" * 42}"\nplatform = "p"\n'
    assert problems_of(text)[0].startswith("surfaces #1: name must be 1 to 41")


def test_load_templates_reports_bad_files_and_keeps_the_good_ones(tmp_path):
    (tmp_path / "shop.toml").write_text(GOOD, encoding="utf-8")
    (tmp_path / "broken.toml").write_text("[[surfaces\n", encoding="utf-8")
    (tmp_path / "social-6.toml").write_text(GOOD, encoding="utf-8")
    (tmp_path / "latin1.toml").write_bytes('title = "caf\xe9"\n'.encode("latin-1"))
    (tmp_path / "Bad Name.toml").write_text(GOOD, encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")

    templates, problems = load_templates(tmp_path)

    assert {"social-6", "personal", "shop"} <= set(templates)
    assert templates["social-6"].builtin  # the built-in wins over a user file of the same name
    assert "broken" not in templates and "latin1" not in templates
    by_file = {(p.file, p.message) for p in problems}
    assert ("social-6.toml", "the name 'social-6' belongs to a built-in template; rename the file") in by_file
    assert any(p.file == "broken.toml" and p.message.startswith("not valid TOML") for p in problems)
    assert ("latin1.toml", "cannot be read as UTF-8 text") in by_file
    assert any(p.file == "Bad Name.toml" for p in problems)
    assert all(p.template and p.file for p in problems)


def test_load_templates_without_a_directory_is_just_the_builtins(tmp_path):
    templates, problems = load_templates(tmp_path / "missing")
    assert set(templates) == {"social-6", "personal"} and problems == []
    assert load_templates(None)[1] == []
