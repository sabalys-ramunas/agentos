"""Reading an export back: decrypt, check every member against the manifest, stage the profiles,
and load the records (ExportContents), refusing a file that is damaged, altered or not an export."""

from __future__ import annotations

import hashlib
import json
import os
import secrets as pysecrets
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from openajar import __version__
from openajar.core import age
from openajar.core.errors import InvalidRequest
from openajar.core.models import LaneSpec
from openajar.workspaces.models import LANE_NAME_RE, check_workspace_id
from openajar.workspaces.transfer.format import (
    AGENDA_MEMBER,
    DATA,
    FORMAT,
    JSON_MEMBERS,
    MAX_JSON,
    MEMORY_MEMBER,
    META,
    READ_VERSIONS,
    BadExport,
    ExportError,
    memory_counts,
)


@dataclass
class ExportContents:
    """A verified export: its JSON in memory, its profiles in `staging` (lanes/<name>/...)."""
    header: dict[str, Any]
    manifest: dict[str, Any]
    workspace: dict[str, Any]
    vault: list[dict[str, Any]]
    flows: list[dict[str, Any]]
    schedules: list[dict[str, Any]]
    grants: list[dict[str, Any]]
    proxies: list[dict[str, Any]]
    staging: Path
    files: int
    bytes: int
    memory: list[dict[str, Any]] = field(default_factory=list)   # format 2; empty for format 1
    agenda: list[dict[str, Any]] = field(default_factory=list)   # format 3; empty before

    @property
    def workspace_id(self) -> str:
        return self.header["workspace"]

    @property
    def lane_names(self) -> list[str]:
        return sorted(self.workspace.get("lanes", {}))

    def profile_dir(self, name: str) -> Path:
        return self.staging / "lanes" / name / "profile"

    def fingerprint(self, name: str) -> Path | None:
        path = self.staging / "lanes" / name / "fingerprint.json"
        return path if path.is_file() else None

    def summary(self) -> dict[str, Any]:
        return {"workspace": self.workspace_id, "created_at": self.header.get("created_at"),
                "source": self.header.get("source"), "agentos_version": self.header.get("agentos_version"),
                "counts": self.manifest.get("counts", {}), "lanes": self.manifest.get("lanes", []),
                "notes": self.manifest.get("notes", []), "not_exported": self.manifest.get("not_exported", [])}

    def discard(self) -> None:
        shutil.rmtree(self.staging, ignore_errors=True)


def _safe_rel(name: str) -> str:
    if not name.startswith(f"{DATA}/"):
        raise BadExport(f"unexpected member {name[:80]!r} in the export")
    rel = PurePosixPath(name[len(DATA) + 1:])
    if rel.is_absolute() or not rel.parts or any(p in ("", ".", "..") for p in rel.parts) or "\\" in name:
        raise BadExport(f"unsafe path {name[:80]!r} in the export")
    return rel.as_posix()


def _member_kind(rel: str, *, directory: bool) -> str:
    """json, profile or fingerprint (files), dir (directories); anything else is refused."""
    parts = rel.split("/")
    if not directory and len(parts) == 1 and parts[0] in (*JSON_MEMBERS, MEMORY_MEMBER, AGENDA_MEMBER):
        return "json"
    if parts[0] == "lanes":
        if len(parts) == 1 and directory:
            return "dir"
        if not LANE_NAME_RE.fullmatch(parts[1]):
            raise BadExport(f"lane name {parts[1][:60]!r} in the export is not valid")
        if len(parts) == 2 and directory:
            return "dir"
        if len(parts) == 3 and parts[2] == "fingerprint.json" and not directory:
            return "fingerprint"
        if parts[2] == "profile" and (directory or len(parts) > 3):
            return "dir" if directory else "profile"
    raise BadExport(f"unexpected member {rel[:80]!r} in the export")


def _hash_to(f: BinaryIO, target: Path, mtime: int) -> str:
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    digest = hashlib.sha256()
    with os.fdopen(fd, "wb") as out:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
            out.write(chunk)
    os.utime(target, (mtime, mtime))
    return digest.hexdigest()


def _read_meta(tar: tarfile.TarFile, member: tarfile.TarInfo) -> dict[str, Any]:
    if not member.isreg() or member.size > MAX_JSON:
        raise BadExport(f"{member.name} is malformed")
    f = tar.extractfile(member)
    assert f is not None
    try:
        data = json.loads(f.read())
    except ValueError:
        raise BadExport(f"{member.name} is not valid JSON") from None
    if not isinstance(data, dict):
        raise BadExport(f"{member.name} is malformed")
    return data


