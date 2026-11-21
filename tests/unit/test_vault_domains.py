import pytest

from openajar.vault.domains import (
    display_host,
    host_of,
    is_loopback,
    normalize_host,
    parse_origin,
    registrable_domain,
)


@pytest.mark.parametrize("host,expected", [
    ("Instagram.COM", "instagram.com"),
    ("instagram.com.", "instagram.com"),
    ("ｉｎｓｔａｇｒａｍ.com", "instagram.com"),          # fullwidth maps like a browser does
    ("bücher.de", "xn--bcher-kva.de"),
    ("XN--BCHER-KVA.DE", "xn--bcher-kva.de"),
    ("instаgram.com", "xn--instgram-46g.com"),          # Cyrillic a: a different host
    ("127.0.0.1", "127.0.0.1"),
    ("[::1]", "::1"),
    ("r3---sn-abc.googlevideo.com", "r3---sn-abc.googlevideo.com"),
    ("under_score.example.com", "under_score.example.com"),
])
def test_normalize_host(host, expected):
    assert normalize_host(host) == expected


@pytest.mark.parametrize("bad", [
    "", " instagram.com", "insta gram.com", "instagram.com%2eevil.io", "user@instagram.com",
    "instagram.com/x", "instagram.com\\evil.io", "0x7f.1", "2130706433", "127.1", "1.2.3.04",
    "xn--zz.com", "-bad.com", "bad-.com", "a..b.com", "fe80::1%eth0", "x" * 300, "inst*gram.com",
])
def test_normalize_host_rejects(bad):
    with pytest.raises(ValueError):
        normalize_host(bad)


def test_ignorable_code_points_map_like_a_browser():
    # UTS46 (what browsers apply to hosts) drops a zero-width space, so the browser would
    # be on a.com; we compare against what the browser reports, so this is consistent.
    assert normalize_host("a\u200b.com") == "a.com"


@pytest.mark.parametrize("host,reg", [
    ("www.instagram.com", "instagram.com"),
    ("instagram.com.evil.io", "evil.io"),
    ("evilinstagram.com", "evilinstagram.com"),
    ("a.b.example.co.uk", "example.co.uk"),
    ("me.github.io", "me.github.io"),
    ("shop.me.github.io", "me.github.io"),
    ("bücher.de", "xn--bcher-kva.de"),
    ("localhost", "localhost"),
    ("127.0.0.1", "127.0.0.1"),
])
def test_registrable_domain(host, reg):
    assert registrable_domain(host) == reg


@pytest.mark.parametrize("host", ["com", "co.uk", "github.io", "unknowntld12", "a.unknowntld12"])
def test_public_suffixes_and_unknown_tlds_have_no_registrable_domain(host):
    assert registrable_domain(host) is None


def test_parse_origin_basics():
    o = parse_origin("https://WWW.Instagram.com/accounts/login/?next=/")
    assert (o.scheme, o.host, o.port) == ("https", "www.instagram.com", 443)
    assert str(o) == "https://www.instagram.com"
    assert str(parse_origin("http://127.0.0.1:8080/x")) == "http://127.0.0.1:8080"
    assert parse_origin("https://[::1]:8443/").host == "::1"
    assert parse_origin("https://instagram.com").port == 443


@pytest.mark.parametrize("bad", [
    "about:blank", "null", "data:text/html,hi", "javascript:alert(1)", "file:///etc/passwd",
    "https://instagram.com@evil.io/", "https://user:pw@instagram.com/", "https://evil.io\\@instagram.com",
    "https://instagram.com:99999/", "https://instagram.com:abc/", "https:///nohost", "https://ins tagram.com",
    "https://instagram.com\t.evil.io", "blob:https://instagram.com/uuid", "ws://instagram.com",
    "https://[::1", "",
])
def test_parse_origin_rejects(bad):
    with pytest.raises(ValueError):
        parse_origin(bad)


def test_loopback_and_display():
    assert is_loopback("localhost") and is_loopback("127.0.0.5") and is_loopback("::1")
    assert not is_loopback("localhost.evil.io") and not is_loopback("10.0.0.1")
    assert display_host("xn--bcher-kva.de") == "bücher.de"
    assert display_host("instagram.com") == "instagram.com"


def test_host_of_accepts_urls_and_bare_hosts():
    assert host_of("https://www.instagram.com/login") == "www.instagram.com"
    assert host_of("instagram.com/accounts") == "instagram.com"
    assert host_of("instagram.com:443") == "instagram.com"
