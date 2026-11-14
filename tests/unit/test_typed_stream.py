"""The typed-text check behind the `untyped` read rules is a stream check (review of the 2026-11-10
fix wave, finding 3). The agent's words passed as a read when typed in chunks of 3 characters,
entered with press, split by markup, JSON-escaped, case-changed, with doubled spaces, or after a 30
minute lifetime. These started as the reviewer's proofs; they now assert each is held as unknown,
and that the site's background traffic with no typed text in it still goes as a read."""

import base64
import json
from urllib.parse import quote, urlencode

import pytest
from test_commit_gate_background import BACKGROUND, JSON, LI, META, TEXT, TYPED, envelope, route_one
from test_egress import COMMITTER, make_guard

from agentos.node.egress import TypedStream, normalise_text, typed_in
from agentos.node.egress.helpers import printable_key

PAGINATION = f"{LI}/flagship-web/rsc-action/actions/pagination"


def _pagination_with(text: str) -> bytes:
    return json.dumps({"pagerId": "feed", "clientArguments": {"payload": {"q": text}}}).encode()


def _raw_pagination(sent: str) -> bytes:
    return ('{"pagerId":"feed","clientArguments":{"payload":{"q":"' + sent + '"}}}').encode()


async def _held(guard, ctx, body: bytes, url: str = PAGINATION, headers=JSON) -> bool:
    route, gate = await route_one(guard, ctx, url, headers, body)
    if route.aborted:
        assert gate.seen and gate.seen[0].endswith(".unknown")
        return True
    assert route.continued == 1 and gate.seen == []
    return False


async def test_control_typed_whole_is_held(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.note_typed(TYPED)
    assert await _held(guard, ctx, _pagination_with(TYPED))


async def test_control_nothing_typed_goes_as_a_read(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    assert not await _held(guard, ctx, _pagination_with(TYPED))


async def test_typed_in_chunks_of_three_is_held(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    for i in range(0, len(TYPED), 3):  # browser_act type in 3-character chunks
        guard.note_typed(TYPED[i:i + 3])
    assert await _held(guard, ctx, _pagination_with(TYPED))


async def test_text_entered_with_press_is_held(tmp_path):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    for ch in TYPED:  # browser_act press "F", "i", "v", ... as tools.py passes them on
        guard.note_key("Space" if ch == " " else (f"Shift+{ch}" if ch.isupper() else ch))
    assert await _held(guard, ctx, _pagination_with(TYPED))


@pytest.mark.parametrize("sent", [
    "Five lessons from a year of <b>baking</b>",                    # rich text run split
    "Five lessons from a year of baking".replace("o", "\\u006f"),  # JSON \u escape of ASCII
    "Five lessons from a year of baking".replace("o", "\\\\u006f"),  # escaped twice
    "five lessons from a year of baking",                           # case change
    "FIVE LESSONS FROM A YEAR OF BAKING",
    "Five  lessons from a year of baking",                          # whitespace change
    "Five lessons from a year of baking".replace(" ", "\\n"),       # each word a line
    "Five&nbsp;lessons&nbsp;from a year of baking",                 # HTML entities
    quote("Five lessons from a year of baking"),                    # URL-escaped inside JSON
    "lessons from a year",                                           # a run of the text, not all of it
])
async def test_transformed_typed_text_is_held(tmp_path, sent):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.note_typed(TYPED)
    assert await _held(guard, ctx, _raw_pagination(sent))


async def test_after_thirty_minutes_typed_text_is_still_held(tmp_path):
    guard, ctx, _, clock = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.note_typed(TYPED)
    clock.now += 1801
    assert await _held(guard, ctx, _pagination_with(TYPED))
    clock.now += 24 * 3600
    assert await _held(guard, ctx, _pagination_with(TYPED.lower()))


@pytest.mark.parametrize("encode", [
    lambda t: base64.b64encode(t.encode()).decode(),
    lambda t: base64.urlsafe_b64encode(("xx" + t).encode()).decode().rstrip("="),
    lambda t: quote(base64.b64encode(json.dumps({"m": t}).encode()).decode(), safe=""),
])
async def test_typed_text_in_base64_is_held(tmp_path, encode):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.note_typed(TYPED)
    url, headers, _ = BACKGROUND["linkedin beacon (envelope)"]
    assert await _held(guard, ctx, envelope(payload=encode(TYPED)), url=url, headers=headers)


@pytest.mark.parametrize("name", sorted(BACKGROUND))
async def test_background_traffic_without_typed_text_still_goes_as_a_read(tmp_path, name):
    """The false-positive side: with the agent's text in the stream, the site's own background requests
    the real node recorded (none of which carry it) still go as reads."""
    url, headers, body = BACKGROUND[name]
    if TYPED.encode() in body:  # "reddit validation" carries the title on purpose: a known read op
        pytest.skip("carries the text by design")
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.note_typed(TYPED)
    guard.note_typed("Thanks, see you at the meetup next Tuesday")
    for ch in "hello":
        guard.note_key(ch)
    route, gate = await route_one(guard, ctx, url, headers, body)
    assert route.continued == 1 and not route.aborted and gate.seen == [], name


@pytest.mark.parametrize("body", [
    _pagination_with("Five lesso"),                            # shares fewer than ten letters in a row
    json.dumps({"q": "baking bread"}).encode(),
    urlencode({**META, "__req": "1f"}).encode(),
    envelope(payload=base64.b64encode(b"\x00\x01binary pegasus event 77").decode()),
    b"opaque sensor data 1f2e3d",
])
async def test_a_body_that_shares_too_little_goes_as_a_read(tmp_path, body):
    guard, ctx, _, _ = make_guard(tmp_path, sites=(), driver=COMMITTER)
    guard.note_typed(TYPED)
    headers = TEXT if not body.startswith(b"{") and b"=" not in body else JSON
    assert not await _held(guard, ctx, body, headers=headers)


def test_short_text_is_found_only_as_a_whole_value():
    assert typed_in(["hi"], json.dumps({"a": "hi"}).encode()) == "hi"
    assert typed_in(["hi"], b"x=1&c=hi") == "hi"
    assert typed_in(["hi"], json.dumps({"a": "this is high"}).encode()) is None


def test_normalising_folds_what_a_page_changes():
    want = "fivelessonsfromayearofbaking"
    for sent in ("Five lessons from a year of <i>baking</i>", "FIVE-LESSONS from a year of baking!",
                 "Five%20lessons%20from%20a%20year%20of%20baking", "Five \\u006cessons from a year of baking",
                 "Five&#32;lessons from a year of baking"):
        assert normalise_text(sent) == want, sent
    assert normalise_text("Žalgiris už vartų") == "žalgirisužvartų"


def test_the_stream_is_capped_and_never_shows_its_text():
    stream = TypedStream()
    for _ in range(5000):
        stream.note("lorem ipsum dolor sit amet ")
    stream.note(TYPED)
    assert stream.found_in(_pagination_with(TYPED)) is not None
    assert "lorem" not in repr(stream) and "Five" not in repr(stream)
    assert len(stream.pieces()) <= 20


@pytest.mark.parametrize(("key", "char"), [
    ("a", "a"), ("Shift+A", "A"), ("KeyQ", "Q"), ("Digit5", "5"), ("Space", " "), (" ", " "),
    ("Enter", None), ("Tab", None), ("Control+a", None), ("Backspace", None), ("ArrowLeft", None),
])
def test_which_presses_enter_text(key, char):
    assert printable_key(key) == char
