"""Workspace templates: the built-in ones and user TOML files in `$OPENAJAR_HOME/templates/`.

A template is a list of surfaces ("for each company: these six surfaces"). Applying one creates
one lane per surface in each chosen workspace (see planner.py). A user file is `<id>.toml`:

    title = "Shop"
    description = "Store, marketplace and support"

    [[surfaces]]
    name = "store"                                # lane name: <workspace>/store
    platform = "shopify"                          # proxy rules: one account per platform per IP
    home_url = "https://shop.example.com/"        # optional
    login_url = "https://shop.example.com/admin"  # optional
    sites = ["shop.example.com", "*.shopifycdn.com"]  # optional: the lane's site list (egress guard)

A bad file is reported by `load_templates`, never raised, so listing templates always works.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from openajar.workspaces.models import (
    LANE_NAME_RE,
    NAME_MAX,
    NOTE_MAX,
    PLATFORM_RE,
    TEMPLATE_ID_RE,
    URL_MAX,
    Template,
    TemplateLane,
    TemplateProblem,
)

ENGINES = ("camoufox",)
MAX_SURFACES = 50
_TOP_KEYS = {"title", "description", "surfaces"}
_SURFACE_KEYS = {"name", "platform", "engine", "home_url", "login_url", "sites"}


class TemplateInvalid(Exception):
    """A template that cannot be used. `problems` has one line per fault."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def builtin_templates() -> dict[str, Template]:
    """The templates that ship with openajar. A user file may not reuse these names."""
    social_6 = Template(
        id="social-6",
        title="One company: six surfaces",
        description="Instagram, Facebook, Reddit, Medium, a blog and LinkedIn for one company.",
        builtin=True,
        surfaces=[
            TemplateLane(name="instagram", platform="instagram", home_url="https://www.instagram.com/",
                    login_url="https://www.instagram.com/accounts/login/"),
            TemplateLane(name="facebook", platform="facebook", home_url="https://www.facebook.com/",
                    login_url="https://www.facebook.com/login/"),
            TemplateLane(name="reddit", platform="reddit", home_url="https://www.reddit.com/",
                    login_url="https://www.reddit.com/login/"),
            TemplateLane(name="medium", platform="medium", home_url="https://medium.com/",
                    login_url="https://medium.com/m/signin"),
            # A blog's address belongs to the company, so the template cannot fix it.
            TemplateLane(name="blog", platform="blog"),
            TemplateLane(name="linkedin", platform="linkedin", home_url="https://www.linkedin.com/feed/",
                    login_url="https://www.linkedin.com/login"),
        ],
    )
    personal = Template(
        id="personal",
        title="Personal accounts",
        description="Your own Instagram, Facebook, Reddit and LinkedIn accounts.",
        builtin=True,
        surfaces=[
            TemplateLane(name="instagram", platform="instagram", home_url="https://www.instagram.com/",
                    login_url="https://www.instagram.com/accounts/login/"),
            TemplateLane(name="facebook", platform="facebook", home_url="https://www.facebook.com/",
                    login_url="https://www.facebook.com/login/"),
            TemplateLane(name="reddit", platform="reddit", home_url="https://www.reddit.com/",
                    login_url="https://www.reddit.com/login/"),
            TemplateLane(name="linkedin", platform="linkedin", home_url="https://www.linkedin.com/feed/",
                    login_url="https://www.linkedin.com/login"),
        ],
    )
    return {template.id: template for template in (social_6, personal)}


def _https_url(value: object, label: str, problems: list[str]) -> str | None:
    """An https address with no credentials in it. Returns it, or records why it is not one."""
    if not isinstance(value, str) or not value or len(value) > URL_MAX or not value.isprintable() \
            or " " in value:
        problems.append(f"{label} must be an https:// address with no spaces")
        return None
    try:
        parts = urlsplit(value)
        hostname, username, password = parts.hostname, parts.username, parts.password
    except ValueError:
        problems.append(f"{label} is not a valid address")
        return None
    if parts.scheme != "https" or not hostname:
        problems.append(f"{label} must be an https:// address")
        return None
    if username or password:
        problems.append(f"{label} must not contain a username or password")
        return None
    return value


