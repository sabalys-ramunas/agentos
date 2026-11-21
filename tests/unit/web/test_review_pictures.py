"""The pictures a review shows, as the app reads them (src/openajar/api/web/decisions.py `preview_image`).
A card's picture is the file the node holds for it, served under the approval's own route. Nothing is
drawn where the node holds none: the review then says what it says, with no broken picture."""

from __future__ import annotations

from openajar.api.web.decisions import preview_image

ORG = "/api/orgs/local"


def test_a_card_picture_is_the_file_it_attaches_under_the_org() -> None:
    item = {"id": "ap_1", "card": {"images": [{"name": "photo"}, {"name": "second"}]}}
    assert preview_image(item) == f"{ORG}/approvals/ap_1/files/photo"


def test_the_nodes_own_preview_picture_comes_first_when_it_has_one() -> None:
    item = {"id": "ap_2", "preview": {"text": "A caption", "image": "/api/approvals/ap_2/preview.png"},
            "card": {"images": [{"name": "photo"}]}}
    assert preview_image(item) == f"{ORG}/approvals/ap_2/preview.png"


def test_no_picture_is_drawn_where_the_node_has_none_or_the_name_is_not_a_file_name() -> None:
    assert preview_image({"id": "ap_3", "card": None}) is None
    assert preview_image({"id": "ap_3", "card": {"images": []}}) is None
    assert preview_image({"id": "ap_3", "card": {"images": [{"name": "../secret"}]}}) is None
    assert preview_image({"id": "ap_3", "card": {"images": ["photo"]}}) is None


def test_a_picture_the_node_does_not_have_answers_not_found(client) -> None:
    assert client.get(f"{ORG}/approvals/ap_missing/preview.png").status_code == 404
    assert client.get(f"{ORG}/approvals/ap_missing/files/photo").status_code == 404
