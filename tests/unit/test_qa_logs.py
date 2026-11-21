"""QA bug hunt: the error catalog and the structured logs.

Each xfail below asserts the CORRECT behaviour and fails today (strict: it turns into a failure
the day the bug is fixed, so the marker must be removed with the fix). Tests without a marker
pin behaviour that was checked and held.

Everything runs against the real modules with files under tmp_path, in-memory streams and
fakes. Nothing touches journald, the network or the user's log directory.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import logging
import re
from pathlib import Path

import pytest

from openajar.core import catalog, logs
from openajar.core.config import NodeConfig
from openajar.ops import logs as reader

log = logging.getLogger("openajar.test.qa_logs")


@pytest.fixture(autouse=True)
def root_logging():
    """setup() replaces the root handlers and captures warnings; put pytest's back afterwards."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level)
    logs.clear_scrubbers()
    logging.captureWarnings(False)


def capture_json(monkeypatch, *, journal: bool = False) -> io.StringIO:
    """Root logging as agentosd sets it up, JSON lines into a string."""
    monkeypatch.setenv("OPENAJAR_LOG_FORMAT", "json")
    stream = io.StringIO()
    logs.setup(None, stream=stream, journal=journal)
    return stream


def last_line(stream: io.StringIO) -> dict:
    return json.loads(stream.getvalue().strip().splitlines()[-1])


def daemon_scrubbers(tmp_path: Path) -> None:
    """The scrubbers agentosd registers at start (node token, proxies, policy secret shapes)."""
    from openajar.node.daemon import install_log_scrubbers

    install_log_scrubbers(NodeConfig(home=tmp_path, mcp_token="node-token-for-the-qa-logs-test", mcp_port=7400))


# ---- 1. secrets in log lines ----

def test_a_secret_cut_in_half_by_the_length_limit_is_still_masked(monkeypatch):
    secret = "Zq-vault-password-4f9a2c71-tail"
    stream = capture_json(monkeypatch)
    logs.add_scrubber(lambda text: text.replace(secret, "[redacted]"))  # what vault.scrub and the token scrubber do
    log.info("%s", "a" * (logs.MAX_MESSAGE - 12) + secret)
    msg = last_line(stream)["msg"]
    assert secret[:8] not in msg, msg[-60:]


LEAKY_TEXT = [
    ("x-api-key header dict", "request headers: {'x-api-key': 'k_91ab33', 'accept': '*/*'}", "k_91ab33"),
    ("x-api-key header line", "X-Api-Key: k_91ab33", "k_91ab33"),
    ("x-auth-token header", "X-Auth-Token: tk4471ab", "tk4471ab"),
    ("csrf token header", "X-Agentos-CSRF: csrf4471ab", "csrf4471ab"),
    ("env password", "child env: DB_PASSWORD=hunter2", "hunter2"),
    ("env token", "child env: GITHUB_TOKEN=tok_4471ab", "tok_4471ab"),
    ("env secret", "child env: STRIPE_SECRET=zz4471ab", "zz4471ab"),
    ("session in a url", "GET https://shop.example/a?sessionid=s83hd72k&page=2", "s83hd72k"),
    ("signature in a url", "GET https://s3.example/b/o?signature=ab12cd34&expires=60", "ab12cd34"),
    ("auth in a url", "GET https://api.example/v1/items?auth=au4471ab", "au4471ab"),
    ("url with an empty user name", "proxy check failed for http://:pw-8812xy@proxy.example:8080", "pw-8812xy"),
    ("url with only a token as user", "clone failed https://tok8812xy@git.example/repo.git", "tok8812xy"),
]


@pytest.mark.parametrize(("name", "text", "secret"), LEAKY_TEXT, ids=[c[0] for c in LEAKY_TEXT])
def test_the_daemons_scrubbers_mask_header_env_and_query_secrets(name, text, secret, tmp_path, monkeypatch):
    stream = capture_json(monkeypatch)
    daemon_scrubbers(tmp_path)
    log.warning("%s", text)
    assert secret not in last_line(stream)["msg"], name


