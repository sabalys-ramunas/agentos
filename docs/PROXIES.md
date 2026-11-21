# Setting up proxies and devices

Decide this before the first logins. A site remembers the computer and the address an account
first logged in from; changing either later is a new computer or a new place to the site, and
that is when it asks the account holder to prove who they are. openajar never changes a
logged-in lane's computer by itself.

## The plan in one paragraph

One **device** per company: one pinned fingerprint (the computer) plus one static ISP proxy (the
office's address), shared by every lane of that company, like one office PC with a browser
profile per account. Your personal accounts get a device and a proxy of their own. With four
companies plus personal, that is five devices and five proxies, not two proxies for 29 lanes
(fifteen computers on one address) and not 29 proxies (29 strangers who share a recovery email).

## What a device is

- A device is a name, a pinned fingerprint (`OPENAJAR_HOME/devices/<name>/fingerprint.json`) and one
  proxy. Every lane on it launches with that fingerprint and leaves through that proxy.
- Each lane keeps its own profile (cookies, logins, storage, history), its own browser process,
  its own display and live view, and its own systemd scope. Two lanes of one device can be warm at
  once, fully apart, and show the same computer: screen, fonts, GPU, timezone, locale, Firefox
  version and window placement.
- A lane on a device keeps a byte-for-byte copy of the device's pin as its own `fingerprint.json`,
  so whatever reads a lane's pin (its timezone for active hours, `openajar lane check`, a backup, an
  older openajar after a rollback) sees the device's.
- The Firefox version moves with Camoufox upgrades on the device, once, for all its lanes.

## Buying proxies

| Choose | Not | Why |
|---|---|---|
| Static ISP (a residential ISP's address, sold as "static residential" or "ISP") | Rotating residential, mobile pools, datacenter | An account should come from one address, every day, that looks like a home or office line. Rotating pools change the address; datacenter ranges are flagged on sight |
| Sticky: the same exit IP for months | "Sticky for 30 minutes" sessions | The self-check pins the exit and quarantines a lane whose exit moves (AUD-14) |
| The business's own country and city | A cheaper country | The pin's timezone, locale and location come from the exit on the first launch and never follow the proxy afterwards |
| HTTP proxy with username and password | SOCKS without auth, IP allowlists | Camoufox takes an HTTP proxy with credentials; an allowlist breaks when the node moves |
| One per company, one for personal accounts | One shared by everyone | Two devices on one exit look like an office the site has never seen together |
| A consumer ISP's ASN (check it with an IP lookup after you buy) | An ASN named after a hosting company | Sites score the network as much as the address |

After `openajar proxy add`, the first lane's self-check pins the proxy's exit; `openajar device show
<name>` and `openajar lane check <lane>` then say whether the proxy answers, whether it still exits
from the pinned address, and whether the exit is in the country and timezone the device's pin says.

## Making the devices

New company, no logins yet:

```
openajar proxy add biz-a 203.0.113.10:12323:user          # asks for the password
openajar device create biz-a-pc --proxy biz-a --note "Biz A office"
openajar lane create biz-a/instagram --device biz-a-pc
openajar lane create biz-a/facebook --device biz-a-pc
```

The first launch of any lane on the device pins it where the proxy exits; every other lane gets
the same pin.

A company whose account a site already knows (it has logged in on openajar):

```
openajar device create biz-a-pc --from-lane biz-a/instagram
openajar lane create biz-a/reddit --device biz-a-pc
```

The device takes that lane's fingerprint and proxy, so nothing changes for it, and the company's
other lanes join the computer the site already knows.

One device is one company's computer, behind one proxy. A lane of another workspace is refused
on it, and a device is refused on a proxy another device or a lane of its own already uses (make
it `--from-lane` that lane instead; when other lanes of their own share the proxy too, it needs
`--yes`). `none` is not a device name: it means "off the device".

Personal accounts:

```
openajar proxy add home 198.51.100.7:8080:user
openajar device create me-pc --proxy home
openajar lane create personal/instagram --device me-pc
```

## Moving a lane that has logged in

```
openajar lane set-device biz-a/facebook biz-a-pc           # shows what changes; nothing moves
openajar lane set-device biz-a/facebook biz-a-pc --yes     # the lane's site sees a new computer
openajar lane set-device biz-a/facebook none --yes         # back to a device of its own
```

A lane that has launched gets the device's fingerprint, which is a new computer to its site:
expect a re-login, a security check or a checkpoint, so do it once, on a quiet day, ready to answer
in the live view. The lane's earlier pin is kept as `fingerprint.json.before-device.bak` and comes
back when it leaves the device (with a warning when its timezone or country is not where the
proxy exits). A lane without such a file keeps the device's fingerprint as its own when it leaves:
the same computer to its site, now alone. A lane that has never launched moves without a warning.
In the panel: Settings, Browser devices, "Move a lane" shows the same plan and needs a tick.

While agentosd runs, these commands go through it. `openajar lane set-proxy` on a lane that has
launched, and `openajar proxy add` over a label that points elsewhere, need `--yes` too: both are a
new IP to the sites.

A device's pin is never drawn again on a guess. A device whose `fingerprint.json` is gone gets
it back from its lanes' copies; a torn pin, or copies that disagree, refuse the launch until the
right pin is back (`openajar lane audit` names them). At start, a lane copy newer than its device's
pin becomes the device's pin, and the older one is kept as `fingerprint.json.replaced.bak`, listed
by the audit until you remove it.

## Checking it

- `openajar device list` and `openajar device show <name>`: each device's proxy, its pin (OS,
  screen, Firefox version, timezone, locale, where it was pinned), its lanes, and its proxy's
  health. The panel shows the same under Settings, Browser devices.
- `openajar lane audit`: every pin's own consistency, and leaks across devices: two devices (or a
  device and a lane of its own) on one proxy or one exit IP, a device's lane on another proxy than
  the device's, lanes of two workspaces on one device, and a pin whose timezone, country or locale
  is not where its proxy now exits (from the lane check's daily lookup).
- `browsers_list` (MCP) and the panel's lane view show each lane's device.

## Later

- A second node for the hardest accounts (a home mini PC, AUD-33): a device lives on one node.
- Meta accounts are the strictest about new devices; keep their lanes on their company's device
  from the first login.
- Changing a device's proxy is not a command yet: make a new device in the same city and move the
  lanes (each move warns), or keep the proxy and ask the provider for the same exit.
