"""Install compiled connectors as runner flows (docs/design/connectors.md 4.4 step 5).

Each operation is saved through FlowService with

    saved_by = {"kind": "catalog", "id": "agentos", "signer": <key id or None>,
                "installed_by": <owner id>, "owner": <owner id>}

which the runner counts as owner-saved (runner.models.is_owner), so the catalog's
`"publishes": false` marks hold: the owner chose to install a reviewed bundle. Only the
owner installs; an operation that still needs a runner capability (PENDING_MARKER) is
refused; a lower version of an installed connector is refused unless forced (a rollback);
the same version with different content is refused (bump the version).

A learned connector (agentos.profiles.learn) is installed by the node itself with
`install_learned`, under its own key <site>-profile and with

    saved_by = {"kind": "learned", "id": "profile-learner", "site": <site>, "field": <field>}

which is NOT the owner's vouch (runner.models.is_owner): it holds exactly one commit operation
`<field>` and its read `<field>_read`, compiles with the strict lint and the commit gate, never
takes a tool name another connector serves, and a re-learn replaces it with a higher version.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentos.core.errors import InvalidRequest, PolicyDenied
from agentos.core.models import Principal
from agentos.runner.connectors.compile import (
    CompiledConnector,
    ConnectorInvalid,
    compile_connector,
    compile_path,
)
from agentos.runner.connectors.models import LEARNED_SUFFIX, version_tuple
from agentos.runner.connectors.signing import BundleRejected, verify_bundle
from agentos.runner.connectors.tools import tool_name
from agentos.runner.flowdoc import FlowVersion
from agentos.runner.flows import FlowService
from agentos.runner.names import CONNECTOR_PREFIX
from agentos.runner.owner import LEARNED_KIND, NodeInstall, is_owner

CATALOG_ID = "agentos"
LEARNER_ID = "profile-learner"
READ_SUFFIX = "_read"  # a learned field's read operation: <field>_read
UNLEARNABLE_FIELDS = frozenset({"name"})  # rate-locked for weeks on Facebook and Instagram


def agentos_version() -> tuple[int, ...]:
    from agentos import __version__

    return tuple(int(x) for x in __version__.split(".")[:3] if x.isdigit())


def _principal(by: Principal | Mapping[str, Any]) -> dict[str, Any]:
    p = by.model_dump() if isinstance(by, Principal) else dict(by)
    return {"kind": p.get("kind"), "id": p.get("id"), "owner": p.get("owner", "owner")}


class ConnectorService:
    def __init__(self, flows: FlowService, *, sites: Mapping[str, tuple[str, ...]] | None = None,
                 trusted: Mapping[str, str] | None = None):
        self.flows = flows
        self.sites = sites
        self.trusted = trusted  # None: signing.CATALOG_KEYS

    # ---- what is installed ----
    def installed(self) -> list[FlowVersion]:
        return [fv for fv in self.flows.list(CONNECTOR_PREFIX.rstrip(".")) if fv.definition.connector]

    def installed_version(self, connector: str) -> str | None:
        versions = [fv.definition.connector["version"] for fv in self.installed()
                    if fv.definition.connector.get("connector") == connector]
        return max(versions, key=version_tuple) if versions else None

    def describe(self) -> list[dict[str, Any]]:
        rows = []
        for fv in self.installed():
            m = fv.definition.connector or {}
            rows.append({"operation": f"{m.get('connector')}.{m.get('operation')}", "flow": fv.id,
                         "version": m.get("version"), "effect": str(fv.effect), "contract": m.get("contract"),
                         "implementation": fv.version, "signer": (fv.saved_by or {}).get("signer"),
                         "kind": (fv.saved_by or {}).get("kind")})
        return rows

    # ---- removing ----
    def remove(self, connector: str, *, by: Principal | Mapping[str, Any]) -> list[str]:
        """Delete every installed operation of `connector` (its flows, all versions); returns the
        flow ids. Only the owner removes; refused while a schedule runs one of its operations.
        Runs already made keep their records. Surfaces stay bound (a reinstall uses them again)."""
        owner = _principal(by)
        if not is_owner(owner):
            raise PolicyDenied("only the owner removes connectors (the panel or the CLI)")
        ops = [fv.id for fv in self.installed() if (fv.definition.connector or {}).get("connector") == connector]
        if not ops:
            raise InvalidRequest(f"no connector {connector!r} is installed on this node (agentos connector list)")
        using = sorted(s.id for s in self.flows.store.list_schedules() if s.flow_id in ops)
        if using:
            raise InvalidRequest(f"schedules {', '.join(using)} run {connector}; remove them first")
        for flow_id in ops:
            self.flows.delete(flow_id)
        return sorted(ops)

    # ---- installing ----
    def install(self, compiled: CompiledConnector, *, by: Principal | Mapping[str, Any],
                signer: str | None = None, force: bool = False) -> list[dict[str, Any]]:
        owner = self.check(compiled, by=by, force=force)
        return self._save(compiled, owner, signer)

    def check(self, compiled: CompiledConnector, *, by: Principal | Mapping[str, Any],
              force: bool = False) -> dict[str, Any]:
        """Raise unless `by` may install `compiled` now; return the owner principal."""
        owner = _principal(by)
        if not is_owner(owner):
            raise PolicyDenied("only the owner installs connectors (the panel or the CLI)")
        c = compiled.connector
        if any((fv.saved_by or {}).get("kind") == "local" and (fv.definition.connector or {}).get("connector") == c.connector
               for fv in self.installed()):
            raise PolicyDenied(f"{c.connector} is a site API on this node; remove it before installing a connector "
                               "with that key")
        pending = {name: op.pending for name, op in compiled.operations.items() if op.pending}
        if pending:
            parts = [f"{c.connector}.{name} needs {', '.join(caps)}" for name, caps in sorted(pending.items())]
            raise InvalidRequest("this node's runner cannot run every operation yet: " + "; ".join(parts))
        if c.requires.agentos:
            need = tuple(int(x) for x in c.requires.agentos.removeprefix(">=").split("."))
            if agentos_version() < need:
                raise InvalidRequest(f"{c.connector} {c.version} needs agentos {c.requires.agentos}")
        self._check_tools(compiled)
        current = self.installed_version(c.connector)
        if current is not None and not force:
            if version_tuple(c.version) < version_tuple(current):
                raise PolicyDenied(f"{c.connector} {current} is installed; {c.version} is a downgrade "
                                   "(force it to roll back)")
            if c.version == current:
                for name, op in compiled.operations.items():
                    have = self.flows.store.get_flow(op.flow_id)
                    same = have is not None and (have.definition.connector or {}).get("version") == c.version
                    if same and have.version != op.implementation:
                        raise PolicyDenied(f"{c.connector} {c.version} is installed with different content for "
                                           f"{name}; a changed connector gets a new version")
        return owner

    def _check_tools(self, compiled: CompiledConnector) -> None:
        c = compiled.connector
        tools = {tool_name(fv): fv.id for fv in self.installed() if fv.definition.connector.get("connector") != c.connector}
        for op in compiled.operations.values():
            if op.tool_name in tools:
                raise InvalidRequest(f"{op.tool_name} is already the tool of {tools[op.tool_name]}: two with one name "
                                     "would drop one of them")

    # ---- learned connectors (agentos.profiles.learn) ----
    def _of(self, connector: str) -> list[FlowVersion]:
        return [fv for fv in self.installed() if (fv.definition.connector or {}).get("connector") == connector]

    def _ledger(self, ledger: Any) -> Any:
        """The node's mark ledger: the one given, else the one beside the runner's database."""
        if ledger is not None:
            return ledger
        path = getattr(self.flows.store, "path", None)
        if path is None or str(path) == ":memory:":
            return None
        from agentos.runner.connectors.marks import MarkLedger

        return MarkLedger(Path(path).parent)

    def install_learned(self, doc: Mapping[str, Any], *, ledger: Any = None, **compile_kw: Any) -> list[str]:
        """Install a connector agentos.profiles.learn generated; returns its flow ids. Node code only
        (the profile learner): no owner principal, and saved as `learned`, which is not the owner's
        vouch. Refuses a key that is not <site>-profile, operations other than one commit `<field>`
        and its read `<field>_read`, the field `name`, a tool name another connector serves, and a
        key a catalog connector or site API holds. A re-learn replaces the installed learned version
        (the version goes past it, so the flows' history says which is which), and every install
        clears the replay marks of both operations (`ledger`, default: the one beside runner.db):
        a learned operation is verified again by its own replays, whether its steps changed or not."""
        if not isinstance(doc, Mapping):
            raise InvalidRequest("a learned connector is a JSON object")
        key = str(doc.get("connector") or "")
        if not key.endswith(LEARNED_SUFFIX):
            raise PolicyDenied(f"a learned connector's key is <site>{LEARNED_SUFFIX}, not {key!r}")
        have = self._of(key)
        if any((fv.saved_by or {}).get("kind") != LEARNED_KIND for fv in have):
            raise PolicyDenied(f"{key} is installed and was not learned; remove it before learning one with that key")
        doc = dict(doc)
        current = self.installed_version(key)
        if current is not None and version_tuple(str(doc.get("version") or "0.0.0")) <= version_tuple(current):
            major, minor, _ = version_tuple(current)
            doc["version"] = f"{major}.{minor + 1}.0"  # a re-learn: a new version past the installed one
        compiled = compile_connector(doc, sites=self.sites, commit_gate=True, learned=True, **compile_kw)
        c = compiled.connector
        commits = [n for n, op in c.operations.items() if str(op.effect) == "commit"]
        field = commits[0] if len(commits) == 1 else None
        if field is None or set(c.operations) != {field, f"{field}{READ_SUFFIX}"} or \
                str(c.operations[f"{field}{READ_SUFFIX}"].effect) != "read":
            raise PolicyDenied(f"a learned connector holds one commit operation <field> and its read <field>{READ_SUFFIX}, "
                               f"not {', '.join(sorted(c.operations))}")
        if field in UNLEARNABLE_FIELDS:
            raise PolicyDenied(f"{field} is never learned: sites rate-lock it for weeks")
        pending = sorted({p for op in compiled.operations.values() for p in op.pending})
        if pending:
            raise InvalidRequest(f"this node's runner cannot run {key} yet: it needs {', '.join(pending)}")
        self._check_tools(compiled)
        saved_by = {"kind": LEARNED_KIND, "id": LEARNER_ID, "site": c.site, "field": field}
        out = []
        for _, op in sorted(compiled.operations.items()):
            assert op.flow is not None
            fv, _ = self.flows.save(op.flow, by=NodeInstall(saved_by))
            out.append(fv.id)
        marks = self._ledger(ledger)
        if marks is not None:
            from agentos.runner.connectors.marks import BY_REPLAY

            for flow_id in out:
                if (marks.get(flow_id) or {}).get("by") == BY_REPLAY:
                    marks.remove(flow_id)
        return out

    def remove_learned(self, site: str, field: str, *, ledger: Any = None) -> list[str]:
        """Delete a learned field's two operations (all versions); returns their flow ids. With
        `ledger` (marks.MarkLedger, default: the one beside runner.db), their marks go too, so a
        later learn starts unverified.
        Refused for a connector that was not learned, or while a schedule runs one of them."""
        key = f"{site}{LEARNED_SUFFIX}"
        names = {field, f"{field}{READ_SUFFIX}"}
        ops = [fv for fv in self._of(key) if (fv.definition.connector or {}).get("operation") in names]
        if not ops:
            raise InvalidRequest(f"no learned {field} for {site} is installed on this node")
        if any((fv.saved_by or {}).get("kind") != LEARNED_KIND for fv in ops):
            raise PolicyDenied(f"{key} was not learned: the owner removes it (agentos connector remove)")
        ids = sorted(fv.id for fv in ops)
        using = sorted(s.id for s in self.flows.store.list_schedules() if s.flow_id in ids)
        if using:
            raise InvalidRequest(f"schedules {', '.join(using)} run {key}; remove them first")
        marks = self._ledger(ledger)
        for flow_id in ids:
            self.flows.delete(flow_id)
            if marks is not None:
                marks.remove(flow_id)
        return ids

    def _save(self, compiled: CompiledConnector, owner: dict[str, Any], signer: str | None) -> list[dict[str, Any]]:
        saved_by = {"kind": "catalog", "id": CATALOG_ID, "signer": signer, "installed_by": owner["id"],
                    "owner": owner["owner"]}
        out = []
        for name, op in sorted(compiled.operations.items()):
            assert op.flow is not None
            fv, created = self.flows.save(op.flow, by=NodeInstall(saved_by))
            out.append({"operation": op.dotted, "flow": fv.id, "version": fv.version, "contract": op.contract,
                        "effect": str(fv.effect), "created": created})
        return out

    def install_dir(self, path: Path | str, *, by: Principal | Mapping[str, Any], force: bool = False,
                    **compile_kw: Any) -> list[dict[str, Any]]:
        """Install from the repo directory (connectors/<site>/), unsigned (phase 1)."""
        compiled = compile_path(path, sites=self.sites, **compile_kw)
        return self.install(compiled, by=by, signer=None, force=force)

    def install_bundle(self, bundle: Any, *, by: Principal | Mapping[str, Any], force: bool = False,
                       **compile_kw: Any) -> list[dict[str, Any]]:
        """Verify a signed bundle, recompile every connector on this node, check its contract
        hashes against the signed index, then install them all (or none)."""
        payload, kid = verify_bundle(bundle, self.trusted)
        compiled: list[CompiledConnector] = []
        for doc, row in zip(payload["connectors"], payload["index"], strict=True):
            try:
                cc = compile_connector(doc, sites=self.sites, **compile_kw)
            except ConnectorInvalid as e:
                raise BundleRejected(f"a signed connector does not compile on this node: {e.message}") from e
            ops = (row or {}).get("operations") or {}
            if row.get("connector") != cc.key or row.get("version") != cc.connector.version or \
                    set(ops) != set(cc.operations):
                raise BundleRejected(f"the bundle index does not match connector {cc.key}")
            for name, op in cc.operations.items():
                if (ops.get(name) or {}).get("contract") != op.contract:
                    raise BundleRejected(f"{cc.key}.{name}: the contract hash does not match the signed index")
            compiled.append(cc)
        owners = [self.check(cc, by=by, force=force) for cc in compiled]  # all or none
        out = []
        for cc, owner in zip(compiled, owners, strict=True):
            out += self._save(cc, owner, kid)
        return out
