"""Logins for agent environments (agentos.environments.logins): the owner's bindings by title, repository
or all, the vault's decision on an environment's lane, the agent's `login` through the environment's
routing and lease, signing in again, the owner's CLI and sign-in now, and that no value reaches argv, a
log, a trace, a result or a replay caption, nor a frame taken while a fill runs."""

from __future__ import annotations

import inspect
import json
import logging
from types import SimpleNamespace

import pytest
from test_environments import node, opened  # noqa: F401  (node is a fixture)
from test_environments_replay import Shots, settled
from test_vault_support import FakeTypist, Page, fld, make_vault, private_home
from typer.testing import CliRunner

from agentos.api.bridge import panel_specials
from agentos.core.errors import AgentosError, InvalidRequest, NotFound
from agentos.core.models import Effect, Signal
from agentos.environments import logins
from agentos.environments import replay as replay_mod
from agentos.environments.models import Environment
from agentos.node import vault_tools
from agentos.policy.wrap import ToolCallError, bind_request_token, wrap_tool
from agentos.vault.models import EntryMeta
from agentos.vault.policy import Reason, Sink, decide, entry_bound

SITE = "http://localhost:8099"
LOGIN_URL = SITE + "/login"
PASSWORD = "e2e-unit-Pass-90817-zz"
USER = "agent-user@example.org"
TITLE = "Shop fix"


def site_form(origin: str = SITE) -> list:
    return [fld("user", origin, type="email", name="username", autocomplete="username", form="f"),
            fld("pass", origin, type="password", name="password", autocomplete="current-password", form="f")]


def fixture_site() -> FakeTypist:
    """The login page, the home page after the submit, and a blank tab to start from."""
    return FakeTypist({"blank": Page("about:blank", []), "login": Page(LOGIN_URL, site_form(), on_enter="home"),
                       "home": Page(SITE + "/", [])}, "blank")


@pytest.fixture
async def vault(tmp_path):
    (tmp_path / "vault").mkdir()
    v = make_vault(private_home(tmp_path / "vault"))
    await v.start(control=False)
    yield v
    await v.stop()


FAMILY = "g-family-1"   # the grant family whose computer the owner bound by title


def facts(title: str, repo: str | None = None, family: str = FAMILY) -> dict:
    return {"title": title, "repo": repo, "family": family}


def by_title(title: str = TITLE, family: str = FAMILY) -> dict:
    return {"title": title, "family": family, "agent": "agent-1"}


# ------------------------------------------------------------------ the owner's bindings


async def test_a_login_is_bound_by_title_by_repo_or_to_all_and_used_only_there(vault):
    titled = await logins.add(vault, LOGIN_URL, user=USER, password=PASSWORD, scope=by_title())
    assert titled.name == "localhost" and titled.url == LOGIN_URL and titled.made
    meta = await vault.get_entry(titled.ref)
    assert meta.lanes == ["env/*"] and meta.config["envs"]["scopes"] == [by_title()]
    lane = "env/eabc123-browser"
    assert entry_bound(meta, lane, facts("shop FIX"))  # titles compare as the environments do
    assert not entry_bound(meta, lane, facts("Blog"))
    assert not entry_bound(meta, lane, facts(TITLE, family="g-another-agent"))  # its title, another agent's computer
    assert not entry_bound(meta, lane, None)  # no environment known: nothing covered
    assert not entry_bound(meta, "biz/insta", None)  # an account lane: not bound at all

    await logins.bind(vault, titled.ref, logins.scope_of(None, "github.com/me/shop", False))
    meta = await vault.get_entry(titled.ref)
    assert entry_bound(meta, lane, facts("Blog", "github.com/me/shop", family="g-any"))  # a repository: any agent's
    assert not entry_bound(meta, lane, facts("Blog", "github.com/me/blog"))
    await logins.bind(vault, titled.ref, logins.scope_of(None, None, True))
    assert entry_bound(await vault.get_entry(titled.ref), lane, facts("Anything", family="g-any"))


