"""`file` streams: a lane's screenshot, an approval's preview and a run's proof, and nothing else."""

import asyncio
from types import SimpleNamespace

import pytest
from channel_fake_controller import NODE_ID, FakeController, node_pubkey, node_runtime, start, stop
from fakes import PNG_1X1

from agentos.core.errors import NotFound
from agentos.core.models import Principal
from agentos.node.channel import bridge, wire
from agentos.node.channel.files import proof_path

PHOTO = b"\x89PNG\r\n\x1a\n a photo for the post"


@pytest.fixture
async def node(tmp_path):
    home = tmp_path / "node"
    async with FakeController(NODE_ID, node_pubkey(home)) as fake:
        runtime = await node_runtime(home, fake, lanes=("personal/reddit", "biz-a/social"))
        await start(runtime)
        peer = await fake.peer()
        await asyncio.wait_for(runtime.channel.welcomed.wait(), 15)  # the fake sees the peer before the node is in
        yield runtime, peer, home
        await stop(runtime)


async def read_file(peer, path, lane=None):
    """(bytes, None) once the stream ends with eof, or (None, error body)."""
    stream_id = wire.ulid()
    sent = await peer.send("stream_open", {"stream_id": stream_id, "kind": "file", "lane": lane,
                                           "params": {"path": path}})
    reply = await peer.reply_to(sent.id)
    if reply.type == "error":
        assert reply.body["stream_id"] == stream_id
        return None, reply.body
    assert reply.type == "stream_ready"
    await peer.recv("stream_close", where=lambda e: e.body["stream_id"] == stream_id)
    data = b""
    while not peer.binary.empty():
        sid, chunk = wire.parse_stream_frame(peer.binary.get_nowait())
        assert sid == stream_id
        data += chunk
    closed = [e for e in peer.seen if e.type == "stream_close" and e.body["stream_id"] == stream_id]
    assert closed[0].body["reason"] == "eof"
    return data, None


def approval_with_photo(runtime, lane):
    store = runtime.get("approvals")
    store.card_files = lambda ap, param: (PHOTO, "image/png") if param == "photo" else (_ for _ in ()).throw(
        NotFound("no such image"))
    card = {"type": "post", "through": lane, "text": "Hello", "images": [{"name": "photo", "mime": "image/png"}],
            "params": [{"name": "photo", "file": True, "image": True, "match": "sha256:" + "0" * 64}]}
    return store.create_action(effect="commit", tool="post_note", lane=lane, domain=None, summary="Post a photo",
                               content_hash="sha256:" + "1" * 64, preview="Hello", requester=Principal(id="agent-1"),
                               grant_id=None, grant_path=None, reason=None, card=card)


async def test_a_warm_lanes_screenshot_streams_and_a_cold_one_is_refused(node):
    runtime, peer, _ = node
    data, error = await read_file(peer, "screenshot.png", "personal/reddit")
    assert data is None and error["code"] == "lane_cold"
    await runtime.lanes.open("personal/reddit")
    data, error = await read_file(peer, "screenshot.png", "personal/reddit")
    assert error is None and data == PNG_1X1
    _, error = await read_file(peer, "screenshot.png")
    assert error["code"] == "invalid"  # a screenshot names its lane
    _, error = await read_file(peer, "screenshot.png", "personal/nope")
    assert error["code"] == "lane_not_found"


async def test_an_approvals_preview_is_its_cards_image_and_only_on_its_own_lane(node):
    runtime, peer, _ = node
    approval = approval_with_photo(runtime, "biz-a/social")
    data, error = await read_file(peer, f"approvals/{approval.id}/preview.png", "biz-a/social")
    assert error is None and data == PHOTO
    data, error = await read_file(peer, f"approvals/{approval.id}/preview.png")
    assert data == PHOTO  # the org's controller may ask without a lane
    # a stream for another workspace's lane does not see this approval
    _, error = await read_file(peer, f"approvals/{approval.id}/preview.png", "personal/reddit")
    assert error["code"] == "not_found"
    _, error = await read_file(peer, "approvals/ap_nosuchapproval/preview.png")
    assert error["code"] == "not_found"
    plain = runtime.get("approvals").create_action(
        effect="commit", tool="post_note", lane="biz-a/social", domain=None, summary="Text only",
        content_hash="sha256:" + "2" * 64, preview="Hi", requester=Principal(id="agent-1"), grant_id=None,
        grant_path=None, reason=None)
    _, error = await read_file(peer, f"approvals/{plain.id}/preview.png")
    assert error["code"] == "not_found"


async def test_an_approval_with_an_image_says_so_in_its_request(node):
    runtime, peer, _ = node
    approval = approval_with_photo(runtime, "biz-a/social")
    request = await peer.recv("approval_request", where=lambda e: e.body["approval"]["id"] == approval.id)
    assert request.body["approval"]["preview"] == {"text": "Hello",
                                                    "image": f"approvals/{approval.id}/preview.png"}
    assert bridge.approval_payload({"id": "ap_x", "preview": None, "card": None})["preview"] is None


