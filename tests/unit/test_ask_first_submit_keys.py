"""Ask me first on a site without rules holds every act the commit gate counts as a submit, not only a
click and the exact key "Enter" (review of the 2026-11-10 fix wave, finding 2). NumpadEnter,
Shift+Enter, Space on the submit button, and type or fill with a newline all submitted the POST form
with no card. These started as the reviewer's proofs; they now assert the form is read and held."""

import pytest
from test_handback import LANE, Ctx, env  # noqa: F401 - env is a fixture

from agentos.core.models import ActionKind
from agentos.node import fields as fields_mod
from agentos.node import tools as node_tools
from agentos.node.egress.helpers import _press_mode

POST_FORM = {"action": "shop.test/account", "method": "post", "fields": [], "raw": "x"}

SUBMITS = [
    (ActionKind.PRESS, "Enter"),
    (ActionKind.PRESS, "NumpadEnter"),
    (ActionKind.PRESS, "Shift+Enter"),
    (ActionKind.PRESS, "Control+Enter"),
    (ActionKind.PRESS, "Space"),
    (ActionKind.PRESS, " "),
    (ActionKind.TYPE, "Ann\n"),
    (ActionKind.TYPE, "Ann\r"),
    (ActionKind.FILL, "Ann\n"),
    (ActionKind.SELECT, "Express"),
]


class Target:
    """The element the act is on: records the mode the form is read for."""

    def __init__(self):
        self.modes: list[str] = []

    async def evaluate(self, js, arg):
        assert js == fields_mod.FORM_DIFF_JS
        self.modes.append(arg[0])
        return dict(POST_FORM)


@pytest.mark.parametrize(("action", "text"), SUBMITS)
async def test_every_act_the_gate_counts_as_a_submit_reads_the_form(env, action, text):  # noqa: F811
    _mgr, page, runtime = env
    assert _press_mode(action.value, text) is not None  # the gate itself calls it a submit
    target = Target()
    page.locator = lambda selector: target
    form, ungated = await node_tools.form_before_submit(Ctx(runtime), LANE, "e9", action, text)
    assert form is not None and form["method"] == "post"
    assert ungated == "shop.test"  # a POST to a host no rule reads: Ask me first holds it as a commit
    assert target.modes == ["click" if action is ActionKind.CLICK else "key"]


@pytest.mark.parametrize(("action", "text"), [
    (ActionKind.PRESS, "Tab"),
    (ActionKind.PRESS, "a"),
    (ActionKind.TYPE, "Ann"),
    (ActionKind.FILL, "no newline here"),
    (ActionKind.HOVER, None),
])
async def test_control_acts_that_submit_nothing_read_no_form(env, action, text):  # noqa: F811
    _mgr, page, runtime = env
    target = Target()
    page.locator = lambda selector: target
    assert _press_mode(action.value, text) is None
    assert await node_tools.form_before_submit(Ctx(runtime), LANE, "e9", action, text) == (None, None)
    assert target.modes == []


@pytest.mark.parametrize(("action", "text"), SUBMITS)
async def test_the_form_is_read_again_for_the_same_act(env, action, text):  # noqa: F811
    """check_form_unchanged reads the form with the act's own mode: a newline or key submit that was
    approved does not fail as "no longer submits it"."""
    _mgr, page, _runtime = env
    target = Target()
    page.locator = lambda selector: target
    form = fields_mod.form_diff(dict(POST_FORM))
    await node_tools.check_form_unchanged(page, "e9", action, form, text)
    assert target.modes == ["key"]


def test_the_hold_and_the_gate_share_one_definition():
    """The page script reads a form for the gate's "key" mode, and "press" means the same."""
    assert "action === 'key'" in fields_mod.FORM_DIFF_JS
    assert "action === 'press'" not in fields_mod.FORM_DIFF_JS