def test_a_title_binding_names_the_one_agent_whose_computer_has_the_title():
    def env(title, family, agent="agent-1", id_="eabc123"):
        return SimpleNamespace(title=title, family=family, agent=agent, id=id_)

    def mgr(open_=(), saves=()):
        ids = {e.id: e for e in open_}
        return SimpleNamespace(store=SimpleNamespace(has=lambda i: i in ids, get=lambda i: ids[i],
                                                     open=lambda: list(open_)),
                               saves=lambda: SimpleNamespace(all=lambda: list(saves)))

    with pytest.raises(InvalidRequest, match="no computer is titled"):
        logins.title_scope(mgr(), TITLE)
    assert logins.title_scope(mgr([env("shop  FIX", "g1")]), TITLE) == {"title": TITLE, "family": "g1", "agent": "agent-1"}
    assert logins.title_scope(mgr(saves=[env(TITLE, "g2", "agent-2", "s1")]), TITLE)["family"] == "g2"  # a save counts
    two = mgr([env(TITLE, "g1"), env(TITLE, "g2", "agent-2", "edef456")])
    with pytest.raises(InvalidRequest, match="--title <id>"):
        logins.title_scope(two, TITLE)
    assert logins.title_scope(two, "edef456") == {"title": TITLE, "family": "g2", "agent": "agent-2"}


def test_a_binding_names_exactly_one_scope():
    with pytest.raises(InvalidRequest):
        logins.scope_of(None, None, False)
    with pytest.raises(InvalidRequest):
        logins.scope_of(TITLE, "github.com/me/shop", False)
    assert logins.scope_of("  Shop   fix ", None, False) == {"title": "Shop fix"}
    assert logins.scope_of(None, "https://github.com/me/shop.git", False) == {"repo": "github.com/me/shop"}


async def test_a_title_binding_follows_the_title_across_a_resume(node, vault):  # noqa: F811
    """A resume gives the environment a new id; its title, and with it the binding, stays."""
    node["rt"].register("vault", vault)
    mgr = node["mgr"]
    mgr.logins_install()
    first = mgr.get((await opened(node, title=TITLE))["id"])
    await logins.add(vault, "localhost", user=USER, password=PASSWORD, scope=logins.title_scope(mgr, TITLE))
    assert [x["name"] for x in await mgr.login_names(first)] == ["localhost"]
    other = mgr.get((await opened(node, title="Other work", session="chat-2"))["id"])
    assert await mgr.login_names(other) == []
    mgr.store.update(first.id, lambda e: setattr(e, "state", "closed"))
    assert vault.facts_for(first.browser_lane) is None  # a closed one covers nothing
    again = Environment(**{**first.model_dump(), "id": "e" + "z" * 10, "state": "ready"})  # as a resume makes it
    mgr.store.put(again)
    assert [x["name"] for x in await mgr.login_names(again)] == ["localhost"]


async def test_removing_takes_one_binding_or_all_and_a_made_login_leaves_with_its_last(vault):
    made = await logins.add(vault, "localhost", user=USER, password=PASSWORD, scope={"title": TITLE})
    await logins.bind(vault, made.ref, {"all": True})
    await logins.remove(vault, "localhost", {"all": True})
    assert [b.scopes for b in await logins.all_bound(vault)] == [({"title": TITLE},)]
    await logins.remove(vault, "localhost")
    assert await logins.all_bound(vault) == [] and await vault.list_entries() == []
    with pytest.raises(NotFound):
        await logins.remove(vault, "localhost")

    own = await vault.add_entry(name="work login", kind="password", domain="localhost", lanes=["biz/app"],
                                username=USER, secret=PASSWORD)
    await logins.bind(vault, "work login", {"repo": "github.com/me/shop"})
    assert (await vault.get_entry(own.id)).lanes == ["biz/app", "env/*"]
    await logins.remove(vault, "work login")
    kept = await vault.get_entry(own.id)  # bound with --entry: it stays in the vault, off environments
    assert kept.lanes == ["biz/app"] and "envs" not in kept.config


async def test_a_second_login_of_one_site_needs_a_name_of_its_own(vault):
    await logins.add(vault, "localhost", user=USER, password=PASSWORD, scope={"all": True})
    assert (await logins.existing(vault, "localhost", USER)).name == "localhost"
    with pytest.raises(InvalidRequest, match="--name"):
        await logins.existing(vault, "localhost", "someone-else@example.org")
    second = await logins.add(vault, "localhost", user="someone-else@example.org", password=PASSWORD,
                              scope={"all": True}, name="localhost admin")
    assert second.name == "localhost admin"


