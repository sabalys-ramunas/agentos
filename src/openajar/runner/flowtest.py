"""Testing a flow offline: its steps run against a recording, with no browser and no account.

A recording (`<name>.recording.yaml`) is what the pages showed, one page state after another: the
URL, the accessibility outline (the text browser_snapshot returns, without its refs), and for a
state an action led to, which action (`after: click button "Post"`). A replay serves those states
to the real flow executor through the same FlowDriver the dev node uses (openajar.runner.fakes), so
every locator resolves under the reference rules a browser lane follows, readbacks read the
recorded page, and a changed locator fails here as it would on the site:

- a navigate shows the next state loaded at that URL (else the latest one before it);
- a click or a press shows the next state recorded `after` that action, before the next page load,
  or, for a link with no such state, loads the link's URL; any other action stays on the page;
- typing goes into the page as on the dev web, so a value readback reads what the flow typed;
- `${param}` in an outline is the test's param value, so a recorded post shows this test's text.

A test (`<flow id>.test.yaml`, next to the flow) names the recording, the params, and what must
come out: the run's status, the steps that ran, the failing step and its error, and what the
flow would publish (each button pressed while the page held text the run entered).

    flow: acme.linkedin.post        # optional: the file name says it
    cases:
      - name: posts the text on the company page
        recording: acme-company-page.recording.yaml
        params: {text: Fresh rye is out of the oven}
        expect:
          status: succeeded_verified
          ran: [0, 1, 2, 3, 4, 5]
          published:
            - pressed: button "Post"
              fields: {textbox "Text editor for creating content": Fresh rye is out of the oven}

A file with no `cases` is one case. scripts and CI run them with `openajar flows test`.
"""

from __future__ import annotations

import json
import random
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openajar.core.errors import InvalidRequest
from openajar.core.models import NavigateResult, Principal, Signal
from openajar.node.snapshot import parse_line
from openajar.runner.fakes import El, FakeClock, FakeDriver, FakeSite, _link, driver_factory
from openajar.runner.flowdoc import FlowDefinition
from openajar.runner.flowfiles import RECORDING_SUFFIX, SUFFIX, TEST_SUFFIX, load_flow, load_yaml
from openajar.runner.flows import validation_message