class _Dialog:
    def __init__(self, page, message="Your session expired"):
        self.page, self.type, self.message, self.default_value = page, "alert", message, ""

    async def accept(self, *_args) -> None:
        return None

    async def dismiss(self) -> None:
        return None


class _Page:
    def __init__(self, context, url):
        self.context, self.url = context, url

    def on(self, *_args) -> None:
        return None

    def is_closed(self) -> bool:
        return False


class _Context:
    def __init__(self):
        self.pages, self.handlers = [], {}

    def on(self, event, callback) -> None:
        self.handlers.setdefault(event, []).append(callback)


async def test_a_dialog_on_a_page_with_a_session_in_its_url_does_not_log_the_session(tmp_path, monkeypatch):
    from openajar.node.interaction import DialogGuard

    stream = capture_json(monkeypatch)
    daemon_scrubbers(tmp_path)
    ctx = _Context()
    guard = DialogGuard(timeout=30).attach(ctx)
    page = _Page(ctx, "https://shop.example/account?sessionid=s83hd72k&auth=au4471ab")
    ctx.pages.append(page)
    ctx.handlers["dialog"][0](_Dialog(page))
    await guard.close()
    written = stream.getvalue()
    assert "dialog d1 (alert) opened on" in written
    assert "s83hd72k" not in written and "au4471ab" not in written, written


def test_an_unhandled_api_error_logs_the_exception_type_and_not_its_text(tmp_path, monkeypatch):
    from openajar.api.errors import error_response

    stream = capture_json(monkeypatch)
    daemon_scrubbers(tmp_path)
    try:
        int("Pw-8812xy")  # a handler parsing a field the caller sent
    except ValueError as exc:
        response = error_response(exc)
    assert response.status_code == 500
    written = stream.getvalue()
    assert "unhandled ValueError in API handler" in written
    assert "Pw-8812xy" not in written, written


def test_a_message_with_a_lone_surrogate_is_still_written_to_the_log_file(tmp_path):
    handlers = logs.setup(tmp_path, stream=io.StringIO(), journal=False)
    log.info("before")
    log.warning("page title was %s", "great sale \ud83d")  # a JS slice() through a surrogate pair
    log.info("after")
    for handler in handlers:
        handler.flush()
    written = (tmp_path / "logs" / logs.LOG_FILE).read_text(encoding="utf-8")
    assert "before" in written and "after" in written  # the neighbours are fine
    assert "page title was" in written, "the warning vanished from the file"


FORGERY = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d +(DEBUG|INFO|WARN|ERROR)\b")


@pytest.mark.parametrize("payload", [
    "denied: lane 'x\n2026-11-04 09:00:01 INFO  openajar.node [lane=personal/reddit grant=g_owner] grant g_9 revoked'",
    "denied: lane '\x1b[2J\x1b]0;pwned\x07'",
], ids=["newline", "ansi"])
def test_a_message_cannot_forge_a_log_line_or_repaint_the_terminal(payload):
    record = logging.LogRecord("openajar.mcp", logging.WARNING, __file__, 1, "%s", (payload,), None)
    record.created = 1762246800
    for out in (logs.HumanFormatter().format(record),
                logs.human_line({"ts": "2026-11-04T09:00:00.000Z", "level": "warn", "logger": "openajar.mcp",
                                 "msg": payload})):
        assert "\x1b" not in out, repr(out)
        assert not any(FORGERY.match(extra) for extra in out.splitlines()[1:]), out


def test_a_long_traceback_keeps_the_exception_message(tmp_path, monkeypatch):
    frames = [f"def f{i}(n):\n    return f{i + 1}(n)  # frame {i}, with a comment to lengthen the source line\n"
              for i in range(80)]
    module = tmp_path / "deep_module_with_a_long_name_that_pads_every_path_in_the_traceback.py"
    module.write_text("\n".join([*frames, "def f80(n):\n    raise RuntimeError('THE-FINAL-ERROR-MESSAGE')\n"]))
    spec = importlib.util.spec_from_file_location("qa_deep_module", module)
    deep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deep)
    stream = capture_json(monkeypatch)
    try:
        deep.f0(1)
    except RuntimeError:
        log.exception("tool failed")
    exc = last_line(stream)["exc"]
    assert "THE-FINAL-ERROR-MESSAGE" in exc, exc[-120:]