def test_no_tool_takes_a_login_binding():
    """Only the owner binds: the login tools take a lane and a login's name, nothing that widens a binding."""
    params = {name: set(inspect.signature(fn).parameters) - {"ctx"} for name, fn in
              (("login", vault_tools.login), ("vault_fill", vault_tools.vault_fill), ("vault_list", vault_tools.vault_list))}
    assert params == {"login": {"on", "entry"}, "vault_fill": {"on", "field", "entry"}, "vault_list": {"on"}}
    from agentos.environments.tools import environment_open

    assert not set(inspect.signature(environment_open).parameters) & {"login", "logins", "entry", "scope"}


# ------------------------------------------------------------------ the vault's decision on an environment's lane


def meta(**config) -> EntryMeta:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return EntryMeta(id="v1", name="localhost", kind="password", domain="localhost", lanes=["env/*"],
                     config=config, has_username=True, has_secret=True, created_at=now, updated_at=now)


def test_the_policy_refuses_a_login_on_an_environment_it_does_not_cover():
    entry = meta(envs={"scopes": [by_title()], "user": None, "url": None, "made": True})
    pw = site_form()[1]
    args = {"lane_id": "env/eabc123-browser", "sink": Sink.FORM_PASSWORD, "url": LOGIN_URL, "field": pw,
            "fields": site_form()}
    assert decide(entry, **args, lane_facts=facts(TITLE)).allowed
    old = meta(envs={"scopes": [{"title": TITLE}], "user": None, "url": None, "made": True})
    assert not decide(old, **args, lane_facts=facts(TITLE)).allowed  # bound before titles named an agent: none
    for wrong in (facts("Blog"), facts(TITLE, family="g-another-agent"), None):
        d = decide(entry, **args, lane_facts=wrong)
        assert not d.allowed and d.reason is Reason.LANE_NOT_BOUND
    unscoped = meta()  # bound to env/* some other way (agentos vault bind): every environment, as before
    assert decide(unscoped, **args, lane_facts=None).allowed
    text_field = fld("pass", SITE, type="text", name="password", form="f")  # a password goes only into a password input
    d = decide(entry, **{**args, "field": text_field, "fields": [site_form()[0], text_field]}, lane_facts=facts(TITLE))
    assert not d.allowed


# ------------------------------------------------------------------ the agent's login, through routing and the lease


@pytest.fixture
async def agent(node, vault, monkeypatch):  # noqa: F811
    """The node fixture with a vault, the typist and the login tools, called as `agentos connect`'s agent."""
    rt, mgr = node["rt"], node["mgr"]
    rt.register("vault", vault)
    mgr.logins_install()
    site = fixture_site()
    rt.register("typist", lambda page: site)
    opened_at: list[str] = []

    async def open_page(runtime, page, url):
        opened_at.append(url)
        page.url = url
        site.current = "login" if url.startswith(LOGIN_URL) else site.current

    monkeypatch.setattr(logins, "_open_page", open_page)
    _grant, token = node["policy"].issue(subject={"id": "agent-2"}, workspaces=["*"], ceiling=Effect.COMMIT,
                                        specials=panel_specials(["environments"]))
    specs = {s.name: s for s in vault_tools.tool_specs(rt)}
    calls = {name: wrap_tool(spec, rt) for name, spec in specs.items()}
    from agentos.environments.tools import tool_specs as env_specs

    calls.update({s.name: wrap_tool(s, rt) for s in env_specs(rt)})

    async def call(name, *, session="chat-1", **kw):
        with bind_request_token(token, session=session):
            return await calls[name](**kw)

    return SimpleNamespace(call=call, site=site, opened_at=opened_at, mgr=mgr, vault=vault, rt=rt, node=node,
                           token=token)