FORMAT: Literal[1] = 1
OWNER = Principal(kind="human", id="owner", owner="owner")  # the test saves the flow as the owner would
# outline attributes that are an ARIA state of the element (`[pressed]` is aria-pressed="true")
STATES = frozenset({"pressed", "checked", "expanded", "selected", "disabled", "required", "busy", "invalid"})
DROPPED = frozenset({"ref", "cursor", "box", "active"})  # where the element sits, not what it is
VALUE_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton"})  # `: text` after these is the value
# roles whose accessible name is their own text: a link's words are on the page, and in its parent's text
NAME_FROM_CONTENT = frozenset({"button", "cell", "checkbox", "columnheader", "gridcell", "heading", "link", "menuitem",
                               "menuitemcheckbox", "menuitemradio", "option", "radio", "row", "rowheader", "switch",
                               "tab", "tooltip", "treeitem"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- the files ----------------------------------------------------------------------------


class RecordedPage(_Strict):
    url: str
    title: str = ""
    after: str | None = None  # the action that led here: 'click button "Post"', 'press Enter'
    signal: Literal["ok", "captcha", "checkpoint", "logged_out", "blocked", "rate_limited", "unknown"] = "ok"
    outline: str = Field(min_length=1)


class Recording(_Strict):
    format: Literal[1] = FORMAT
    note: str = ""
    identity: dict[str, str] | None = None  # {site, user_id}: who FlowDriver.account says is signed in
    pages: list[RecordedPage] = Field(min_length=1)


class Published(_Strict):
    pressed: str
    fields: dict[str, str]


class Expect(_Strict):
    status: str | None = None
    ran: list[str | int] | None = None
    failed_step: str | int | None = None
    error: str | None = None
    published: list[Published] | None = None


class Case(_Strict):
    name: str = "default"
    recording: str
    params: dict[str, Any] = Field(default_factory=dict)
    on: str | None = None
    expect: Expect = Field(default_factory=Expect)


class TestFile(_Strict):
    flow: str | None = None
    cases: list[Case] = Field(min_length=1)


def _model(cls: type[BaseModel], data: Any, where: str) -> Any:
    try:
        return cls.model_validate(data)
    except ValidationError as e:
        raise InvalidRequest(f"{where}: {validation_message(e)}" if where else validation_message(e)) from None


def load_recording(path: Path, *, named: bool = True) -> Recording:
    """A recording file, checked. `named`: errors start with the file's path."""
    where = str(path) if named else ""
    if not path.is_file():
        raise InvalidRequest(f"the recording {path} does not exist")
    return _model(Recording, load_yaml(path.read_text(encoding="utf-8"), where), where)


def load_test(path: Path, *, named: bool = True) -> TestFile:
    where = str(path) if named else ""
    data = load_yaml(path.read_text(encoding="utf-8"), where)
    if isinstance(data, dict) and "cases" not in data:
        data = {"flow": data.get("flow"), "cases": [{k: v for k, v in data.items() if k != "flow"}]}
    return _model(TestFile, data, where)


def flow_id_of(test: Path, spec: TestFile) -> str:
    return spec.flow or test.name.removesuffix(TEST_SUFFIX)


# ---- outlines into pages ------------------------------------------------------------------


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        try:
            return str(json.loads(text))
        except ValueError:
            return text[1:-1]
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def _attrs(flags: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for flag in flags:
        key, sep, value = flag.partition("=")
        if key in DROPPED:
            continue
        if key in STATES:
            out[f"aria-{key}"] = value if sep else "true"
            if key == "disabled":
                out["disabled"] = value if sep else "true"
        else:
            out[key] = value if sep else "true"
    return out


def parse_outline(text: str, title: str = "") -> El:
    """An outline (browser_snapshot's text) as the element tree the reference resolver reads:
    role, accessible name, [attributes], `: text` (a text box's value), `/url:` as href."""
    root = El("document", title)
    stack: list[tuple[int, El]] = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip():
            continue
        line = parse_line(raw)
        body = raw.strip().removeprefix("- ")
        while stack[-1][0] >= line.depth:
            stack.pop()
        parent = stack[-1][1]
        if line.role is None:
            if body.startswith("/"):  # a property of the element above: /url, /placeholder
                key, _, value = body[1:].partition(":")
                parent.attrs["href" if key == "url" else key] = _unquote(value)
            else:
                parent.add(El("text", text=_unquote(body.removeprefix("text:"))))
            continue
        _start, end = line.attrs_span or (len(raw), len(raw))
        tail = raw[end:].strip()
        el = El(line.role, line.name or "", attrs=_attrs(line.attrs))
        if tail.startswith(":") and tail != ":":
            words = _unquote(tail[1:])
            if line.role in VALUE_ROLES:
                el.attrs["value"] = words
            else:
                el.text = words
        parent.add(el)
        stack.append((line.depth, el))
    _name_as_text(root)
    return _link(root)


def _has_text(el: El) -> bool:
    return bool(el.text) or any(_has_text(c) for c in el.children)


def _name_as_text(el: El) -> None:
    """A link or a button shows its name as its text, unless the outline lists that text below it."""
    for child in el.children:
        _name_as_text(child)
    if el.role in NAME_FROM_CONTENT and el.name and not _has_text(el):
        el.text = el.name


def fill_params(text: str, params: dict[str, Any]) -> str:
    for name, value in params.items():
        text = text.replace("${" + name + "}", value if isinstance(value, str) else json.dumps(value))
    return text


def describe(el: El) -> str:
    return f'{el.role} "{el.name}"' if el.name else el.role


# ---- the replay ---------------------------------------------------------------------------


class ReplayDriver(FakeDriver):
    """A FlowDriver over a recording: the page states in order, moved through by the flow's own
    navigations and actions (see the module's docstring)."""

    def __init__(self, recording: Recording, params: dict[str, Any]):
        super().__init__(FakeSite())
        self.recording = recording
        self.params = params
        self.state: int | None = None
        self.labels: dict[str, str] = {}  # field key -> its description, for `published`
        self.published: list[dict[str, Any]] = []
        self.site.identity = dict(recording.identity) if recording.identity else None

    def _page(self) -> RecordedPage | None:
        return None if self.state is None else self.recording.pages[self.state]

    def _render(self) -> None:
        page = self._page()
        if page is None:
            self.root = _link(El("document", "Not found"))
            return
        self.root = parse_outline(fill_params(page.outline, self.params), page.title)

    def _show(self, index: int | None, url: str) -> None:
        if url != self.url:
            self.values = {}
        self.state, self.url = index, url
        self._render()
        page = self._page()
        self.site.signal = Signal(page.signal) if page is not None else Signal.OK

    def _forward(self, after: str) -> int | None:
        """The next state recorded after this action, before the next page load."""
        start = -1 if self.state is None else self.state
        for i in range(start + 1, len(self.recording.pages)):
            page = self.recording.pages[i]
            if page.after is None:
                return None
            if page.after.strip() == after:
                return i
        return None

    async def navigate(self, url: str) -> NavigateResult:
        self._hook("navigate", url)
        pages = self.recording.pages
        start = -1 if self.state is None else self.state
        index = next((i for i in range(start + 1, len(pages)) if pages[i].url == url and pages[i].after is None),
                     None)
        if index is None:
            index = next((i for i in range(min(start, len(pages) - 1), -1, -1) if pages[i].url == url), None)
        self._show(index, url)
        self.log.append(("navigate", url))
        return NavigateResult(url=url, title=self.root.name, status=200 if index is not None else 404,
                              signal=self.site.signal)

    def _note_published(self, what: str) -> None:
        fields = {self.labels.get(k, k): v for k, v in self.values.items() if v}
        if fields:
            self.published.append({"pressed": what, "fields": fields})

    async def click(self, element: El) -> None:
        self._hook("click", element)
        what = describe(element)
        self._note_published(what)
        index = self._forward(f"click {what}")
        if index is not None:
            self._show(index, self.recording.pages[index].url)
        elif element.attrs.get("href"):
            await self.navigate(urljoin(self.url, element.attrs["href"]))
        else:
            self._render()
        self.log.append(("click", what))

    async def press(self, key: str, element: El | None = None) -> None:
        self._hook("press", element)
        self._note_published(f"press {key}")
        index = self._forward(f"press {key}")
        if index is not None:
            self._show(index, self.recording.pages[index].url)
        self.log.append(("press", key))

    def _entered(self, element: El) -> None:
        self.labels[element.key or element.name] = describe(element)

    async def type(self, element: El, text: str) -> None:
        self._entered(element)
        await super().type(element, text)

    async def fill(self, element: El, text: str) -> None:
        self._entered(element)
        await super().fill(element, text)

    async def select(self, element: El, option: str) -> None:
        self._entered(element)
        await super().select(element, option)

    async def upload(self, element: El, file_ref: str) -> None:
        self._entered(element)
        await super().upload(element, file_ref)

    async def screenshot(self) -> bytes:
        return b"\x89PNG replay of " + self.url.encode()


# ---- running a case -----------------------------------------------------------------------


@dataclass
class CaseResult:
    name: str
    problems: list[str] = field(default_factory=list)
    run: dict[str, Any] = field(default_factory=dict)
    published: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems


async def run_case(defn: FlowDefinition, case: Case, recording: Recording, home: Path) -> CaseResult:
    """Run the flow once on the recording, in a runner of its own on `home`, and check the case."""
    from openajar.core.runtime import Runtime
    from openajar.runner.service import build

    driver = ReplayDriver(recording, case.params)
    runtime = Runtime()
    runner = build(runtime, home=home, driver_factory=driver_factory(driver), clock=FakeClock(),
                   rng=random.Random(7))
    out = CaseResult(case.name)
    try:
        runtime.flows.save(defn, by=OWNER)
        params = runtime.flows.validate_params(runtime.flows.get(defn.id), case.params)
        run, _ = runner.submit(flow_id=defn.id, params=params, lane=case.on, principal=OWNER, source="test",
                               authorized=True)
        await runner.run_until_idle()
        out.run = runner.describe(runner.get(run.id), steps=True)
    except InvalidRequest as e:
        out.problems.append(e.message)
        return out
    finally:
        runner.store.close()
    out.published = driver.published
    out.problems = check(case.expect, out.run, driver.published)
    return out


def _error_line(run: dict[str, Any]) -> str:
    err = run.get("error") or {}
    if not err:
        return ""
    where = f"step {err.get('step')} ({err.get('type')}): " if err.get("step") is not None else ""
    line = f"{where}{err.get('code')}: {err.get('message')}"
    if err.get("expected") or err.get("found"):
        line += f" (expected {err.get('expected')}, found {err.get('found')})"
    return line


def check(expect: Expect, run: dict[str, Any], published: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    status = str(run.get("status"))
    if expect.status is not None and status != expect.status:
        why = _error_line(run)
        problems.append(f"status: expected {expect.status}, got {status}" + (f": {why}" if why else ""))
    steps = run.get("steps") or []
    if expect.ran is not None:
        ran = [str(s["path"]) for s in steps if s.get("status") == "done"]
        want = [str(p) for p in expect.ran]
        if ran != want:
            problems.append(f"ran: expected steps {', '.join(want) or 'none'}, got {', '.join(ran) or 'none'}")
    err = run.get("error") or {}
    if expect.failed_step is not None and str(err.get("step")) != str(expect.failed_step):
        got = f"step {err.get('step')}" if err else "no failure"
        problems.append(f"failed_step: expected step {expect.failed_step} to fail, got {got}")
    if expect.error is not None:
        text = f"{err.get('code')}: {err.get('message')}" if err else ""
        if expect.error not in text:
            problems.append(f"error: expected one that says {expect.error!r}, got {text or 'none'}")
    if expect.published is not None:
        want_pub = [p.model_dump() for p in expect.published]
        if published != want_pub:
            problems.append("published: expected " + _show_pub(want_pub) + ", got " + _show_pub(published))
    return problems


def _show_pub(items: list[dict[str, Any]]) -> str:
    if not items:
        return "nothing"
    return "; ".join(f"{p['pressed']} with " + ", ".join(f"{k} = {v!r}" for k, v in p["fields"].items())
                     for p in items)


# ---- finding and running test files ---------------------------------------------------------


@dataclass
class FileResult:
    path: Path
    flow: str | None = None
    error: str | None = None  # the test file, its flow or a recording could not be used
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.cases)


def find_tests(paths: list[Path]) -> list[Path]:
    """Test files under directories, the test files named, and the tests next to flow files named."""
    out: set[Path] = set()
    for p in paths:
        if p.is_dir():
            out.update(f for f in p.rglob("*" + TEST_SUFFIX) if f.is_file())
        elif p.name.endswith(TEST_SUFFIX):
            out.add(p)
        elif p.name.endswith(SUFFIX) and not p.name.endswith(RECORDING_SUFFIX):
            test = p.with_name(p.name.removesuffix(SUFFIX) + TEST_SUFFIX)
            if test.is_file():
                out.add(test)
        elif not p.exists():
            raise InvalidRequest(f"{p} does not exist")
    return sorted(out)


async def run_test_file(path: Path) -> FileResult:
    result = FileResult(path)
    try:
        spec = load_test(path)
        result.flow = flow_id_of(path, spec)
        flow_path = path.with_name(result.flow + SUFFIX)
        if not flow_path.is_file():
            raise InvalidRequest(f"{path.name} tests flow {result.flow}, but {flow_path} does not exist")
        ff = load_flow(flow_path)
        if ff.definition is None or ff.errors:
            raise InvalidRequest(f"{flow_path}: " + "; ".join(ff.errors))
        cases = [(case, load_recording(path.parent / case.recording)) for case in spec.cases]
    except InvalidRequest as e:
        result.error = e.message
        return result
    for case, recording in cases:
        with tempfile.TemporaryDirectory(prefix="openajar-flowtest-") as home:
            result.cases.append(await run_case(ff.definition, case, recording, Path(home)))
    return result


def validate_extras(paths: list[Path]) -> list[tuple[Path, str]]:
    """Offline checks of the test files and recordings under `paths` (their shape, and that a test
    names a flow and recordings that are there)."""
    problems: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for p in paths:
        files = list(p.rglob("*.yaml")) if p.is_dir() else [p]
        for f in sorted(files):
            if f in seen:
                continue
            seen.add(f)
            try:
                if f.name.endswith(RECORDING_SUFFIX):
                    load_recording(f, named=False)
                elif f.name.endswith(TEST_SUFFIX):
                    spec = load_test(f, named=False)
                    flow = flow_id_of(f, spec)
                    if not f.with_name(flow + SUFFIX).is_file():
                        problems.append((f, (f"it tests flow {flow}, but {f.with_name(flow + SUFFIX).name} is not "
                                            "next to it")))
                    for case in spec.cases:
                        if not (f.parent / case.recording).is_file():
                            problems.append((f, f"case {case.name!r}: the recording {case.recording} does not exist"))
            except InvalidRequest as e:
                problems.append((f, e.message))
    return problems