def test_a_journal_line_stays_under_the_journald_line_limit(monkeypatch):
    stream = capture_json(monkeypatch, journal=True)
    try:
        raise RuntimeError("\x01" * 9000)
    except RuntimeError:
        log.exception("%s", "\x01" * 9000)
    line = stream.getvalue().strip()
    assert len(line.encode("utf-8")) <= 48 * 1024, len(line.encode("utf-8"))


async def test_concurrent_requests_and_threads_keep_their_own_ids(monkeypatch):
    """Checked and sound: two requests interleaving on one loop, each with a worker thread."""
    stream = capture_json(monkeypatch)

    async def request(name: str, delay: float) -> None:
        with logs.log_context(request=f"rq_{name}", lane=f"ws-{name}/lane"):
            await asyncio.sleep(delay)
            logs.update(grant=f"g_{name}")
            await asyncio.to_thread(log.info, "worker of %s", name)
            await asyncio.sleep(0.01)
            log.info("done %s", name)

    await asyncio.gather(request("a", 0.02), request("b", 0.0))
    log.info("outside")
    lines = [json.loads(raw) for raw in stream.getvalue().splitlines()]
    for line in lines:
        if line["msg"].startswith(("worker of", "done")):
            name = line["msg"].rsplit(" ", 1)[1]
            assert (line["request"], line["workspace"], line["grant"]) == (f"rq_{name}", f"ws-{name}", f"g_{name}")
    assert "request" not in lines[-1] and "grant" not in lines[-1]


# ---- 2. `openajar logs` ----

def write_lines(home: Path, lines: list[dict], name: str = logs.LOG_FILE) -> None:
    folder = home / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / name).open("a") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def stamp(msg: str, **ids) -> dict:
    return {"ts": "2026-11-04T09:00:00.000Z", "level": "info", "logger": "openajar.node", "msg": msg, **ids}


def test_follow_shows_the_lines_written_just_before_a_rotation(tmp_path):
    write_lines(tmp_path, [stamp("a")])
    current = tmp_path / "logs" / logs.LOG_FILE
    steps = iter([lambda: (write_lines(tmp_path, [stamp("b-written-before-the-rotation")]),
                           current.rename(current.with_name(f"{logs.LOG_FILE}.1")),
                           write_lines(tmp_path, [stamp("c-after")]))])
    done = []

    def sleep(_seconds):
        step = next(steps, None)
        if step is None:
            done.append(True)
        else:
            step()

    got = [line["msg"] for line in reader.file_lines(tmp_path, follow=True, sleep=sleep, stop=lambda: bool(done))]
    assert got == ["a", "b-written-before-the-rotation", "c-after"], got


def test_a_lane_glob_never_matches_lines_without_a_lane():
    foreign = reader.from_journal({"MESSAGE": "Started agentosd.service", "PRIORITY": "6",
                                   "__REALTIME_TIMESTAMP": "1762246800000000", "SYSLOG_IDENTIFIER": "systemd"})
    assert not reader.Query(lane="*").matches(foreign)
    assert not reader.Query(lane="*").matches(stamp("no lane on this one"))


@pytest.mark.parametrize(("level", "shown_from"), [("ERROR", "error"), ("Error", "error"), ("WARNING", "warn")])
def test_the_level_filter_ignores_the_case_of_the_stored_level(level, shown_from):
    line = {"ts": "2026-11-04T09:00:00.000Z", "level": level, "msg": "browser crashed"}  # a JSON line of a child
    assert reader.Query(level=shown_from).matches(line)


def test_a_since_value_that_cannot_be_read_says_so():
    with pytest.raises(ValueError, match="cannot read --since"):
        reader.parse_since("\u00b2m")


