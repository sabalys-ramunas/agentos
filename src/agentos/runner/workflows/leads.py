"""Leads: how an agent proposes people to write to, and how a person decides (docs/EMAIL.md, "Leads").

A list changes only by a person. `propose` puts what an agent found in a review queue, and the owner
takes all of a batch, picks from it, or drops it. Only `answered_in_person` counts on the card, for a
controller's relay carries no address, so nobody there has read the batch.

An agent learns nothing here about who is on the list. Its answer is the same whether an address was
on the list, suppressed, waiting, dropped before, or new, and so are the bounds, which count what was
received. The card is the node's and carries no grant, so an agent's family cannot read how many got
through. What an agent does read is a list's size (`workflow_catalog`, a workflow's `send_limits`), so
once a person has decided it can see how many were added, and never who. How many are suppressed is a
person's to read, and `send_suppress` answers the same either way.

A card reaches the controller and the notifier, so it holds counts and source names, and no person's
name, company, domain or address. Who they are is read on the node, in the panel or the CLI.

Everything in a lead is a stranger's text: it is kept as it came and shown as text. That goes for
`name` once it is on a list too (`engine.py`), so a send it fills asks first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentos.core.errors import AgentosError, InvalidRequest, NotFound, PolicyDenied, ServiceUnavailable
from agentos.core.models import Principal
from agentos.runner.owner import answered_in_person
from agentos.runner.util import iso, new_id, parse_dt
from agentos.runner.workflows.sends import MAX_LIST_SIZE, base_address, normal_address

log = logging.getLogger("agentos.runner")

MAX_BATCH = 50          # leads in one proposal: one look for a person
MAX_PER_FAMILY_DAY = 200  # a grant family's leads in a day, as many as an account sends by default
MAX_PER_LIST_DAY = 500  # a list's leads in a day, across every family
DROP_DAYS = 30          # how long a family stays away from an address a person dropped
REVIEW_DAYS = 7         # how long a batch waits for a person before it goes
SOURCES_SHOWN = 5
TAKE_ALL = "Take them all"
DROP_ALL = "Drop them"
NOTE_KEY = "leads_note"
SALT_KEY = "lead_salt"
NOTE = ("Before you write to these people: you are the sender, and you answer for it. Rules on unsolicited mail differ "
        "by country. In the EU, writing to a named person can need their consent, and to a business less so. agentos "
        "gives you the tools and does not decide this for you.")
ADDRESS_IN_TEXT_RE = re.compile(r"\S*\s*@\s*\S*")
PATH_RE = re.compile(r"[/?#\\]")
OWN_PAGE = "their own page"
NO_NAME = "a source with no name"
NOT_IN_PERSON = "only the owner decides on leads, signed in on this computer or at its command line"


class Lead(BaseModel):
    """One record of what was found. There is no field for a pattern, a file or a list of addresses."""
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str
    name: str | None = Field(default=None, max_length=120)
    company: str | None = Field(default=None, max_length=120)
    source: str = Field(min_length=1, max_length=500)
    why: str | None = Field(default=None, max_length=300)


def plain(text: Any, limit: int) -> str:
    """A stranger's text as one short line for a card: no control or formatting characters (which
    can reorder what a person reads), no line breaks, no quote that could end the quote around it,
    and no address, wherever one was typed, because a card leaves the node."""
    kept = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
                   for c in str(text or "") if unicodedata.category(c) != "Cf")
    return ADDRESS_IN_TEXT_RE.sub("(address)", " ".join(kept.replace('"', "'").split()))[:limit]


def where_from(lead: dict[str, Any]) -> str:
    """A lead's source as a card shows it: a page's host, or the label it was given up to any path,
    clipped. Never a link, and never the lead: a source that names their domain, name or company is
    their own page, because a card leaves the node."""
    source = lead["source"]
    host = urlsplit(source).hostname if "://" in source else None
    shown = plain(host or PATH_RE.split(source)[0], 60)
    who = (lead["address"].rpartition("@")[2], lead["name"], lead["company"])
    return OWN_PAGE if any(x and x.lower() in shown.lower() for x in who) else shown or NO_NAME


def bad_source(source: str) -> bool:
    try:
        return not source.strip() or ("://" in source and urlsplit(source).scheme.lower() not in ("http", "https"))
    except ValueError:  # `http://[nonsense`
        return True


class Leads:
    def __init__(self, service: Any):
        self.service = service
        self.store, self.sends, self.clock, self.runner = service.store, service.sends, service.clock, service.runner

    def _approvals(self) -> Any:
        return self.service._svc("approvals")

    def _digest(self, address: str) -> str:
        salt = self.store.runner_store.meta(SALT_KEY, secrets.token_hex(16))
        return hashlib.sha256(f"{salt}:{address}".encode()).hexdigest()[:32]

    def _family(self, actor: Any) -> str:
        path = self.runner._grant_path(actor.grant_id) if actor.grant_id else None
        return path.strip("/").split("/")[0] if path else (actor.grant_id or actor.by)

    # ---- proposing ----
    @staticmethod
    def _parse(leads: list[Any]) -> tuple[list[tuple[str, Lead]], list[dict[str, Any]]]:
        """What can be read as a record, and what cannot. This looks at the record alone, so what it
        says tells an agent nothing about a list."""
        good, refused = [], []
        for index, raw in enumerate(leads):
            try:
                lead = Lead.model_validate(raw)
            except ValidationError:
                refused.append({"index": index, "why": "A lead is one record: email and source, and name, company and why if known."})
                continue
            address = normal_address(lead.email)
            if address is None:
                refused.append({"index": index, "why": "That is not one email address."})
            elif bad_source(lead.source):
                refused.append({"index": index, "why": "A source is the page's http or https address, or a name."})
            else:
                good.append((address, lead))
        return good, refused

    def _left_out(self, address: str, list_id: str, workspace: str, family: str, now: datetime) -> bool:
        return (self.store.is_suppressed(workspace, address) or self.store.person(list_id, address) is not None
                or self.store.lead_known(list_id, address) or self.store.lead_dropped(family, self._digest(address), now))

    def propose(self, list_id: str, leads: Any, actor: Any) -> dict[str, Any]:
        """Into the queue. The answer is about the records, and never about who is on the list."""
        info = self.store.list_info(list_id) if isinstance(list_id, str) else None
        if info is None:
            raise NotFound(f"there is no list {list_id} on this node")
        if not isinstance(leads, list) or not leads:
            raise InvalidRequest("leads is a list of records, one for each person found")
        if len(leads) > MAX_BATCH:
            raise PolicyDenied(f"too_many_leads: a batch holds at most {MAX_BATCH}, so a person can read it")
        approvals = self._approvals()
        if approvals is None:
            raise ServiceUnavailable("asking a person needs the policy service on this node")
        good, refused = self._parse(leads)
        if not good:
            return {"batch": None, "received": 0, "refused": refused, "status": "nothing_to_review"}
        now, workspace, family = self.clock.now(), info["workspace"], self._family(actor)
        self.store.lead_drops_prune(now)
        batch_id = new_id("lb")
        rows: dict[str, dict[str, Any]] = {}
        for address, lead in good:
            one = base_address(address)  # jane@ and jane+news@ are one person, so one lead
            if one not in rows and not self._left_out(address, list_id, workspace, family, now):
                rows[one] = {"list_id": list_id, "address": address, "batch_id": batch_id, "name": lead.name,
                                 "company": lead.company, "source": lead.source, "why": lead.why, "added_at": iso(now)}
        batch = {"id": batch_id, "list_id": list_id, "workspace": workspace, "family": family, "day": self.sends.day(),
                 "received": len(good), "left_out": len(good) - len(rows), "state": "waiting" if rows else "empty",
                 "by": actor.by, "at": iso(now), "expires_at": iso(now + timedelta(days=REVIEW_DAYS))}
        got = self.store.lead_batch_put(batch, list(rows.values()), per_family=MAX_PER_FAMILY_DAY, per_list=MAX_PER_LIST_DAY)
        if got != "ok":
            bound = (f"your agents may propose {MAX_PER_FAMILY_DAY} a day" if got == "family"
                     else f"this list takes {MAX_PER_LIST_DAY} proposed leads a day")
            raise PolicyDenied(f"too_many_leads: {bound}, so try again tomorrow")
        if rows:
            self._card(batch, info, actor)
        return {"batch": batch_id, "received": len(good), "refused": refused, "status": "review"}

    def _card(self, batch: dict[str, Any], info: dict[str, Any], actor: Any) -> None:
        """Written here, from the records, and never in an agent's words: `why` is not on it. Counts
        and source names only, because it leaves the node. It is the node's own question, under no
        grant, so no agent reads how many got through."""
        leads = self.store.leads_of(batch["id"])
        first = self.note() is not None
        sources = Counter(where_from(x) for x in leads)
        named = sources.most_common(SOURCES_SHOWN)
        counts = [f"{count} from {where}" for where, count in named]
        left = sources.total() - sum(count for _, count in named)
        more = f", and {left} from {len(sources) - len(named)} more" if left else ""
        how_many = f"{len(leads)} {'lead' if len(leads) == 1 else 'leads'}"
        lines = [f"An agent proposes {how_many} for {plain(info['name'], 120)}: {', '.join(counts)}{more}.",
                 "Who they are, every address and every source is on this computer, in Leads, where you can pick some of them."]
        if first:
            lines.append(NOTE)
        owner = str(actor.principal.get("owner") or "owner")
        ap = self._approvals().create_question(
            question="\n".join(lines), options=[TAKE_ALL, DROP_ALL], requester=Principal(kind="flow", id="leads", owner=owner),
            grant_id=None, grant_path=None, ttl_s=REVIEW_DAYS * 86_400, workspace=batch["workspace"])
        self.store.lead_batch_card(batch["id"], ap.id, noted=first)

    # ---- a person decides ----
    def _close(self, batch: dict[str, Any], taken: set[str], by: str) -> str:
        """Takes `taken` onto the list. The rest of the batch was dropped by a person. Whoever was
        suppressed since it was proposed stays off, and is not remembered as a drop."""
        now = self.clock.now()
        leads = self.store.leads_of(batch["id"])
        take = [x for x in leads if x["address"] in taken and not self.store.is_suppressed(batch["workspace"], x["address"])]
        drops = [self._digest(x["address"]) for x in leads if x["address"] not in taken]
        state = "dropped" if not taken else "taken" if len(taken) == len(leads) else "picked"
        got = self.store.lead_batch_close(batch["id"], state, take, drops, now + timedelta(days=DROP_DAYS), by, now, MAX_LIST_SIZE)
        if got == "ok" and batch["noted"]:
            self.acknowledge(in_person=True, by=by)
        return got

    def decide(self, batch_id: str, take: Any, *, in_person: bool, by: str) -> dict[str, Any]:
        """`take` is `all`, or the addresses to keep: the rest are dropped, and none is none."""
        if not in_person:
            raise PolicyDenied(NOT_IN_PERSON)
        batch = self._batch(batch_id)
        if batch["state"] != "waiting":
            raise InvalidRequest(f"this batch is already {batch['state']}")
        waiting = {x["address"] for x in self.store.leads_of(batch_id)}
        if take == "all":
            taken = waiting
        elif isinstance(take, list):
            taken = {normal_address(x) for x in take}
            if not taken <= waiting:
                raise InvalidRequest("only an address in this batch can be picked from it")
        else:
            raise InvalidRequest('take is "all", or a list of the addresses to keep')
        got = self._close(batch, taken, by)  # type: ignore[arg-type]
        if got == "full":
            raise InvalidRequest(f"a list holds at most {MAX_LIST_SIZE} people")
        if got == "closed":
            raise InvalidRequest("this batch was just decided")
        self._drop_card(self.store.lead_batch(batch_id) or batch)
        return self._public(self.store.lead_batch(batch_id) or batch)

    def _drop_card(self, batch: dict[str, Any]) -> None:
        """A batch decided in the panel or the CLI leaves nothing waiting in Needs you."""
        approvals = self._approvals()
        if approvals is None or not batch["approval_id"]:
            return  # the daemon's tick closes it
        try:
            if approvals.get(batch["approval_id"]).state == "pending":
                approvals.decide(batch["approval_id"], "deny", by="owner", note="Decided in Leads.")
        except AgentosError:
            log.info("the card of batch %s was closed already", batch["id"])
        self.store.lead_batch_card(batch["id"], None)

    def check_cards(self) -> int:
        """Look at what waits. Only a person's answer on the card decides, and an answer from anyone
        else decides nothing: the batch stays in Leads, for a person, and the owner hears so."""
        approvals = self._approvals()
        if approvals is None:
            return 0
        for batch in self.store.lead_batches_with_a_card():
            self._drop_card(batch)
        done = 0
        for batch in self.store.lead_batches("waiting"):
            if self.clock.now() >= parse_dt(batch["expires_at"]):
                self.store.lead_batch_close(batch["id"], "expired", [], [], self.clock.now(), "nobody", self.clock.now(), MAX_LIST_SIZE)
                self._drop_card({**batch, "state": "expired"})
                done += 1
                continue
            done += self._read_card(approvals, batch)
        return done

    def _read_card(self, approvals: Any, batch: dict[str, Any]) -> int:
        if not batch["approval_id"]:
            return 0
        try:
            ap = approvals.get(batch["approval_id"])
        except NotFound:
            ap = None
        if ap is not None and ap.state == "pending":
            return 0
        said = None
        if ap is not None and answered_in_person(ap):
            said = DROP_ALL if ap.state == "denied" else (ap.answer or "")
        if said in (TAKE_ALL, DROP_ALL):
            waiting = {x["address"] for x in self.store.leads_of(batch["id"])}
            got = self._close(batch, waiting if said == TAKE_ALL else set(), str(ap.decided_by))
            self.store.lead_batch_card(batch["id"], None)
            if got == "full":
                self.runner._notify("Leads", f"The list is full, at {MAX_LIST_SIZE} people, so nothing was added.", data={"batch": batch["id"]})
            return 1
        self.store.lead_batch_card(batch["id"], None)
        if ap is not None and ap.state != "expired":
            self.runner._notify("Leads", "A batch of leads still waits for you. Only the owner decides it, in Leads on this computer.",
                                data={"batch": batch["id"]})
        return 1

    # ---- what the owner reads ----
    def _batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.store.lead_batch(batch_id)
        if batch is None:
            raise NotFound(f"there is no batch {batch_id} on this node")
        return batch

    @staticmethod
    def _public(batch: dict[str, Any]) -> dict[str, Any]:
        return {"id": batch["id"], "list": batch["list_id"], "workspace": batch["workspace"], "state": batch["state"],
                "received": batch["received"], "left_out": batch["left_out"], "by": batch["by"], "at": batch["at"],
                "expires_at": batch["expires_at"], "decided_by": batch["decided_by"], "decided_at": batch["decided_at"]}

    @staticmethod
    def _lead(row: dict[str, Any]) -> dict[str, Any]:
        keys = ("name", "company", "source", "why", "added_at", "state", "taken_at", "taken_by")
        return {"email": row["address"], **{k: row.get(k) for k in keys}}

    def review(self, *, in_person: bool) -> dict[str, Any]:
        """What waits, with the note for a first list of leads until a person has seen it."""
        if not in_person:
            raise PolicyDenied(NOT_IN_PERSON)
        return {"note": self.note(), "batches": [self._public(b) for b in self.store.lead_batches("waiting")]}

    def batch(self, batch_id: str, *, in_person: bool) -> dict[str, Any]:
        if not in_person:
            raise PolicyDenied(NOT_IN_PERSON)
        return {**self._public(self._batch(batch_id)), "leads": [self._lead(x) for x in self.store.leads_of(batch_id)]}

    def export(self, list_id: str, *, in_person: bool) -> dict[str, Any]:
        """Each lead of a list, with where it came from and when, which the law may ask of the owner."""
        if not in_person:
            raise PolicyDenied(NOT_IN_PERSON)
        if self.store.list_info(list_id) is None:
            raise NotFound(f"there is no list {list_id} on this node")
        return {"list": list_id, "leads": [self._lead(x) for x in self.store.leads_on(list_id)]}

    def erase(self, workspace: Any, email: Any, *, in_person: bool) -> dict[str, int]:
        if not in_person:
            raise PolicyDenied(NOT_IN_PERSON)
        address = normal_address(email)
        if address is None or not workspace:
            raise InvalidRequest("erasing a lead needs a workspace and an email address")
        return self.store.lead_erase(str(workspace), address, self._digest(address), self.clock.now() + timedelta(days=DROP_DAYS))

    # ---- the note for a first list of leads ----
    def note(self) -> str | None:
        """The words, until a person has seen them."""
        return None if self.store._rows("SELECT 1 FROM meta WHERE key = ?", (NOTE_KEY,)) else NOTE

    def acknowledge(self, *, in_person: bool, by: str) -> bool:
        if not in_person:
            raise PolicyDenied(NOT_IN_PERSON)
        seen = json.dumps({"by": by, "at": iso(self.clock.now())})
        return self.store._exec("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (NOTE_KEY, seen)).rowcount > 0