async def test_a_runs_proof_is_read_only_from_the_runners_shots_folder(node, tmp_path):
    runtime, peer, home = node
    shot = home / "runner" / "shots" / "run_abc123" / "step-final-a1.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(PHOTO)
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"not for the controller")
    runs = {
        "run_abc123": SimpleNamespace(lane="biz-a/social", surface=None, error=None,
                                      proof={"screenshot": "runner/shots/run_abc123/step-final-a1.png"}),
        "run_escape": SimpleNamespace(lane="biz-a/social", surface=None, error=None,
                                      proof={"screenshot": "runner/shots/../../../secret.png"}),
        "run_absolute": SimpleNamespace(lane="biz-a/social", surface=None, error=None,
                                        proof={"screenshot": str(outside)}),
        "run_noshot": SimpleNamespace(lane="biz-a/social", surface=None, error=None, proof={"screenshot": None}),
    }

    def get(run_id):
        if run_id not in runs:
            raise NotFound(run_id)
        return runs[run_id]

    runtime.register("runner", SimpleNamespace(get=get))
    data, error = await read_file(peer, "runs/run_abc123/proof.png", "biz-a/social")
    assert error is None and data == PHOTO
    _, error = await read_file(peer, "runs/run_abc123/proof.png", "personal/reddit")
    assert error["code"] == "not_found"
    for run_id in ("run_escape", "run_absolute", "run_noshot", "run_missing"):
        _, error = await read_file(peer, f"runs/{run_id}/proof.png")
        assert error["code"] == "not_found", run_id
    assert proof_path(home, None, {"screenshot": "runner/shots/run_abc123/step-final-a1.png"}) == shot.resolve()


@pytest.mark.parametrize("path", [
    "../secret.png", "/etc/passwd", "approvals/../../node.key/preview.png", "runs/run_abc/../proof.png",
    "approvals/ap_x/preview.png/..", "secrets/node.key", "screenshot.png\x00", "approvals/ap_x/other.png",
    "runs/run_x/proof.png ", "logs/agentosd.jsonl", "", 7, None,
])
async def test_no_path_outside_the_three_patterns_is_served(node, path):
    _runtime, peer, _ = node
    _, error = await read_file(peer, path, "personal/reddit")
    assert error["code"] == "invalid"


async def test_a_file_streams_bytes_respect_the_controllers_credit(node, monkeypatch):
    runtime, peer, _ = node
    big = b"\x89PNG" + b"x" * (600 * 1024)
    await runtime.lanes.open("personal/reddit")

    async def screenshot(page):
        return big

    monkeypatch.setattr(runtime.interaction, "screenshot", screenshot)
    stream_id = wire.ulid()
    sent = await peer.send("stream_open", {"stream_id": stream_id, "kind": "file", "lane": "personal/reddit",
                                           "params": {"path": "screenshot.png"}})
    assert (await peer.reply_to(sent.id)).type == "stream_ready"
    await asyncio.sleep(0.3)
    got = 0
    while not peer.binary.empty():
        got += len(wire.parse_stream_frame(peer.binary.get_nowait())[1])
    assert got == wire.INITIAL_CREDIT
    await peer.send("stream_credit", {"stream_id": stream_id, "bytes": 1 << 20})
    await peer.recv("stream_close", where=lambda e: e.body["stream_id"] == stream_id)
    while not peer.binary.empty():
        got += len(wire.parse_stream_frame(peer.binary.get_nowait())[1])
    assert got == len(big)


def test_a_proof_is_read_up_to_the_cap_and_no_further(tmp_path, monkeypatch):
    from pathlib import Path

    from agentos.core.errors import InvalidRequest
    from agentos.node.channel import files

    shot = tmp_path / "runner" / "shots" / "run_abc" / "final.png"
    shot.parent.mkdir(parents=True)
    run = SimpleNamespace(lane="biz-a/social", surface=None, error=None,
                          proof={"screenshot": "runner/shots/run_abc/final.png"})
    runtime = SimpleNamespace(get=lambda name: SimpleNamespace(get=lambda run_id: run))
    monkeypatch.setattr(files, "MAX_FILE_BYTES", len(PHOTO))
    # a file that grows after a size check is still read only to the cap: no whole-file read
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("read the whole file"))
    shot.write_bytes(PHOTO)
    assert files._run_proof(runtime, tmp_path, None, "run_abc") == PHOTO
    shot.write_bytes(PHOTO + b"!")
    with pytest.raises(InvalidRequest):
        files._run_proof(runtime, tmp_path, None, "run_abc")
