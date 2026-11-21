"""`mail_send`: one message, inside every rule a list send keeps (docs/EMAIL.md, "Sending").

A send from a mailbox spends that mailbox's daily limit through the same ledger as a list send, so
there is one counter whichever path it took. A message outside a list goes through a ledger row of
its own day (`direct:<day>`): its key is what makes "the same address twice" atomic, and its rows are
what counts the addresses of the day. A failure that may have sent stays in the ledger, so it is never
repeated. Suppression, the unsubscribe the node adds, and the row written before the message leaves
are the same as for a list. An error here reaches a controller in a trace, so none names an address.

A reply is a conversation, not sending to many. The node decides what one is (`is_reply`), never a
flag from an agent. It has a row of its own (`reply:<day>`), so it spends the daily limit and is
never repeated, and nothing in the `direct:` rules counts it. It carries no unsubscribe footer. It
stays inside suppression, and a From can be forged, so the limit is what bounds a false one.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from openajar.core.errors import InvalidRequest, OpenajarError, PolicyDenied
from openajar.runner.mail.message import message_ids
from openajar.runner.workflows.mail import Mailbox, Received, SendInDoubt, SendRefused, build_message
from openajar.runner.workflows.replies import sender_of
from openajar.runner.workflows.sends import base_address, normal_address

log = logging.getLogger("openajar.runner")

MAX_DIRECT_A_DAY = 10
COUNTED = ("sending", "sent", "unknown")


class UseAList(PolicyDenied):
    """Sending this way would be sending to many: that goes through a list."""
    code = "use_a_list"


class MailSuppressed(PolicyDenied):
    """This address asked not to be contacted."""
    code = "mail_suppressed"


class MailDailyLimit(PolicyDenied):
    """The mailbox's daily limit is used up."""
    code = "mail_daily_limit"


class MailNotSent(OpenajarError):
    """It certainly did not leave. Safe to try again."""
    code = "mail_not_sent"


class MailInDoubt(OpenajarError):
    """It may have left, so it is never sent again."""
    code = "mail_in_doubt"


def workspace_of(box: Mailbox) -> str:
    return box.id.split("/", 1)[0]


def recipient(to: Any) -> str:
    """One plain address: a value that holds anything else never reaches a header."""
    address = normal_address(to)
    if address is None:
        raise InvalidRequest("`to` is one email address.")
    return address


def direct_run(day: str) -> str:
    return f"direct:{day}"


def reply_run(day: str) -> str:
    return f"reply:{day}"


def is_reply(address: str, parent: Received | None) -> bool:
    """`parent` is a message the node read from this mailbox, a person at this exact address wrote it,
    and it has an id for the thread headers to name. Anything else is a message like any other."""
    return (parent is not None and not parent.unreadable and not parent.auto_reply
            and sender_of(parent.sender) == address and bool(message_ids(parent.message_id)))


def check(wf: Any, box: Mailbox, address: str, *, reply: bool = False) -> None:
    """What refuses a message before anyone is asked. Checked again just before it goes."""
    sends = wf.sends
    if sends.store.is_suppressed(workspace_of(box), address):
        raise MailSuppressed("That address asked not to be contacted from this workspace, so this was not sent.")
    if reply:
        return
    day = sends.day()
    rows = [r for r in sends.store.ledger_rows(direct_run(day), box.id) if r["state"] in COUNTED]
    if any(base_address(r["address"]) == base_address(address) for r in rows):
        raise UseAList(f"{box.id} already wrote to that address today. Writing again the same day counts as sending "
                       "to many, so it goes through a list.")
    if len(rows) >= MAX_DIRECT_A_DAY:
        raise UseAList(f"{box.id} already wrote to {MAX_DIRECT_A_DAY} addresses today outside a list. More than that "
                       "counts as sending to many, so it goes through a list.")


async def send_one(wf: Any, box: Mailbox, address: str, subject: str, body: str, *, parent: Any = None) -> dict[str, Any]:
    """After the caller was authorized: claim the slot, send, and write down what happened."""
    reply = is_reply(address, parent)
    check(wf, box, address, reply=reply)
    sends = wf.sends
    unsubscribe = None if reply else sends.unsubscribe_for(workspace_of(box), address, mailbox=box.address)
    if unsubscribe is None and not reply:
        raise MailNotSent("This message has no way to unsubscribe, so it was not sent.")
    message = build_message(box, to=address, subject=subject, body=body, unsubscribe=unsubscribe,
                            in_reply_to=parent.message_id if parent else "", references=parent.references if parent else "")
    day, now = sends.day(), wf.clock.now()
    store, person = sends.store, base_address(address)  # the slot is the person's, whatever the tag
    run, slot = direct_run(day), person
    if reply:  # one row for each answer, and the same answer again is the same row
        said = hashlib.sha256(f"{parent.uid}\0{subject}\0{body}".encode("utf-8", "replace")).hexdigest()[:16]
        run, slot = reply_run(day), f"{person}#{said}"
    store.ledger_clear_failed(run, box.id)  # a message that certainly did not leave holds no slot
    got = store.ledger_claim(run, box.id, slot, box.id, day, now, sends.limit_of(box.id))
    if got == "limit":
        raise MailDailyLimit(f"{box.id} reached its limit of {sends.limit_of(box.id)} today. It opens again at the "
                             "owner's midnight.")
    if got != "claimed" and reply:
        raise MailInDoubt("This same answer already left, or may have, so it will not be sent again.")
    if got != "claimed":
        raise UseAList(f"{box.id} already wrote to that address today, so this goes through a list.")
    try:
        filed = await wf.mail.send(box, message, to=address)
    except SendRefused as e:
        store.ledger_mark(run, box.id, slot, box.id, "failed", day, wf.clock.now(), str(e)[:200])
        raise MailNotSent(f"Not sent: {e}") from None
    except SendInDoubt as e:
        store.ledger_mark(run, box.id, slot, box.id, "unknown", day, wf.clock.now(), str(e)[:200])
        raise MailInDoubt("It may or may not have left, so it will not be sent again.") from None
    except Exception as e:  # handed over or not, nobody can say: it is never repeated
        log.exception("mailbox %s: a send ended in a way nobody can read", box.id)
        store.ledger_mark(run, box.id, slot, box.id, "unknown", day, wf.clock.now(), type(e).__name__)
        raise MailInDoubt("It may or may not have left, so it will not be sent again.") from None
    store.ledger_mark(run, box.id, slot, box.id, "sent", day, wf.clock.now())
    return {"sent": True, "mailbox": box.id, "message_id": str(message["Message-ID"]), "in_sent_folder": bool(filed), "reply": reply,
            "sent_today": store.sent_on(box.id, day), "daily_limit": sends.limit_of(box.id)}