async def test_the_agents_login_opens_the_page_fills_it_and_signs_in_again_after_a_logout(agent):
    await logins.add(agent.vault, LOGIN_URL, user=USER, password=PASSWORD, scope={"all": True})
    out = await agent.call("environment_open", title=TITLE)
    assert out["logins"] == [{"name": "localhost", "site": "localhost", "totp": False}]
    assert "login(on=" in out["next"]
    on = out["on"]
    await logins.remove(agent.vault, "localhost")
    await logins.add(agent.vault, LOGIN_URL, user=USER, password=PASSWORD, scope=logins.title_scope(agent.mgr, TITLE))
    assert (await agent.call("environment", on=on))["logins"] == out["logins"]  # bound by its title now

    result = await agent.call("login", on=on)
    assert result["ok"] is True and result["step"] == "done"
    assert agent.opened_at == [LOGIN_URL]  # the node opened the login page: the tab was blank
    assert agent.site.typed_text("user") == [USER] and agent.site.typed_text("pass") == [PASSWORD]

    agent.site.current = "login"  # the site logged it out: the agent calls login again, and nothing asks
    again = await agent.call("login", on=on)
    assert again["ok"] is True and agent.opened_at == [LOGIN_URL]  # on the login page already: no navigation
    assert agent.site.typed_text("pass") == [PASSWORD, PASSWORD]

    agent.site.current = "home"  # on the site but not on its login page: the node opens it
    third = await agent.call("login", on=on)
    assert third["ok"] is True and agent.opened_at == [LOGIN_URL, LOGIN_URL]

    blob = json.dumps([out, result, again])
    assert PASSWORD not in blob and USER not in blob


async def test_a_login_bound_to_another_title_is_refused_even_by_name(agent):
    await logins.add(agent.vault, LOGIN_URL, user=USER, password=PASSWORD, scope=by_title("Another one"))
    on = (await agent.call("environment_open", title=TITLE))["on"]
    with pytest.raises(ToolCallError) as e:
        await agent.call("login", on=on)
    assert "not_found" in str(e.value) or "no login" in str(e.value)
    with pytest.raises(ToolCallError):
        await agent.call("login", on=on, entry="localhost")
    agent.site.current = "login"
    with pytest.raises(ToolCallError):  # on the page, by name: the vault's own check refuses it
        await agent.call("vault_fill", on=on, field="e1", entry="localhost")
    assert agent.site.typed == []


async def test_only_the_holders_handle_reaches_the_machine(agent):
    await logins.add(agent.vault, LOGIN_URL, user=USER, password=PASSWORD, scope={"all": True})
    out = await agent.call("environment_open", title=TITLE)
    for session, on in (("chat-2", out["id"]), ("chat-2", None), ("chat-2", f"env/{out['id']}-browser")):
        kw = {"on": on} if on else {}
        with pytest.raises(ToolCallError):
            await agent.call("login", session=session, **kw)
    assert agent.site.typed == []
    assert (await agent.call("login", on=out["on"], session="chat-2"))["ok"] is True  # the handle is the lease


async def test_several_logins_name_themselves_and_none_says_how_to_bind_one(agent):
    on = (await agent.call("environment_open", title=TITLE))["on"]
    with pytest.raises(ToolCallError, match="agentos environments login add"):
        await agent.call("login", on=on)
    await logins.add(agent.vault, LOGIN_URL, user=USER, password=PASSWORD, scope={"all": True})
    await logins.add(agent.vault, "https://example.com/login", user=USER, password=PASSWORD, scope={"all": True})
    with pytest.raises(ToolCallError, match="entry=<name>"):
        await agent.call("login", on=on)
    assert (await agent.call("login", on=on, entry="localhost"))["ok"] is True


