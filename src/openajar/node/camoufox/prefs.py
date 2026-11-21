"""The Firefox prefs a lane launches with: the private-network guard (WebRTC through the proxy on a
proxied lane), the cache, steady chrome, the wheel notch and the Linux host's text rendering."""

from __future__ import annotations

from typing import Any

PRIVATE_NET_PAC = """// openajar network guard: pages in a lane cannot reach the node or its private networks.
// isInNet only ever sees dotted IPv4: Firefox's isInNet reads an IPv6 string as 0, which
// would match 0.0.0.0/8 and blackhole every IPv6-reachable site.
var BLOCK = "PROXY 127.0.0.1:9";
function isV4(s) { return /^[0-9]{1,3}(\\.[0-9]{1,3}){3}$/.test(s); }
function isV6(s) { return s.indexOf(":") >= 0; }
function privateV4(ip) {
  return isInNet(ip, "127.0.0.0", "255.0.0.0") || isInNet(ip, "10.0.0.0", "255.0.0.0") ||
         isInNet(ip, "172.16.0.0", "255.240.0.0") || isInNet(ip, "192.168.0.0", "255.255.0.0") ||
         isInNet(ip, "169.254.0.0", "255.255.0.0") || isInNet(ip, "100.64.0.0", "255.192.0.0") ||
         isInNet(ip, "0.0.0.0", "255.0.0.0");
}
function privateV6(ip) {
  var s = ip.replace(/^\\[|\\]$/g, "").toLowerCase();
  if (s == "::1" || s == "::") return true;
  if (/^fe[89ab]/.test(s) || /^f[cd]/.test(s)) return true;
  var m = s.match(/^::ffff:([0-9.]+)$/);
  return m ? (isV4(m[1]) && privateV4(m[1])) : false;
}
function classify(a) {
  if (isV6(a)) return privateV6(a) ? BLOCK : "DIRECT";
  if (isV4(a)) return privateV4(a) ? BLOCK : "DIRECT";
  return null;
}
function FindProxyForURL(url, host) {
  var h = host.toLowerCase();
  if (h == "localhost" || dnsDomainIs(h, ".localhost") || isPlainHostName(h)) return BLOCK;
  var literal = classify(h);
  if (literal) return literal;
  var ip = dnsResolve(h);
  if (!ip) return "DIRECT";
  return classify(ip) || "DIRECT";
}"""


# Firefox's smart sizing lets the disk cache of one profile grow to about 1 GB; on a small node
# with many lanes that fills the disk, and a failed write during a cookie flush can leave the
# profile logged out (AUD-23). 256 MB per lane is plenty for a cache.
# WebRTC on a proxied lane. Camoufox's proxy_only_if_behind_proxy keeps STUN behind the proxy only
# on documents loaded through it: an about:blank document, or a blank iframe any page can make,
# sends STUN over UDP from the node's own IP (measured, tests/node/test_lanecheck_node.py). proxy_only closes that. A page loaded through the proxy already behaved this way, so
# what a site sees on a normal page does not change.
WEBRTC_PROXY_PREFS: dict[str, Any] = {"media.peerconnection.ice.proxy_only": True}

CACHE_PREFS: dict[str, Any] = {
    "browser.cache.disk.smart_size.enabled": False,
    "browser.cache.disk.capacity": 262144,  # KB
}

# Browser chrome that would change the page's size from one launch to the next. On its second
# start a profile shows Firefox's one-time "Open previous tabs?" bar, 41 px tall, so that launch
# alone reports a smaller window.innerHeight. -1 turns the bar off (as Firefox's own remote agent
# does); a page cannot read the pref.
STEADY_CHROME_PREFS: dict[str, Any] = {
    "browser.startup.couldRestoreSession.count": -1,
}

# What a wheel notch scrolls. Camoufox's humanize re-snaps every wheel call to whole notches, so a
# page never sees the distance asked for, only notch-sized steps of it: at Firefox's own setting on
# this platform a notch is 48 px, a size no mouse of ours sends and one that reads as too small for
# a notched wheel. The multiplier sets the notch to 132 px, the size the owner's own mouse sends in
# this Firefox (measured from the human reference sessions: every wheel event is 132 px, in and
# out). A page now sees one mouse's step and the calls decide how many of them.
WHEEL_PREFS: dict[str, Any] = {"mousewheel.default.delta_multiplier_y": 275}

# Text a page measures on a Linux node. Firefox's GTK font fallback can measure the first use of a
# character that only a bundled font has before that font's character map is loaded, so canvas
# measureText of an emoji or a rare letter changed between launches (a macOS identity read 178 on
# 2 launches in 6, 181 on the rest). Camoufox turns async fallback off for Linux identities only,
# because a real Mac falls back through CoreText; on this host every identity falls back through
# GTK, so it is off for all of them (181 on 10 launches in 10).
LINUX_HOST_PREFS: dict[str, Any] = {
    "gfx.font_rendering.fallback.async": False,
}
