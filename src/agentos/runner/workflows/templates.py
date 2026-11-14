"""The templates: real jobs, ready to fill in. `tag:` steps take what the workspace has.

They act on what they read from the owner's own accounts and use what arrived only to look it up,
so a step that sends runs on its own (a commit filled with outside text asks first, docs/WORKFLOWS.md).
Social posting is one template among them.
"""

from __future__ import annotations

from typing import Any


def _act(id_: str, label: str, tag: str, **rest: Any) -> dict[str, Any]:
    return {"id": id_, "kind": "action", "label": label, "operation": f"tag:{tag}", "inputs": {}, **rest}


def _send(id_: str, label: str, to: str | None = None, **inputs: str) -> dict[str, Any]:
    return _act(id_, label, "mail.send", inputs=inputs, send={"list": None, **({"to": to} if to else {})})


TEMPLATES: list[dict[str, Any]] = [
    {"id": "order", "name": "New order handling", "icon": "order",
     "sentence": "When a new order arrives, read it, check the stock, then email the customer.",
     "note": "For a shop. Nobody waits to hear from you.",
     "trigger": {"kind": "webhook", "label": "when a new order arrives"},
     "steps": [
         _act("order", "read the order", "shop.order", inputs={"order_id": "{{trigger.order_id}}"}),
         _act("stock", "check the stock", "shop.stock", inputs={"item": "{{order.item}}"}),
         {"id": "enough", "kind": "branch", "label": "if it is in stock",
          "when": {"left": "{{stock.left}}", "op": "more_than", "right": "0"},
          "then": [_send("thanks", "email the customer", "{{order.email}}", subject="Your order is on its way",
                         body="Hello {{person.name}}, thank you for your order.")],
          "otherwise": [{"id": "tell", "kind": "notify", "label": "tell you", "message": "An order is out of stock."}]},
     ]},
    {"id": "invoice", "name": "Invoice reminders", "icon": "invoice",
     "sentence": "Every weekday at 09:00, find overdue invoices, then for each one, send a reminder.",
     "note": "Get paid without chasing anyone yourself.",
     "trigger": {"kind": "schedule", "cron": "0 9 * * 1-5", "label": "every weekday at 09:00"},
     "steps": [
         _act("find", "find overdue invoices", "invoices.overdue"),
         {"id": "each", "kind": "loop", "label": "for each invoice", "item": "invoice", "over": "{{find.invoices}}",
          "body": [_send("remind", "send a reminder", "{{each.email}}", subject="Invoice {{each.number}} is overdue",
                         body="Hello {{person.name}}, invoice {{each.number}} is {{each.days}} days overdue.")]},
     ]},
    {"id": "newsletter", "name": "Newsletter", "icon": "newsletter",
     "sentence": "Every Thursday at 10:00, write the newsletter, ask you to read it, then send it to your subscribers.",
     "note": "You stay the editor. It does the rest.",
     "trigger": {"kind": "schedule", "cron": "0 10 * * 4", "label": "every Thursday at 10:00"},
     "steps": [
         _act("write", "write the newsletter", "mail.draft"),
         {"id": "read", "kind": "ask", "label": "ask you to read it", "question": "Is this newsletter ready to go?"},
         _send("send", "send it to your subscribers", subject="{{write.subject}}", body="{{write.body}}"),
     ]},
    {"id": "outreach", "name": "Customer outreach", "icon": "outreach",
     "sentence": "Every Tuesday at 10:00, find people who went quiet, then for each one, send a follow-up.",
     "note": "To your own list of people you already know.",
     "trigger": {"kind": "schedule", "cron": "0 10 * * 2", "label": "every Tuesday at 10:00"},
     "steps": [
         _act("find", "find people who went quiet", "leads.quiet"),
         {"id": "each", "kind": "loop", "label": "for each person", "item": "person", "over": "{{find.leads}}",
          "body": [_send("follow", "send a follow-up", "{{each.email}}", subject="Checking in",
                         body="Hello {{person.name}}, how are things going?")]},
     ]},
    {"id": "post", "name": "Social posts", "icon": "post",
     "sentence": "Every Friday at 09:00, pick an idea, ask you to read it, then post it.",
     "note": "One post, on the accounts you choose.",
     "trigger": {"kind": "schedule", "cron": "0 9 * * 5", "label": "every Friday at 09:00"},
     "steps": [
         _act("plan", "pick an idea", "content.plan"),
         {"id": "read", "kind": "ask", "label": "ask you to read it", "question": "Is this post ready to go out?"},
         _act("post", "post it", "post.publish", inputs={"caption": "{{plan.idea}}"}),
     ]},
    {"id": "webhook", "name": "Webhook job", "icon": "webhook", "developer": True,
     "sentence": "When your system calls, do the job, then tell you.",
     "note": "For developers: a signed webhook starts it, and a file defines it.",
     "trigger": {"kind": "webhook", "label": "when your system calls"},
     "steps": [
         _act("job", "do the job", "job.run"),
         {"id": "tell", "kind": "notify", "label": "tell you", "message": "The job ran."},
     ]},
    {"id": "new_row", "name": "When a new row appears, send me a note", "icon": "new_row",
     "sentence": "When a new row appears on a site, send you a note.",
     "note": "Choose a Site API list and the field that says which row is which.",
     "trigger": {"kind": "site_change", "every_minutes": 15, "label": "when a new row appears"},
     "steps": [
         {"id": "each", "kind": "loop", "label": "for each new row", "item": "row", "over": "{{trigger.added}}",
          "body": [{"id": "tell", "kind": "notify", "label": "send you a note", "message": "New row: {{each.row_key}}"}]},
     ]},
    {"id": "reply", "name": "When someone replies, tell me", "icon": "outreach",
     "sentence": "When someone replies to a message a routine sent, tell you.",
     "note": "A reply is matched to what was sent, not to who it says it is from.",
     "trigger": {"kind": "email", "label": "when an email arrives"},
     "steps": [
         {"id": "answers", "kind": "branch", "label": "if it answers something a routine sent",
          "when": {"left": "{{trigger.in_reply_to_run}}", "op": "not_empty"},
          "then": [{"id": "tell", "kind": "notify", "label": "tell you", "message": "{{trigger.from}} replied: {{trigger.subject}}"}]},
     ]},
    {"id": "follow_up", "name": "Follow up once if no reply in 3 days", "icon": "outreach",
     "sentence": "When you start it, send to your list, wait 3 days, then follow up once with those who did not reply.",
     "note": "The follow-up keeps every rule of the first message, and counts against the same daily limit.",
     "trigger": {"kind": "manual", "label": "when you start it"},
     "steps": [
         _send("first", "send the first message", subject="{{trigger.subject}}", body="{{trigger.body}}"),
         {"id": "wait", "kind": "wait", "label": "wait 3 days", "amount": 3, "unit": "days"},
         {"id": "answered", "kind": "stop_if_replied", "label": "leave out whoever replied", "after": "first"},
         _send("again", "follow up once", subject="{{trigger.subject}}", body="{{trigger.follow_up}}"),
     ]},
    {"id": "to_list", "name": "Send this to my list", "icon": "newsletter",
     "sentence": "When you start it, send your message to everyone on your list.",
     "note": "One message each, with an unsubscribe. It asks first where your rules say so.",
     "trigger": {"kind": "manual", "label": "when you start it"},
     "steps": [_send("send", "send it to your list", subject="{{trigger.subject}}", body="{{trigger.body}}")]},
]


def fill(template: dict[str, Any], workspace: str | None, tagged: Any) -> dict[str, Any]:
    """A definition from a template: each `tag:` takes what the workspace has, or stays unfinished
    for a person to choose. `tagged(tag)` is the operation id, or None."""
    def steps(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for item in items:
            step = dict(item)
            operation = step.get("operation")
            if isinstance(operation, str) and operation.startswith("tag:"):
                step["operation"] = tagged(operation[4:])
            for arm in ("then", "otherwise", "body"):
                if arm in step:
                    step[arm] = steps(step[arm])
            out.append(step)
        return out

    return {"name": template["name"], "workspace": workspace, "trigger": dict(template["trigger"]),
            "steps": steps(template["steps"])}
