"""The connector steps of a flow execution (docs/design/connectors.md 4.5): assert_identity, the
preflight, extract, read_json and for_each items.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from agentos.core.driver import JsonResponse
from agentos.core.locator import Locator
from agentos.core.template import TemplateError, render
from agentos.runner.execution import (
    BODY_WAIT_S,
    IDENTITY_CODES,
    SELF_ALIASES,
    WATCH_POLL_S,
    ExecState,
    NoControl,
    RunControl,
    StepFailure,
    guarded,
    norm_text,
)
from agentos.runner.readvalues import (
    OUTPUT_CAP,
    cap_output,
    is_reading,
    json_body,
    json_kind,
    json_rows,
    json_value,
    on_site,
    output_labels,
    picked_by_caller,
    typed_value,
)
from agentos.runner.steps import (
    ExtractField,
    ExtractStep,
    ForEachStep,
    IdentityCheck,
    NavigateStep,
    PreflightStep,
    ReadJsonStep,
    watchers,
)
from agentos.runner.util import clip_text, norm_lines

log = logging.getLogger("agentos.runner")


class ConnectorStepsMixin:
    """The connector steps of FlowExecutor (agentos.runner.executor)."""

    async def _identity(self, check: IdentityCheck, state: ExecState) -> dict[str, Any]:
        """Who the page acts as, compared with `expect`. Raises wrong_account / wrong_surface
        (with a lane hold) for another id, identity_unknown when none can be read."""
        d = state.driver
        try:
            expect = render(check.expect, state.scope).strip()
        except TemplateError as e:
            raise StepFailure("missing_param", e.message, acted=False) from e
        if not expect:
            raise StepFailure("missing_param", "the identity to expect is empty", acted=False)
        kind = check.acting_as
        seen: str | None = None
        source = check.source
        if check.source == "cookie":
            fn = getattr(d, "account", None)
            if fn is None:
                raise StepFailure("unsupported", "this node's flow driver cannot read the account identity",
                                  acted=False)
            site = check.site or state.meta.get("site")
            ident = await guarded(fn(site, state.flow.domain), "find")
            if ident:
                seen, source = str(ident.get("user_id") or "") or None, ident.get("source") or "cookie"
            where = f"the identity cookie of {site or state.flow.domain or 'the site'}"
        elif check.source == "url":
            # an alias (LinkedIn's /in/me/) is not an id yet: wait until the site redirected it to
            # the account's own URL; one that never resolves is unknown, never another account
            deadline = self.clock.monotonic() + check.timeout_ms / 1000
            while ((m := re.search(check.pattern or "", await d.current_url())) is None
                   or m.group(1).casefold() in SELF_ALIASES) and self.clock.monotonic() < deadline:
                await self.clock.sleep(self.poll_s)
            seen = m.group(1) if m and m.group(1).casefold() not in SELF_ALIASES else None
            where = "the page URL"
        else:
            loc = self._loc(check.locator, state)
            await guarded(d.find(loc, timeout_ms=check.timeout_ms), "find", loc)
            st = await guarded(d.read_state(loc), "find", loc)
            if not st.found:
                code = "ambiguous_locator" if st.ambiguous else "element_not_found"
                raise StepFailure(code, f"the element whose link names the identity: {st.count} matches",
                                  acted=False, locator=loc, expected=f"exactly one {loc.describe()}",
                                  found=f"{st.count} matches")
            m = re.search(check.pattern or "", st.attributes.get("href") or "")
            seen = m.group(1) if m else None
            where = f"the href of {loc.describe()}"
        if not seen:
            raise StepFailure("identity_unknown", f"could not read who this run acts as from {where}; the page "
                              "may have changed or the lane is signed out. Nothing was done.", acted=False,
                              expected=f"{kind} {expect}", found="no id")
        if seen.casefold() != expect.casefold():
            code = IDENTITY_CODES.get(kind, "wrong_surface")
            what = "account" if kind == "account" else kind.replace("_", " ")
            raise StepFailure(code, f"the lane acts as another {what} than {expect}. Nothing was done and the lane "
                              "is held: check it in the live view (browser_view), then hand it back.", acted=False,
                              expected=expect, found=f"another {what}", hold=f"needs_human:{code}")
        return {"acting_as": kind, "id": seen, "source": source, "status": "match"}

    async def _preflight(self, step: PreflightStep, state: ExecState) -> list[dict[str, Any]]:
        """Right before the commit: the composer holds exactly what was approved. The page checks are
        read again until they hold or the step's time is up; acting as is checked once they hold."""
        deadline = self.clock.monotonic() + step.timeout_ms / 1000
        while True:
            results = await self._preflight_reads(step, state)
            if all(r["passed"] for r in results) or self.clock.monotonic() >= deadline:
                break
            await self.clock.sleep(self.poll_s)
        _preflight_verdict(results)
        for i, c in enumerate(step.checks):
            if c.kind == "acting_as":
                results.append({"check": i, "kind": c.kind, "passed": True, **await self._identity(c.identity(), state)})
        return results

    async def _preflight_reads(self, step: PreflightStep, state: ExecState) -> list[dict[str, Any]]:
        """One read of every page check of a preflight (acting as is not a page check)."""
        results: list[dict[str, Any]] = []
        for i, c in enumerate(step.checks):
            if c.kind == "acting_as":
                continue
            loc = self._loc(c.locator, state)
            st = await guarded(state.driver.read_state(loc), "find", loc)
            if c.kind == "value_equals":
                results.append(_value_equals(i, c, loc, st, state))
            elif c.kind == "text_differs":
                results.append(_text_differs(i, c, loc, st, state))
            else:
                ok = st.count == c.equals
                results.append({"check": i, "kind": c.kind, "locator": loc.describe(), "passed": ok,
                                "expected": c.equals, "found": st.count})
        return results

    async def _extract(self, step: ExtractStep, state: ExecState) -> dict[str, Any]:
        d = state.driver
        fn = getattr(d, "extract_items", None)
        if fn is None:
            raise StepFailure("unsupported", "this node's flow driver cannot extract records", acted=False)
        loc = self._loc(step.items, state)
        fields = {name: f.model_copy(update={"locator": self._loc(f.locator, state)}) if f.locator else f
                  for name, f in step.fields.items()}
        pick = {"pick": step.pick} if step.pick else {}  # a driver from before `pick` still reads a plain extract
        rows, total = await guarded(fn(loc, fields, step.max_items, **pick), "find", loc)
        url = await d.current_url()
        items: list[dict[str, Any]] = []
        problems: list[str] = []
        clipped = total > step.max_items or len(rows) > step.max_items
        for k, row in enumerate(rows[: step.max_items]):
            rec: dict[str, Any] = {}
            for name, f in step.fields.items():
                value, cut, problem = typed_value(row.get(name), f, url)
                if problem:
                    problems.append(f"item {k} {name}: {problem}")
                clipped = clipped or cut
                rec[name] = value
            if step.drop_empty and all(v is None for v in rec.values()):
                total -= 1
                continue
            items.append(rec)
        if problems:
            raise StepFailure("output_contract_violation", "extracted values do not match the connector's types (the "
                              f"site may have changed shape): {'; '.join(problems[:5])}", acted=False, locator=loc,
                              found=problems[:10])
        saved = {"items": items, "count": len(items), "total": total, "truncated": clipped}
        labels = {"untrusted": sorted(n for n, f in step.fields.items() if f.untrusted),
                  "private": sorted(n for n, f in step.fields.items() if f.private)}
        out: dict[str, Any] = {"url": url, "extracted": len(items), "total": total,
                               "vars": {step.save_as: saved}, "labels": {step.save_as: labels}}
        if clipped:
            out["truncated_vars"] = [step.save_as]
        return out

    def _watch(self, steps: list[Any], i: int, prefix: str, state: ExecState) -> None:
        """Before a navigate loads, start listening for each read_json that watches it."""
        if not isinstance(steps[i], NavigateStep):
            return
        for j in watchers(steps, i):
            fn = getattr(state.driver, "watch_json", None)
            if fn is None:
                raise StepFailure("unsupported", "this node's flow driver cannot read a page's JSON", acted=False)
            state.watches[f"{prefix}{j}"] = fn(on_site(steps[j].url, state.flow.domain))
            if picked_by_caller(steps[i].url, state):
                state.picked.add(f"{prefix}{j}")

    async def _read_json(self, step: ReadJsonStep, path: str, state: ExecState, control: RunControl) -> dict[str, Any]:
        """A failure names the step's own pattern and a status, nothing of the response: its URL can
        hold a token, and its body is only for the declared fields."""
        d = state.driver
        watch = state.watches.pop(path, None)
        if watch is None:
            raise StepFailure("invalid", "read_json has no page load to watch: it follows its navigate", acted=False)
        try:
            deadline = self.clock.monotonic() + step.timeout_ms / 1000
            while not watch.responses and self.clock.monotonic() < deadline + (BODY_WAIT_S if is_reading(watch) else 0):
                control.check()
                await self.clock.sleep(WATCH_POLL_S)
            seen = next(iter(watch.responses), None)
            if seen is None and is_reading(watch):
                seen = JsonResponse(step.url, 0, problem="slow")
        finally:
            watch.close()
        self._check_signal(await guarded(d.signal(), "find"), expected=f"a JSON response on {step.url}")
        rows = json_rows(json_body(seen, step, picked=path in state.picked), step)
        url = await d.current_url()
        fields = {name: (f, ExtractField(attr="value", format=f.format, host=f.host, max_chars=f.max_chars))
                  for name, f in step.fields.items()}
        items: list[dict[str, Any]] = []
        problems: list[str] = []
        clipped = len(rows) > step.max_items
        for k, row in enumerate(rows[: step.max_items]):
            if not isinstance(row, dict):
                problems.append(f"item {k}: is {json_kind(row)}, not an object")
                continue
            rec: dict[str, Any] = {}
            for name, (f, shape) in fields.items():
                value, cut, problem = json_value(row, f, shape, url)
                if problem:
                    problems.append(f"item {k} {name}: {problem}")
                clipped = clipped or cut
                rec[name] = value
            items.append(rec)
        if problems:
            raise StepFailure("output_contract_violation", "the JSON does not match the connector's types (the site may "
                              f"have changed shape): {'; '.join(problems[:5])}", acted=False, found=problems[:10])
        saved = {"items": items, "count": len(items), "total": len(rows), "truncated": clipped}
        labels = {"untrusted": sorted(n for n, f in step.fields.items() if f.untrusted),
                  "private": sorted(n for n, f in step.fields.items() if f.private)}
        out: dict[str, Any] = {"url": url, "extracted": len(items), "total": len(rows),
                               "vars": {step.save_as: saved}, "labels": {step.save_as: labels}}
        if clipped:
            out["truncated_vars"] = [step.save_as]
        return out

    @staticmethod
    def _loop_items(step: ForEachStep, state: ExecState) -> list[Any]:
        value = state.scope["vars"].get(step.over)
        items = value.get("items") if isinstance(value, dict) and "items" in value else value
        if not isinstance(items, list):
            raise StepFailure("invalid", f"for_each over {step.over!r}: it holds no list", acted=False)
        return items[: step.max_items]

    async def _discard(self, state: ExecState) -> str | None:
        """Run the connector's discard steps (close the composer without saving), best effort.
        None when there are none."""
        try:
            steps = state.flow.discard_steps()
        except ValueError:
            return "skipped: the discard steps are invalid"
        if not steps or state.driver is None:
            return None
        try:
            for i, step in enumerate(steps):
                await self._run_step(step, f"discard.{i}", state, NoControl())
        except Exception as e:  # noqa: BLE001 - the failure that stopped the run is what matters
            msg = e.message if isinstance(e, StepFailure) else f"{type(e).__name__}: {e}"
            return f"failed: {clip_text(state.scrub(msg), 200)}"
        return "done"

    def _output(self, state: ExecState) -> tuple[dict[str, Any] | None, list[str]]:
        """Run.output for a connector run: outputs mapped from the saved values, checked
        against the output schema, labelled and capped (docs/design/connectors.md 4.10)."""
        from agentos.runner.connectors.outputs import outputs_from_saved

        meta = state.meta
        data, problems = outputs_from_saved(meta, state.scope["vars"])
        if problems:
            return None, problems
        untrusted, private = output_labels(meta.get("outputs") or {}, state.labels)
        truncated = any(path.split(".", 1)[0] in state.truncated for path in (meta.get("outputs") or {}).values())
        data, capped = cap_output(data)
        if data is None:
            return None, [f"data: over the {OUTPUT_CAP // 1024} KB cap with nothing to trim"]
        output = {
            "data": data, "untrusted_fields": untrusted, "private_fields": private,
            "truncated": truncated or capped,
            "source": {"connector": meta.get("connector"), "operation": meta.get("operation"),
                       "version": meta.get("version"), "contract": meta.get("contract"),
                       "flow_version": state.run.flow_version, "surface": state.run.surface, "lane": state.run.lane,
                       "read_at": self.clock.now().isoformat()},
        }
        return state.scrub(output), []

    async def _screenshot(self, state: ExecState, path: str) -> str | None:
        if self.shots_dir is None or state.driver is None:
            return None
        try:
            data = await state.driver.screenshot()
        except Exception:  # noqa: BLE001 - a missing screenshot must not hide the real failure
            return None
        rel = Path(state.run.id) / f"step-{path.replace('.', '_')}-a{state.run.attempts}.png"
        target = self.shots_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        try:
            return str(target.relative_to(self.ref_base)) if self.ref_base else str(target)
        except ValueError:
            return str(target)


