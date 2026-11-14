"""Who answered: what the node sent, matched against what comes back (docs/EMAIL.md, "New mail in routines").

Each routine message gets a Message-ID nobody can guess, kept with the run, step, address and mailbox
it went out on. A reply counts when its In-Reply-To or References names one of those ids, in the same
mailbox, and its From is the address that message went to. Neither alone is enough: From can be forged,
and a forwarded thread carries the id to people the message was not for. So a reply can stop mail only
to the person it answers, in that thread, and anyone without the id gains nothing by claiming to be them.
"""

from __future__ import annotations

import secrets
from datetime import timedelta
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any

from agentos.runner.mail.message import message_ids
from agentos.runner.util import Clock
from agentos.runner.workflows.models import MAX_WAIT_DAYS
from agentos.runner.workflows.sends import base_address, normal_address
from agentos.runner.workflows.store import WorkflowStore

MAX_IDS = 50  # of a References header: a thread's newest ids are last, and a stranger writes the header
KEEP_DAYS = MAX_WAIT_DAYS + 30  # longer than a run can wait before it looks


def message_id(mailbox_address: str) -> str:
    return f"<{secrets.token_urlsafe(24)}@{mailbox_address.rpartition('@')[2]}>"


def sender_of(text: str) -> str | None:
    """The one address a From names, or None: a From with several is not any one of them."""
    found = [a for _, a in getaddresses([text or ""]) if a]
    return normal_address(found[0]) if len(found) == 1 else None


def named_ids(message: Any) -> list[str]:
    """The ids a message says it answers, the closest first."""
    direct = message_ids(message.in_reply_to)[:MAX_IDS]
    earlier = message_ids(message.references)[-MAX_IDS:]
    return list(dict.fromkeys([*direct, *reversed(earlier)]))


class Replies:
    def __init__(self, store: WorkflowStore, clock: Clock):
        self.store, self.clock = store, clock

    def thread(self, message: EmailMessage, *, run_id: str, path: str, address: str, box: Any) -> None:
        """Give a message its id and keep it, before it leaves, so a quick reply is never missed."""
        minted = message_id(box.address)
        del message["Message-ID"]
        message["Message-ID"] = minted
        now = self.clock.now()
        self.store.thread_put(minted, run_id, path, address, box.id, now)
        self.store.threads_prune(now - timedelta(days=KEEP_DAYS))

    def note(self, box: Any, message: Any) -> str | None:
        """A message arrived: keep who it answers, and give the run it answers, or None. An automatic
        answer (out of office) is nobody answering."""
        ids = named_ids(message)
        sender = sender_of(message.sender)
        if not ids or sender is None or message.auto_reply:
            return None
        sent = self.store.threads(ids, box.id)
        matched = [sent[i] for i in ids if i in sent and base_address(sent[i]["address"]) == base_address(sender)]
        for row in matched:
            self.store.reply_put(row["run_id"], row["path"], row["address"], self.clock.now())
        return matched[0]["run_id"] if matched else None

    def replied(self, run_id: str, path: str) -> set[str]:
        return self.store.replied(run_id, path)
