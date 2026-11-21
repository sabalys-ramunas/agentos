"""The node's ledger of canary marks: which connector operations a canary on this node proved.

    ledger = MarkLedger(home)                       # $OPENAJAR_HOME/connector-marks.json
    ledger.put("reddit.comment", proof="sha256:...", text="2026-11-06 reddit.com canary: ...")
    ledger.state("connector.reddit.comment", installed_definition)   # MarkState or None
    ledger.remove("reddit.comment")

A mark names the operation (site.op) and the proof hash (compile.proof_hash) of the
implementation the canary ran. It is honoured only while the installed operation's proof hash is
that one: any edit to its steps, locators, readbacks, preflights, commit request or pace makes it
stale (`verified_stale`), and the operation is shown as unverified again.

Provenance: marks live here, on the node, and nowhere else counts. A connector file or a signed
bundle may carry a `verified` text (and its hash); that is information for a reader, never a
mark, so a manifest from anywhere cannot make an operation run. Only the owner writes this file,
from a shell on the node (`openajar connector verify`): no MCP tool, API route or agent writes it.
The file is the owner's (mode 0600) and is written atomically.

A second provenance, `by: "replay"`, is written only by node code (`mark_replay`, called by the
profile learner after its model-free replays of a learned operation passed); no MCP tool, API route
or CLI writes it. It is honoured only for a learned connector (saved_by kind `learned`, keyed
<site>-profile): openajar.profiles.learner.ProfileLearner.state checks the installed flow's kind, and
`mark_replay` refuses any other key. Like an owner's mark it names the proof hash, so a learned
operation re-installed with other steps makes it stale.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openajar.runner.connectors.compile import ConnectorInvalid, proof_hash
from openajar.runner.connectors.models import LEARNED_SUFFIX, PROOF_HASH_RE
from openajar.runner.names import CONNECTOR_PREFIX

MARKS_FILE = "connector-marks.json"
VERIFIED_STALE = "verified_stale"
BY_OWNER, BY_REPLAY = "owner", "replay"  # who wrote a mark: the owner from a shell, or the node's replays
EVIDENCE_MAX = 4000  # bytes of a replay mark's evidence, as JSON


@dataclass(frozen=True)
class MarkState:
    """A ledger mark against what is installed: `text` the canary's words, `stale` when the
    installed operation is not the implementation the canary proved."""
    text: str
    proof: str
    stale: bool = False
    by: str = BY_OWNER  # owner, or replay (honoured for a learned operation only)

    @property
    def reason(self) -> str | None:
        return VERIFIED_STALE if self.stale else None

    def __str__(self) -> str:
        return "" if self.stale else self.text


def operation_key(name: str) -> str:
    """site.op from site.op or connector.site.op."""
    key = name.removeprefix(CONNECTOR_PREFIX)
    site, _, op = key.partition(".")
    if not site or not op or "." in op:
        raise ConnectorInvalid(f"{name!r} is not an operation: name it as site.op, like reddit.comment")
    return key


class MarkLedger:
    def __init__(self, home: Path | str):
        self.path = Path(home) / MARKS_FILE

    def _read(self) -> dict[str, Any]:
        try:
            doc = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}  # an unreadable ledger honours nothing (fail closed)
        marks = doc.get("marks") if isinstance(doc, dict) else None
        return marks if isinstance(marks, dict) else {}

    def _write(self, marks: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = json.dumps({"version": 1, "marks": marks}, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".connector-marks.")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(blob)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def all(self) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self._read().items() if isinstance(v, dict)}

    def get(self, name: str) -> dict[str, Any] | None:
        return self.all().get(operation_key(name))

    def put(self, name: str, *, proof: str, text: str, version: str | None = None) -> dict[str, Any]:
        key = operation_key(name)
        if not proof.startswith("sha256:"):
            raise ConnectorInvalid("a mark names the proof hash of the implementation the canary ran")
        return self._put(key, {"proof": proof, "text": text, "version": version, "by": BY_OWNER})

    def _put(self, key: str, entry: dict[str, Any]) -> dict[str, Any]:
        marks = self.all()
        marks[key] = {**entry, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        self._write(marks)
        return marks[key]

    def remove(self, name: str) -> bool:
        key = operation_key(name)
        marks = self.all()
        if key not in marks:
            return False
        del marks[key]
        self._write(marks)
        return True

    def state(self, name: str, definition: Mapping[str, Any] | None) -> MarkState | None:
        """The mark for operation `name` against its installed definition (FlowDefinition.canonical()),
        or None when there is none (or nothing is installed)."""
        if definition is None:
            return None
        mark = self.get(name)
        if not mark or not isinstance(mark.get("text"), str) or not isinstance(mark.get("proof"), str):
            return None
        by = mark.get("by") or BY_OWNER
        if by not in (BY_OWNER, BY_REPLAY):
            return None  # a provenance this node does not know honours nothing (fail closed)
        installed = proof_hash(definition)
        return MarkState(text=mark["text"], proof=mark["proof"], stale=mark["proof"] != installed, by=by)


def mark_replay(home_or_store: MarkLedger | Path | str, flow_id: str, proof_hash: str,
                evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Mark a learned operation verified by the node's own model-free replays (`by: "replay"`).
    Node code only (the profile learner): no MCP tool, API route or CLI calls it. `flow_id` is
    connector.<site>-profile.<field> (or site-profile.field); `proof_hash` the installed operation's
    (compile.proof_hash) the replays ran; `evidence` what they saw (run ids, the value read back),
    small and JSON. An owner's mark for the same implementation is kept as it is."""
    ledger = home_or_store if isinstance(home_or_store, MarkLedger) else MarkLedger(home_or_store)
    key = operation_key(flow_id)
    if not key.partition(".")[0].endswith(LEARNED_SUFFIX):
        raise ConnectorInvalid(f"{key}: a replay marks a learned operation only (connector <site>{LEARNED_SUFFIX}); "
                               "a catalog operation needs the owner's mark (openajar connector verify)")
    if not isinstance(proof_hash, str) or not PROOF_HASH_RE.match(proof_hash):
        raise ConnectorInvalid("a replay mark names the proof hash of the implementation the replays ran")
    if not isinstance(evidence, Mapping):
        raise ConnectorInvalid("a replay mark's evidence is an object")
    try:
        blob = json.dumps(dict(evidence), sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise ConnectorInvalid(f"a replay mark's evidence is JSON: {e}") from e
    if len(blob.encode()) > EVIDENCE_MAX:
        raise ConnectorInvalid(f"a replay mark's evidence is at most {EVIDENCE_MAX} bytes")
    have = ledger.get(key)
    if have and have.get("by", BY_OWNER) == BY_OWNER and have.get("proof") == proof_hash:
        return have
    day = datetime.now(UTC).date().isoformat()
    text = f"{day} {key.partition('.')[0].removesuffix(LEARNED_SUFFIX)} replay: the learned operation replayed " \
           "with no model and its value read back"
    return ledger._put(key, {"proof": proof_hash, "text": text, "version": evidence.get("version"),
                             "by": BY_REPLAY, "evidence": json.loads(blob)})