def _walk(path: Path, staging: Path, passphrase: str | None,
          identities: list[bytes] | None) -> tuple[dict, dict, dict[str, bytes], dict[str, tuple[int, str]], set[str]]:
    header: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    blobs: dict[str, bytes] = {}
    seen: dict[str, tuple[int, str]] = {}
    dirs: set[str] = set()
    try:
        raw = path.open("rb")
    except OSError as e:
        raise BadExport(f"cannot open {path}: {e.strerror or e}") from None
    with raw:
        try:
            dec = age.Decryptor(raw, passphrase, identities=identities)
            with tarfile.open(fileobj=dec, mode="r|gz") as tar:
                for member in tar:
                    if manifest is not None:
                        raise BadExport("the export has data after its manifest")
                    if header is None:
                        if member.name != f"{META}/format.json":
                            raise BadExport("this is not an openajar workspace export (no format.json first)")
                        header = _read_meta(tar, member)
                        if header.get("format") != FORMAT:
                            raise BadExport("this is not an openajar workspace export")
                        if header.get("version") not in READ_VERSIONS:
                            raise BadExport(f"export format {header.get('version')} is not supported by openajar "
                                            f"{__version__}; import it with a newer openajar")
                        continue
                    if member.name == f"{META}/manifest.json":
                        manifest = _read_meta(tar, member)
                        continue
                    rel = _safe_rel(member.name)
                    if member.isdir():
                        _member_kind(rel, directory=True)
                        dirs.add(rel)
                        (staging / rel).mkdir(mode=0o700, parents=True, exist_ok=True)
                        continue
                    if not member.isreg():
                        raise BadExport(f"{member.name[:80]} is not a regular file or directory")
                    kind = _member_kind(rel, directory=False)
                    if rel in seen:
                        raise BadExport(f"{rel} appears twice in the export")
                    f = tar.extractfile(member)
                    assert f is not None
                    if kind == "json":
                        if member.size > MAX_JSON:
                            raise BadExport(f"{rel} is too large")
                        data = f.read()  # held in memory only: vault.json carries secrets
                        blobs[rel] = data
                        seen[rel] = (len(data), hashlib.sha256(data).hexdigest())
                        continue
                    target = staging / rel
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    seen[rel] = (member.size, _hash_to(f, target, member.mtime))
        except age.NoIdentity as e:
            raise BadExport(str(e)) from None
        except age.WrongPassphrase:
            raise BadExport("wrong passphrase") from None
        except age.TruncatedArchive:
            raise BadExport(f"{path.name} is truncated (an interrupted export or copy); export it again") from None
        except age.NotAgeFile:
            raise BadExport(f"{path.name} is not an encrypted openajar export") from None
        except age.CorruptArchive as e:
            raise BadExport(f"{path.name}: {e}; it was damaged or altered") from None
        except (tarfile.TarError, EOFError) as e:
            raise BadExport(f"{path.name} is damaged ({type(e).__name__})") from None
        except OSError as e:
            raise ExportError(f"cannot stage the export: {e.strerror or e}") from None
    if header is None or manifest is None:
        raise BadExport(f"{path.name} is incomplete (no manifest); export it again")
    return header, manifest, blobs, seen, dirs


def _load_json(blobs: dict[str, bytes], name: str, kind: type) -> Any:
    try:
        data = json.loads(blobs[name])
    except KeyError:
        raise BadExport(f"the export has no {name}") from None
    except ValueError:
        raise BadExport(f"{name} in the export is not valid JSON") from None
    if not isinstance(data, kind):
        raise BadExport(f"{name} in the export is malformed")
    return data


def _load_memory(blobs: dict[str, bytes], header: dict[str, Any], workspace_id: str) -> list[dict[str, Any]]:
    """memory.jsonl: required in format 2, absent in format 1."""
    if header.get("version", 1) < 2:
        if MEMORY_MEMBER in blobs:
            raise BadExport(f"a format 1 export has no {MEMORY_MEMBER}")
        return []
    raw = blobs.get(MEMORY_MEMBER)
    if raw is None:
        raise BadExport(f"the export has no {MEMORY_MEMBER}")
    out: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            raise BadExport(f"{MEMORY_MEMBER} in the export is not valid JSON lines") from None
        if not isinstance(rec, dict) or not isinstance(rec.get("record"), str):
            raise BadExport(f"{MEMORY_MEMBER} in the export is malformed")
        out.append(rec)
    if out and (out[0].get("record") != "memory" or out[0].get("workspace") != workspace_id):
        raise BadExport(f"{MEMORY_MEMBER} does not match the export's workspace")
    return out


def _load_agenda(blobs: dict[str, bytes], header: dict[str, Any], workspace_id: str) -> list[dict[str, Any]]:
    """agenda.json: required in format 3, absent before."""
    if header.get("version", 1) < 3:
        if AGENDA_MEMBER in blobs:
            raise BadExport(f"a format {header.get('version', 1)} export has no {AGENDA_MEMBER}")
        return []
    entries = _load_json(blobs, AGENDA_MEMBER, list)
    if any(not isinstance(e, dict) or e.get("workspace") != workspace_id for e in entries):
        raise BadExport(f"{AGENDA_MEMBER} does not match the export's workspace")
    return entries