def _surface(index: int, raw: object, problems: list[str]) -> TemplateLane | None:
    where = f"surfaces #{index}"
    if not isinstance(raw, dict):
        problems.append(f"{where} must be a table")
        return None
    before = len(problems)
    for key in raw:
        if key not in _SURFACE_KEYS:
            problems.append(f"{where}: unknown key {key!r}")
    name = raw.get("name")
    if name is None:
        problems.append(f"{where}: name is required")
    elif not isinstance(name, str) or not LANE_NAME_RE.fullmatch(name):
        problems.append(f"{where}: name must be 1 to 41 characters (lowercase letters, digits and dashes)")
    platform = raw.get("platform")
    if platform is None:
        problems.append(f"{where}: platform is required")
    elif not isinstance(platform, str) or not PLATFORM_RE.fullmatch(platform):
        problems.append(f"{where}: platform must be 1 to 41 characters (lowercase letters, digits and dashes)")
    engine = raw.get("engine", "camoufox")
    if not isinstance(engine, str) or engine not in ENGINES:
        problems.append(f"{where}: engine must be one of: {', '.join(ENGINES)}")
    home_url = login_url = None
    if "home_url" in raw:
        home_url = _https_url(raw["home_url"], f"{where}: home_url", problems)
    if "login_url" in raw:
        login_url = _https_url(raw["login_url"], f"{where}: login_url", problems)
    sites = None
    if "sites" in raw:
        sites = _sites(raw["sites"], f"{where}: sites", problems)
    if len(problems) > before:
        return None
    return TemplateLane(name=name, platform=platform, engine=engine, home_url=home_url, login_url=login_url,
                        sites=sites)


def _sites(value: object, label: str, problems: list[str]) -> list[str] | None:
    """A surface's site list: domain patterns, as `openajar lane sites` takes them."""
    from openajar.core.models import validate_sites

    if not isinstance(value, list):
        problems.append(f"{label} must be a list of domain patterns")
        return None
    try:
        return validate_sites(value)
    except ValueError as e:
        problems.append(f"{label}: {e}")
        return None


def parse_template(template_id: str, text: str, *, builtin: bool = False, source: str | None = None) -> Template:
    """Parse and validate one template. Raises TemplateInvalid listing every fault found."""
    problems: list[str] = []
    if not TEMPLATE_ID_RE.fullmatch(template_id):
        problems.append("the file name must be 1 to 41 characters (lowercase letters, digits and dashes)")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TemplateInvalid([f"not valid TOML: {exc}"]) from None
    for key in data:
        if key not in _TOP_KEYS:
            problems.append(f"unknown top-level key {key!r}")
    title = data.get("title", template_id)
    if not isinstance(title, str) or not title.strip() or len(title) > NAME_MAX:
        problems.append(f"title must be text of 1 to {NAME_MAX} characters")
    description = data.get("description", "")
    if not isinstance(description, str) or len(description) > NOTE_MAX:
        problems.append(f"description must be text of at most {NOTE_MAX} characters")
    surfaces: list[TemplateLane] = []
    raw_surfaces = data.get("surfaces")
    if raw_surfaces is None:
        problems.append("no [[surfaces]] tables: a template needs at least one surface")
    elif not isinstance(raw_surfaces, list):
        problems.append("surfaces must be written as [[surfaces]] tables")
    elif len(raw_surfaces) > MAX_SURFACES:
        problems.append(f"a template may have at most {MAX_SURFACES} surfaces")
    else:
        seen: dict[str, int] = {}
        for index, raw in enumerate(raw_surfaces, start=1):
            surface = _surface(index, raw, problems)
            if surface is None:
                continue
            if surface.name in seen:
                problems.append(f"surfaces #{index}: surface name {surface.name!r} is already used by "
                                f"surfaces #{seen[surface.name]}")
            else:
                seen[surface.name] = index
            surfaces.append(surface)
    if problems:
        raise TemplateInvalid(problems)
    return Template(id=template_id, title=title.strip(), description=description, surfaces=surfaces,
                    builtin=builtin, source=source)


def load_templates(directory: Path | None) -> tuple[dict[str, Template], list[TemplateProblem]]:
    """Built-in templates plus every valid `*.toml` in `directory`, and a problem for each bad file."""
    templates = builtin_templates()
    problems: list[TemplateProblem] = []
    if directory is None or not directory.is_dir():
        return templates, problems
    for path in sorted(directory.glob("*.toml")):
        template_id = path.stem

        def problem(message: str, _id: str = template_id, _file: str = path.name) -> None:
            problems.append(TemplateProblem(file=_file, template=_id, message=message))

        if template_id in templates:
            problem(f"the name {template_id!r} belongs to a built-in template; rename the file")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            problem("cannot be read as UTF-8 text")
            continue
        try:
            templates[template_id] = parse_template(template_id, text, source=path.name)
        except TemplateInvalid as exc:
            for message in exc.problems:
                problem(message)
    return templates, problems