def _value_equals(i: int, c: Any, loc: Locator, st: Any, state: ExecState) -> dict[str, Any]:
    """The composer's field holds the approved value (compared with whitespace collapsed)."""
    want = state.scope.get(c.param)
    if want is None:
        raise StepFailure("missing_param", f"param {c.param!r} has no value", acted=False)
    ok = st.found and norm_text(st.value or "") == norm_text(str(want))
    found = (("the value it expected" if ok else f"another value of {len(st.value or '')} characters")
             if st.found else f"{st.count} matches" + (" (ambiguous)" if st.ambiguous else ""))
    return {"check": i, "kind": c.kind, "locator": loc.describe(), "passed": ok,
            "expected": f"${{{c.param}}}", "found": found}


def _text_differs(i: int, c: Any, loc: Locator, st: Any, state: ExecState) -> dict[str, Any]:
    """Exactly one element (nth never picks one of several), and its text is not the value the edit
    sets: an edit to the value already there is a no-op the site may still record, so it stops here.
    `same` marks that case alone (one element, read, showing exactly that value): the run ends
    already_current."""
    want = render(c.text or "", state.scope)
    one = st.found and not st.ambiguous and st.count == 1
    shown = st.lines if c.lines else st.text
    norm = norm_lines if c.lines else norm_text
    same = one and shown is not None and norm(shown) == norm(want)
    ok = one and shown is not None and not same
    found = (clip_text(shown or "", 200) if one
             else f"{st.count} matches" + (" (ambiguous)" if st.count > 1 else ""))
    return {"check": i, "kind": c.kind, "locator": loc.describe(), "passed": ok,
            "expected": f"not {c.text}", "found": found, "same": same}


def _preflight_verdict(results: list[dict[str, Any]]) -> None:
    """Raise when a page check failed: already_current when every failed text_differs found its one
    element showing exactly the value (nothing to do, neither a mismatch nor a broken connector),
    else preflight_mismatch. Drops the `same` marks either way."""
    bad = [r for r in results if not r["passed"]]
    current = bool(bad) and all(r.get("same") for r in bad)
    for r in results:
        r.pop("same", None)
    if not bad:
        return
    shown = "; ".join(f"{r['kind']} {r['locator']}: expected {r['expected']}, found {r['found']}" for r in bad)
    expected = [{"kind": r["kind"], "locator": r["locator"], "expected": r["expected"]} for r in bad]
    if current:
        raise StepFailure("already_current", f"the page already shows the value this edit sets, so "
                          f"nothing was saved: {shown}", acted=False, expected=expected,
                          found=[r["found"] for r in bad])
    why = ("the page could not be read as this edit expects (the element is missing or not alone), so "
           "nothing was saved" if any(r["kind"] == "text_differs" for r in bad) else
           "the composer does not hold exactly what was approved, so nothing was posted")
    raise StepFailure("preflight_mismatch", f"{why}: {shown}", acted=False, expected=expected,
                      found=[r["found"] for r in bad])