def _check_lanes(workspace_id: str, data: dict[str, Any], dirs: set[str]) -> None:
    lanes = data.get("lanes")
    if data.get("id") != workspace_id or not isinstance(lanes, dict):
        raise BadExport("workspace.json does not match the export's header")
    record = data.get("record")
    if record is not None and (not isinstance(record, dict) or record.get("id") != workspace_id):
        raise BadExport("the workspace record in the export is malformed")
    for name, lane in lanes.items():
        if not isinstance(name, str) or not LANE_NAME_RE.fullmatch(name) or not isinstance(lane, dict):
            raise BadExport("a lane in workspace.json is malformed")
        try:
            spec = LaneSpec.model_validate(lane.get("spec"))
        except ValueError:
            raise BadExport(f"the spec of lane {name} in the export is malformed") from None
        if spec.id != f"{workspace_id}/{name}":
            raise BadExport(f"lane {name} in the export names another lane ({spec.id})")
        if f"lanes/{name}/profile" not in dirs:
            raise BadExport(f"the export has no profile for lane {name}")
        readiness = lane.get("readiness")
        if readiness is not None and not isinstance(readiness, dict):
            raise BadExport(f"the readiness of lane {name} in the export is malformed")


def read_export(path: Path, staging: Path, *, passphrase: str | None = None,
                identities: list[bytes] | None = None) -> ExportContents:
    """Decrypt, unpack and verify an export. `staging` must not exist yet; it is created 700 and
    holds the profiles afterwards. On any problem it is removed again and BadExport is raised."""
    path, staging = Path(path), Path(staging)
    staging.mkdir(mode=0o700, parents=False)
    try:
        header, manifest, blobs, seen, dirs = _walk(path, staging, passphrase, identities)
        listed = {}
        for f in manifest.get("files", []):
            if not isinstance(f, dict) or not isinstance(f.get("path"), str):
                raise BadExport("the manifest is malformed")
            listed[f["path"]] = (f.get("size"), f.get("sha256"))
        if set(listed) != set(seen):
            missing = sorted(set(listed) - set(seen))[:3]
            extra = sorted(set(seen) - set(listed))[:3]
            raise BadExport(f"the manifest does not match the export (missing {missing}, unlisted {extra})")
        bad = [rel for rel, got in seen.items() if got != listed[rel]]
        if bad:
            raise BadExport(f"checksum mismatch for {', '.join(bad[:3])}; the export was altered or damaged")
        if set(manifest.get("dirs", [])) != dirs:
            raise BadExport("the manifest's directories do not match the export")
        if any(manifest.get(k) != header.get(k) for k in ("format", "version", "workspace", "created_at", "source")):
            raise BadExport("the manifest does not match the export's header")
        workspace_id = header.get("workspace")
        try:
            check_workspace_id(workspace_id)
        except InvalidRequest:
            raise BadExport("the export names an invalid workspace") from None
        workspace = _load_json(blobs, "workspace.json", dict)
        _check_lanes(workspace_id, workspace, dirs)
        names = set(workspace["lanes"])
        strays = sorted({rel.split("/")[1] for rel in [*seen, *dirs] if rel.startswith("lanes/")} - names)
        if strays or {lane.get("name") for lane in manifest.get("lanes", [])} != names:
            raise BadExport("the lanes in the export do not match its workspace.json")
        parts = {name.removesuffix(".json"): _load_json(blobs, name, list) for name in JSON_MEMBERS[1:]}
        memory = _load_memory(blobs, header, workspace_id)
        agenda = _load_agenda(blobs, header, workspace_id)
        blobs.clear()
        counts = manifest.get("counts")
        if not isinstance(counts, dict):
            raise BadExport("the manifest has no counts")
        actual = {"lanes": len(workspace["lanes"]), "vault_entries": len(parts["vault"]), "flows": len(parts["flows"]),
                  "schedules": len(parts["schedules"]), "grants": len(parts["grants"]),
                  "proxies": len(parts["proxies"]),
                  "flow_versions": sum(len(f.get("versions") or []) for f in parts["flows"] if isinstance(f, dict)),
                  "profile_bytes": sum(size for rel, (size, _) in seen.items() if rel.split("/")[2:3] == ["profile"]),
                  "profile_files": sum(1 for rel in seen if rel.split("/")[2:3] == ["profile"]),
                  "fingerprints": sum(1 for rel in seen if rel.endswith("/fingerprint.json"))}
        if header.get("version", 1) >= 2:
            actual.update(memory_counts(memory))
        if header.get("version", 1) >= 3:
            actual["agenda_entries"] = len(agenda)
        wrong = [k for k, v in actual.items() if counts.get(k) != v]
        if wrong:
            raise BadExport(f"the manifest's counts do not match the export ({', '.join(wrong)})")
        return ExportContents(header=header, manifest=manifest, workspace=workspace, staging=staging,
                              files=len(seen), bytes=sum(size for size, _ in seen.values()), memory=memory,
                              agenda=agenda, **parts)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def staging_dir(home: Path, what: str) -> Path:
    """A fresh private directory path under OPENAJAR_HOME/tmp (left out of backups), not created."""
    root = Path(home) / "tmp"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root / f"{what}-{pysecrets.token_hex(6)}"