def test_the_file_reader_survives_noise_and_keeps_the_good_lines(tmp_path):
    """Checked and sound: bad UTF-8, JSON that is not an object, a truncated last line, an empty file."""
    folder = tmp_path / "logs"
    folder.mkdir()
    good = json.dumps(stamp("ok", level="error"))
    (folder / logs.LOG_FILE).write_bytes(b'\xff\xfe{"msg":"bad utf8"}\n123\nnull\n"str"\n[1,2]\n' + good.encode()
                                         + b'\n{"msg":"trunc","ts":"2026-11-04T09:0')
    msgs = [line["msg"] for line in reader.file_lines(tmp_path, follow=False)]
    assert "ok" in msgs and any(m.startswith("{\"msg\":\"trunc\"") for m in msgs)
    (folder / logs.LOG_FILE).write_bytes(b"")
    assert list(reader.file_lines(tmp_path, follow=False)) == []
    query = reader.Query(level="warn", since=1.0, lane="*")
    for odd in ({"msg": None}, {"msg": "x", "ts": 123, "level": 30}, {"msg": "x", "lane": 5, "ts": ["a"]}):
        query.matches(odd)
        logs.human_line(odd)


# ---- 3. the error catalog ----

def test_no_catalog_line_gives_the_classic_unsafe_advice():
    """Checked and sound: every agent, fix and reason line against a list of things that must never be advised."""
    unsafe = re.compile(r"sudo|chmod 7|rm -rf|--force|disable the|turn off the|token in the url|in the url"
                        r"|cookie file|copy the cookie|other account|another account|lock file|--no-verify"
                        r"|skip the check|bypass", re.IGNORECASE)
    hits = []
    for code, entry in catalog.entries().items():
        for label, text in (("agent", entry.agent), ("fix", entry.fix), *entry.reasons.items()):
            if unsafe.search(text):
                hits.append((code, label, text))
    assert hits == []


def test_the_retry_after_for_a_memory_write_cap_is_a_wait_that_works():
    from openajar.memory.errors import RateLimited
    from openajar.memory.models import Limits
    from openajar.memory.service import _Rates

    now = [1000.0]
    rates = _Rates(lambda: now[0])
    limits = Limits(writes_per_day=2)
    rates.record("family", False)
    rates.record("family", False)
    with pytest.raises(RateLimited) as caught:
        rates.check("family", False, limits)
    told = [line for line in catalog.mcp_lines(caught.value.code, caught.value.message)
            if line.startswith("retry_after: ")]
    if not told:
        return  # no promise, no lie
    now[0] += int(told[0].split(": ")[1]) + 1
    try:
        rates.check("family", False, limits)
        blocked = False
    except RateLimited:
        blocked = True
    assert not blocked, f"the catalog said {told[0]!r} and the next write was refused again"


def test_a_refused_code_does_not_send_the_agent_to_another_grant():
    lines = catalog.mcp_lines("vault_code_refused", "A code for this login must come from the owner or another "
                                                    "approver, not from the grant that asked for it.")
    assert "another grant" not in " ".join(lines).lower(), lines


def test_an_out_of_scope_denial_does_not_suggest_another_lane():
    lines = catalog.mcp_lines("denied", "out_of_scope: a memory call names one workspace, not a pattern")
    assert "use a lane" not in lines[0].lower(), lines[0]


def test_an_engine_error_does_not_ask_for_a_blind_repeat():
    lines = catalog.mcp_lines("engine_error", "Camoufox crashed while the page was submitting")
    step = lines[0].lower()
    assert "check" in step or "read" in step or "snapshot" in step, lines  # as deadline_exceeded does


def test_problems_checks_the_reason_texts_too():
    entry = catalog.Entry(code="x", meaning="m", agent="a", fix="f", reasons={"expired": "Stop \u2014 and ask", "gone": ""})
    found = catalog.Scan(by_code={"x": ["openajar.x.X"]}, failed=[])
    found_problems = catalog.problems(found, {"x": entry})
    assert any("em dash" in p for p in found_problems), found_problems
    assert any("empty" in p for p in found_problems), found_problems
