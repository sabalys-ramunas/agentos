"""Hetzner Cloud API client (https://docs.hetzner.cloud): only the calls agentos.vms makes.

The token is read from `$AGENTOS_HOME/secrets/hetzner.token` (0600) by the caller and passed in;
this module never logs, prints or stores it. Every error is a VmError with Hetzner's own code and
message, never the request headers.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from agentos.vms.models import VmError
from agentos.vms.provider import Server, ServerSpec

TIMEOUT_S = 30.0
PER_PAGE = 50


def _selector(labels: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def _id(value: str) -> int | str:
    return int(value) if str(value).isdigit() else value


def _server(d: dict) -> Server:
    ipv4 = ((d.get("public_net") or {}).get("ipv4") or {}).get("ip")
    return Server(id=str(d["id"]), name=d.get("name", ""), status=d.get("status", ""), ipv4=ipv4,
                  labels=dict(d.get("labels") or {}),
                  server_type=((d.get("server_type") or {}).get("name") or ""), created=d.get("created", ""))


class HetznerProvider:
    name = "hetzner"
    firewall = True

    def __init__(self, token: str, *, api_url: str = "https://api.hetzner.cloud/v1",
                 transport: httpx.AsyncBaseTransport | None = None):
        if not token or not token.strip():
            raise VmError("no Hetzner API token: run `agentos vm setup` and paste a read-write token")
        self._token = token.strip()
        self._api = api_url.rstrip("/")
        self._transport = transport

    def __repr__(self) -> str:  # never the token
        return f"HetznerProvider({self._api})"

    # ------------------------------------------------------------------ http
    async def _call(self, method: str, path: str, *, params: dict | None = None, body: Any = None,
                    ok_404: bool = False) -> dict | None:
        headers = {"Authorization": f"Bearer {self._token}", "User-Agent": "agentos-vms"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=self._transport) as c:
                r = await c.request(method, self._api + path, params=params, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise VmError(f"Hetzner API unreachable ({type(e).__name__})") from None
        if r.status_code == 404 and ok_404:
            return None
        if r.status_code >= 400:
            try:
                err = (r.json() or {}).get("error") or {}
            except ValueError:
                err = {}
            code = err.get("code") or f"http_{r.status_code}"
            msg = err.get("message") or r.reason_phrase
            if r.status_code == 401:
                msg = "the token was refused (revoked, or not a Hetzner Cloud API token); run `agentos vm setup` again"
            elif code == "forbidden":
                msg += " (a read-only token cannot create or delete servers)"
            raise VmError(f"Hetzner: {code}: {msg}", provider_code=code)
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    async def _paged(self, path: str, key: str, params: dict) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            data = await self._call("GET", path, params={**params, "page": page, "per_page": PER_PAGE}) or {}
            out += data.get(key) or []
            nxt = (((data.get("meta") or {}).get("pagination")) or {}).get("next_page")
            if not nxt or page >= 40:
                return out
            page = int(nxt)

    async def _wait_action(self, action: dict | None, timeout_s: float = 600.0) -> None:
        if not action or action.get("status") in (None, "success"):
            return
        aid = action["id"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            data = await self._call("GET", f"/actions/{aid}") or {}
            a = data.get("action") or {}
            if a.get("status") == "success":
                return
            if a.get("status") == "error":
                e = a.get("error") or {}
                raise VmError(f"Hetzner: {e.get('code', 'action_failed')}: {e.get('message', 'the action failed')}",
                              provider_code=e.get("code"))
            await asyncio.sleep(3)
        raise VmError(f"Hetzner: action {aid} did not finish in {int(timeout_s)} s")

    # ------------------------------------------------------------------ provider
    async def check(self) -> None:
        await self._call("GET", "/servers", params={"per_page": 1})

    async def _available(self) -> dict[int, set[str]] | None:
        """Server type id: the locations where it can be ordered right now (a datacenter there
        lists it as available). None when Hetzner does not say."""
        try:
            dcs = await self._paged("/datacenters", "datacenters", {})
        except VmError:
            return None
        out: dict[int, set[str]] = {}
        for dc in dcs:
            loc = (dc.get("location") or {}).get("name")
            for tid in ((dc.get("server_types") or {}).get("available") or []):
                out.setdefault(int(tid), set()).add(loc)
        return out or None

    async def server_types(self) -> list[dict]:
        types = await self._paged("/server_types", "server_types", {})
        available = await self._available()
        out = []
        for t in types:
            if t.get("deprecated") or (t.get("deprecation") or {}).get("unavailable_after"):
                continue
            gone = {loc.get("name") for loc in t.get("locations") or [] if loc.get("deprecation")}
            prices = {}
            for p in t.get("prices") or []:
                try:
                    if p.get("location") not in gone:
                        prices[p.get("location")] = float((p.get("price_hourly") or {}).get("gross"))
                except (TypeError, ValueError):
                    continue
            where = None if available is None else sorted(available.get(int(t.get("id") or 0), set()))
            out.append({"name": t.get("name"), "cores": t.get("cores"), "memory_gb": t.get("memory"),
                        "disk_gb": t.get("disk"), "cpu_type": t.get("cpu_type"), "architecture": t.get("architecture"),
                        "price_hourly_eur": prices, "available": where})
        return out

    async def price_hourly(self, server_type: str, location: str) -> float | None:
        data = await self._call("GET", "/server_types", params={"name": server_type}) or {}
        types = data.get("server_types") or []
        if not types:
            raise VmError(f"Hetzner has no server type {server_type!r}; see `agentos vm types`")
        for p in types[0].get("prices") or []:
            if p.get("location") == location:
                try:
                    return float((p.get("price_hourly") or {}).get("gross"))
                except (TypeError, ValueError):
                    return None
        raise VmError(f"server type {server_type} is not offered in {location}; see `agentos vm types`")

    async def create_server(self, spec: ServerSpec) -> Server:
        body: dict[str, Any] = {
            "name": spec.name, "server_type": spec.server_type, "image": _id(spec.image),
            "location": spec.location, "user_data": spec.user_data, "labels": spec.labels,
            "start_after_create": True,
            "public_net": {"enable_ipv4": True, "enable_ipv6": True},
        }
        if spec.ssh_key_id:
            body["ssh_keys"] = [_id(spec.ssh_key_id)]
        if spec.firewall_id:
            body["firewalls"] = [{"firewall": _id(spec.firewall_id)}]
        data = await self._call("POST", "/servers", body=body) or {}
        # data["root_password"] is null with an SSH key; it is never read or kept either way
        return _server(data["server"])

    async def get_server(self, server_id: str) -> Server | None:
        data = await self._call("GET", f"/servers/{server_id}", ok_404=True)
        return None if data is None else _server(data["server"])

    async def list_servers(self, labels: dict[str, str]) -> list[Server]:
        return [_server(d) for d in await self._paged("/servers", "servers", {"label_selector": _selector(labels)})]

    async def delete_server(self, server_id: str) -> None:
        await self._call("DELETE", f"/servers/{server_id}", ok_404=True)

    async def ensure_firewall(self, name: str, labels: dict[str, str], ssh_from: list[str]) -> str:
        rules = [
            {"direction": "in", "protocol": "tcp", "port": "22", "source_ips": ssh_from,
             "description": "SSH from the agentos node"},
            {"direction": "in", "protocol": "icmp", "source_ips": ssh_from, "description": "ping from the node"},
        ]
        data = await self._call("GET", "/firewalls", params={"name": name}) or {}
        found = data.get("firewalls") or []
        if found:
            fid = str(found[0]["id"])
            have = found[0].get("rules") or []
            if [(r.get("protocol"), r.get("port"), sorted(r.get("source_ips") or [])) for r in have] != \
                    [(r["protocol"], r.get("port"), sorted(r["source_ips"])) for r in rules]:
                res = await self._call("POST", f"/firewalls/{fid}/actions/set_rules", body={"rules": rules}) or {}
                for a in res.get("actions") or []:
                    await self._wait_action(a, timeout_s=120)
            return fid
        res = await self._call("POST", "/firewalls", body={"name": name, "labels": labels, "rules": rules}) or {}
        return str(res["firewall"]["id"])

    async def create_ssh_key(self, name: str, public_key: str, labels: dict[str, str]) -> str:
        data = await self._call("POST", "/ssh_keys", body={"name": name, "public_key": public_key,
                                                          "labels": labels}) or {}
        return str(data["ssh_key"]["id"])

    async def delete_ssh_key(self, key_id: str) -> None:
        await self._call("DELETE", f"/ssh_keys/{key_id}", ok_404=True)

    async def list_ssh_keys(self, labels: dict[str, str]) -> list[tuple[str, str]]:
        keys = await self._paged("/ssh_keys", "ssh_keys", {"label_selector": _selector(labels)})
        return [(str(k["id"]), k.get("name", "")) for k in keys]

    async def resolve_image(self, image: str) -> str:
        if not image.startswith("snapshot:"):
            return image
        name = image.split(":", 1)[1]
        data = await self._call("GET", "/images", params={"type": "snapshot", "sort": "created:desc",
                                                          "label_selector": f"agentos-image={name}"}) or {}
        imgs = [i for i in data.get("images") or [] if i.get("status") == "available"]
        if not imgs:
            raise VmError(f"no snapshot named {name}; build it with `agentos vm image build {name}`")
        return str(imgs[0]["id"])

    async def image_info(self, image: str) -> dict | None:
        """A snapshot's architecture and disk size (a server for it needs both); None for a
        system image such as ubuntu-24.04, which Hetzner has for every architecture."""
        if not image.startswith("snapshot:") and not image.isdigit():
            return None
        if image.isdigit():
            data = await self._call("GET", f"/images/{image}") or {}
            img = data.get("image") or {}
        else:
            img_id = await self.resolve_image(image)
            img = (await self._call("GET", f"/images/{img_id}") or {}).get("image") or {}
        size = img.get("disk_size")
        return {"architecture": img.get("architecture"), "disk_gb": float(size) if size is not None else None}

    async def create_snapshot(self, server_id: str, description: str, labels: dict[str, str]) -> str:
        data = await self._call("POST", f"/servers/{server_id}/actions/create_image",
                                body={"type": "snapshot", "description": description, "labels": labels}) or {}
        await self._wait_action(data.get("action"), timeout_s=1200)
        return str(data["image"]["id"])
