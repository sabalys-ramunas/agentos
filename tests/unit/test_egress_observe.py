"""Observing a lane's requests by shape (agentos.node.egress: request_shape, the observe file),
the way effect rules are checked against a throwaway account's real publish requests."""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

import pytest
from test_egress import COMMITTER, FakeRequest, make_guard

from agentos.core.errors import InvalidRequest
from agentos.node import egress
from agentos.node.egress import read_observe, request_shape, write_observe

SECRET = "my private caption 42"


def test_json_shape_has_the_operation_and_field_names_never_values():
    body = json.dumps({"operation": "CreatePost", "variables": {"input": {"title": SECRET, "body": {"text": SECRET},
                                                                           "subredditName": "test"}},
                       "csrf_token": "c0ffee"}).encode()
    shape = request_shape({"Content-Type": "application/json; charset=utf-8"}, body)
    assert shape["type"] == "application/json" and shape["op"] == "CreatePost"
    assert "variables.input.title" in shape["fields"] and "variables.input.body.text" in shape["fields"]
    assert SECRET not in json.dumps(shape) and "c0ffee" not in json.dumps(shape)


def test_form_shape_reads_meta_style_json_variables_and_the_operation_header():
    body = urlencode({"fb_api_req_friendly_name": "ComposerStoryCreateMutation",
                      "variables": json.dumps({"input": {"message": {"text": SECRET}, "attachments": [{"photo": {"id": "1"}}]}}),
                      "doc_id": "123"}).encode()
    shape = request_shape({"content-type": "application/x-www-form-urlencoded",
                           "X-FB-Friendly-Name": "ComposerStoryCreateMutation"}, body)
    assert shape["op"] == "ComposerStoryCreateMutation"
    assert shape["headers"] == {"x-fb-friendly-name": "ComposerStoryCreateMutation"}
    assert {"variables.input.message.text", "variables.input.attachments[].photo.id", "doc_id"} <= set(shape["fields"])
    assert SECRET not in json.dumps(shape)


def test_multipart_shape_lists_part_names_and_large_bodies_are_not_parsed():
    body = (b'--b\r\nContent-Disposition: form-data; name="caption"\r\n\r\n' + SECRET.encode()
            + b'\r\n--b\r\nContent-Disposition: form-data; name="upload_id"\r\n\r\n77\r\n--b--\r\n')
    shape = request_shape({"content-type": "multipart/form-data; boundary=b"}, body)
    assert shape["fields"] == ["caption", "upload_id"] and SECRET not in json.dumps(shape)
    big = request_shape({"content-type": "application/json"}, b'{"a": "' + b"x" * (egress.SHAPE_PARSE_MAX + 1) + b'"}')
    assert "fields" not in big


def test_names_that_are_not_identifiers_are_dropped():
    shape = request_shape({"content-type": "application/json"}, json.dumps({SECRET: 1, "ok_key": 2}).encode())
    assert shape["fields"] == ["ok_key"]


def test_observe_file_starts_expires_and_ends(tmp_path):
    f = tmp_path / egress.OBSERVE_NAME
    assert read_observe(f) is None
    until = write_observe(f, 30)
    assert until is not None and read_observe(f) == pytest.approx(until, abs=1)
    assert (f.stat().st_mode & 0o777) == 0o600
    assert read_observe(f, now=time.time() + 31 * 60) is None
    write_observe(f, None)
    assert not f.exists() and read_observe(f) is None
    with pytest.raises(InvalidRequest):
        write_observe(f, egress.OBSERVE_MAX_MINUTES + 1)


async def test_an_observed_lane_traces_every_non_get_by_shape(tmp_path):
    guard, ctx, events, clock = make_guard(tmp_path, driver=COMMITTER)
    body = json.dumps({"operation": "CreatePost", "variables": {"title": SECRET}}).encode()
    req = FakeRequest("https://www.instagram.com/svc/graphql?x=t0k3n", "POST", body=body,
                      headers={"content-type": "application/json"})
    ctx.emit("request", req)
    assert guard.trace.read() == []  # not observing: unknown requests are not traced
    write_observe(guard.observe_path, 10)
    clock.now += egress.SITES_TTL_S
    ctx.emit("request", req)
    ctx.emit("request", req)  # the same operation within a second: one row
    ctx.emit("request", FakeRequest("https://www.instagram.com/", "GET"))  # GETs never
    [row] = guard.trace.read()
    assert row["kind"] == "observed_request" and row["shape"]["op"] == "CreatePost"
    assert (row["host"], row["path"], row["method"]) == ("www.instagram.com", "/svc/graphql", "POST")
    text = (tmp_path / "trace.jsonl").read_text()
    assert SECRET not in text and "t0k3n" not in text
    assert events == [] and guard.blocks_since(0) == []


async def test_an_observed_commit_carries_its_shape(tmp_path):
    guard, ctx, _events, _clock = make_guard(tmp_path, driver=COMMITTER)
    write_observe(guard.observe_path, 10)
    body = b'--b\r\nContent-Disposition: form-data; name="caption"\r\n\r\nhi\r\n--b--\r\n'
    ctx.emit("request", FakeRequest("https://i.instagram.com/api/v1/media/configure/", "POST", body=body,
                                    headers={"content-type": "multipart/form-data; boundary=b"}))
    [row] = guard.trace.read()
    assert row["kind"] == "observed_commit" and row["shape"]["fields"] == ["caption"]
