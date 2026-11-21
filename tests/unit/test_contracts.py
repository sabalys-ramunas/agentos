import pytest

from openajar.core.config import NodeConfig, load_config, save_config
from openajar.core.models import LaneSpec, ProxyConfig, parse_lane_id


def test_lane_id_roundtrip():
    assert parse_lane_id("personal/reddit") == ("personal", "reddit")
    spec = LaneSpec(id="biz-a/instagram")
    assert spec.workspace == "biz-a" and spec.name == "instagram"


@pytest.mark.parametrize("bad", ["Personal/reddit", "reddit", "a/b/c", "a/", "/b", "a b/c"])
def test_lane_id_rejects(bad):
    with pytest.raises(ValueError):
        parse_lane_id(bad)


def test_proxy_public_hides_credentials():
    p = ProxyConfig(label="lt1", host="1.2.3.4", port=12323, username="u", password="secret")
    assert "secret" not in repr(p)
    assert p.public() == {"label": "lt1", "host": "1.2.3.4", "port": 12323}
    assert p.server == "http://1.2.3.4:12323"


def test_config_roundtrip(tmp_path):
    cfg = NodeConfig(home=tmp_path, mcp_token="t0k", max_warm_lanes=2)
    path = save_config(cfg)
    assert oct(path.stat().st_mode)[-3:] == "600"
    again = load_config(tmp_path)
    assert again.mcp_token == "t0k" and again.max_warm_lanes == 2 and again.home == tmp_path
    assert again.profile_dir("personal/reddit") == tmp_path / "lanes/personal/reddit/profile"