async def test_no_value_in_logs_traces_results_or_replay_captions(agent, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    shots = Shots()
    lanes = agent.node["lanes"]
    monkeypatch.setattr(lanes, "frame_turn", shots.frame_turn, raising=False)
    monkeypatch.setattr(lanes, "person_in_control", shots.person_in_control, raising=False)
    monkeypatch.setattr(replay_mod, "SETTLE_S", 0)
    await logins.add(agent.vault, LOGIN_URL, user=USER, password=PASSWORD, scope={"all": True})
    out = await agent.call("environment_open", title=TITLE)
    result = await agent.call("login", on=out["on"])
    await settled(agent.mgr)
    env = agent.mgr.get(out["id"])
    calls = agent.mgr.replay_store().calls(env.id)
    frames = agent.mgr.replay_store().frames(env.id)
    assert any(c["tool"] == "login" for c in calls)
    assert [f["tool"] for f in frames if f["cause"] == "agent"] == ["login"]  # the page after it, as any page call
    traces = [t.model_dump(mode="json") if hasattr(t, "model_dump") else str(t)
              for t in agent.node["policy"].traces.query(limit=500)[0]]
    blob = json.dumps([result, calls, frames, traces, env.model_dump(mode="json")], default=str) + caplog.text
    assert PASSWORD not in blob and USER not in blob and out["on"] not in json.dumps(calls)


async def test_no_frame_is_taken_while_a_fill_runs(node, monkeypatch):  # noqa: F811
    shots = Shots()
    monkeypatch.setattr(node["lanes"], "frame_turn", shots.frame_turn, raising=False)
    mgr = node["mgr"]
    env = mgr.get((await opened(node))["id"])
    with mgr.filling(env.id):
        for cause in ("agent", "change", "person", "save"):
            assert await mgr.take_frame(env, cause) == "busy"
    assert shots.page.shots == []
    assert await mgr.take_frame(env, "agent") == "kept"

    def fill_starts():  # a fill that begins while a frame is shot: the frame goes
        mgr._track(env.id).filling += 1

    shots.page.color, shots.page.during = (0, 0, 0), fill_starts
    assert await mgr.take_frame(env, "agent") == "busy"


# ------------------------------------------------------------------ the owner signs it in now


async def test_the_owner_signs_an_environment_in_now_and_hears_when_the_person_is_needed(agent, monkeypatch):
    out = await agent.call("environment_open", title=TITLE)
    await logins.add(agent.vault, LOGIN_URL, user=USER, password=PASSWORD, scope=logins.title_scope(agent.mgr, TITLE))
    monkeypatch.setattr(agent.mgr, "_live_link", lambda env: {"browser": "http://127.0.0.1:1/view/x"})
    done = await agent.mgr.sign_in_now("shop fix", "localhost")
    assert done["ok"] is True and done["environment"]["id"] == out["id"] and done["login"]["name"] == "localhost"
    assert "links" not in done

    agent.site.pages["login"].on_enter = "captcha"
    agent.site.pages["captcha"] = Page(SITE + "/challenge", [], signal=Signal.CAPTCHA)
    agent.site.current = "login"
    stuck = await agent.mgr.sign_in_now(TITLE, "localhost")
    assert stuck["ok"] is False and stuck["needs"] == "captcha" and stuck["links"]["browser"]
    from agentos.environments.logins_cli import signed_in_words

    assert "CAPTCHA" in signed_in_words(stuck) and "Live view: http://127.0.0.1:1/view/x" in signed_in_words(stuck)
    assert "Signed in to localhost" in signed_in_words(done)
    with pytest.raises(NotFound, match="agentos environments resume"):
        await agent.mgr.sign_in_now("Nothing open", "localhost")
    with pytest.raises(NotFound, match="no login"):
        await agent.mgr.sign_in_now(TITLE, "example.com")
    assert PASSWORD not in json.dumps([done, stuck])


# ------------------------------------------------------------------ the CLI


@pytest.fixture
def cli(vault, monkeypatch):
    from agentos.environments import cli as env_cli
    from agentos.environments import logins_cli

    monkeypatch.setattr(logins_cli, "_vault", lambda: vault)
    shop = SimpleNamespace(id="eabc123", title=TITLE, family=FAMILY, agent="agent-1")  # the computer an agent has
    mgr = SimpleNamespace(store=SimpleNamespace(has=lambda i: False, open=lambda: [shop]),
                          saves=lambda: SimpleNamespace(all=list))
    monkeypatch.setattr(env_cli, "_managers", lambda: (mgr, None))
    return logins_cli


def run(cli, *args, input=None):
    from agentos.environments.resume_cli import app

    return CliRunner().invoke(app, ["login", *args], input=input)


def test_the_cli_reads_the_password_from_a_hidden_prompt_or_stdin_never_argv(cli, vault, monkeypatch):
    asked: list[str] = []

    def getpass(prompt):
        asked.append(prompt)
        return PASSWORD if prompt.startswith("Password") else "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"

    monkeypatch.setattr(cli.getpass, "getpass", getpass)
    r = run(cli, "add", LOGIN_URL, "--user", USER, "--title", TITLE, "--totp")
    assert r.exit_code == 0, r.output
    assert len(asked) == 2 and "Stored localhost for the environment titled \"Shop fix\"" in r.output
    assert PASSWORD not in r.output

    r = run(cli, "add", "example.com", "--user", USER, "--all", "--stdin", input=PASSWORD + "\n")
    assert r.exit_code == 0, r.output
    r = run(cli, "add", "localhost", "--user", USER, "--repo", "github.com/me/shop")  # the same login: bound too, no prompt
    assert r.exit_code == 0 and "Bound localhost" in r.output and len(asked) == 2

    listed = run(cli, "list", "--json")
    rows = {row["name"]: row for row in json.loads(listed.output)["logins"]}
    assert rows["localhost"]["totp"] is True and rows["localhost"]["user"] == USER
    assert rows["localhost"]["environments"] == ["the environment titled \"Shop fix\" of agent-1",
                                                 "environments of github.com/me/shop"]
    [local] = [b for b in cli.asyncio.run(logins.all_bound(vault)) if b.name == "localhost"]
    assert local.scopes[0]["family"] == FAMILY  # the agent whose computer has the title, recorded at the bind
    assert PASSWORD not in listed.output
    assert "Shop fix" in run(cli, "list").output

    r = run(cli, "remove", "localhost", "--title", TITLE)
    assert r.exit_code == 0 and "Removed localhost from the environment titled" in r.output
    r = run(cli, "remove", "example.com")
    assert r.exit_code == 0
    assert [b.name for b in cli.asyncio.run(logins.all_bound(vault))] == ["localhost"]

    for cmd in (cli.add, cli.remove, cli.list_cmd, cli.now):  # no option can carry a value
        assert not set(inspect.signature(cmd).parameters) & {"password", "secret", "value", "seed"}


def test_the_cli_refuses_an_empty_password_and_a_missing_scope(cli, monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "")
    r = run(cli, "add", "localhost", "--title", TITLE)
    assert r.exit_code == 1 and "empty" in r.output
    r = run(cli, "add", "localhost")
    assert r.exit_code == 1 and "--title" in r.output


def test_login_title_site_signs_in_now_through_agentosd(cli, monkeypatch):
    sent = []

    def post(path, body):
        sent.append((path, body))
        return {"ok": True, "step": "done", "needs": None, "environment": {"id": "eabc", "title": TITLE},
                "login": {"name": "localhost", "site": "localhost"}}

    monkeypatch.setattr(cli, "_post", post)
    r = run(cli, TITLE, "localhost")
    assert r.exit_code == 0, r.output
    assert sent == [("/api/environments/login", {"title": TITLE, "site": "localhost"})]
    assert "Signed in to localhost in \"Shop fix\" (eabc)." in r.output


async def test_the_sign_in_route_answers_the_owner_only_and_never_a_value(agent, tmp_path):
    import httpx

    from agentos.api import ApiSettings, build_api

    await agent.call("environment_open", title=TITLE)
    await logins.add(agent.vault, LOGIN_URL, user=USER, password=PASSWORD, scope=logins.title_scope(agent.mgr, TITLE))
    api = build_api(agent.rt, ApiSettings(lane_pump_seconds=0, panel_dir=tmp_path / "no-panel"))
    owner = agent.rt.get("config").mcp_token
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://localhost") as c:
        body = {"title": TITLE, "site": "localhost"}
        r = await c.post("/api/environments/login", json=body, headers={"Authorization": f"Bearer {agent.token}"})
        assert r.status_code in (401, 403), r.text  # an agent's grant: never
        assert agent.site.typed == []
        r = await c.post("/api/environments/login", json=body, headers={"Authorization": f"Bearer {owner}"})
        assert r.status_code == 403 and "presence_required" in r.text  # the bare node token: an agent may hold it
        assert agent.site.typed == []
        from agentos.commands.base import operator_presence

        person = operator_presence(agent.rt.get("config"))  # the node's own CLI: a one-time code in panel.db
        assert person
        r = await c.post("/api/environments/login", json=body, headers={"Authorization": f"Bearer {owner}", **person})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and PASSWORD not in r.text and USER not in r.text


def test_an_error_names_its_fix():
    e = NotFound(logins._none_words(SimpleNamespace(title=TITLE), [], None))
    assert isinstance(e, AgentosError) and "--title \"Shop fix\"" in e.message
