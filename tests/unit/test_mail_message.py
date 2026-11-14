"""What a stranger sends, made safe to read (agentos.runner.mail.message)."""

from __future__ import annotations

from email.message import EmailMessage

from agentos.runner.mail.message import attachment_name, html_to_text, message_ids, one_line, parse
from agentos.runner.workflows.mail import parse_message


def raw(*headers: str, body: bytes = b"hello\r\n") -> bytes:
    return "\r\n".join(headers).encode() + b"\r\n\r\n" + body


def test_an_encoded_word_cannot_put_a_line_break_in_a_value():
    p = parse(raw("From: a@example.com", "Subject: =?utf-8?Q?hi=0D=0ABcc:_all@example.com?="))
    assert "\n" not in p.subject and "\r" not in p.subject and p.subject == "hi Bcc: all@example.com"


def test_an_encoded_subject_and_sender_come_out_decoded():
    p = parse(raw("From: =?utf-8?B?Um9iaW4=?= <r@example.com>", "Subject: =?iso-8859-13?Q?Lab=E0s?="))
    assert p.sender == "Robin <r@example.com>" and p.subject == "Labąs"


def test_a_direction_override_is_taken_out_of_a_name():
    assert one_line(f"invoice{chr(0x202E)}gnp.exe{chr(0x200B)}") == "invoicegnp.exe"


def test_html_becomes_text_and_keeps_nothing_that_could_run_or_load():
    html = ('<html><head><title>t</title><style>p{color:red}</style></head><body><p>Hello <b>there</b></p>'
            '<script>alert(1)</script><img src="https://tracker.example/p.gif" alt="logo">'
            '<a href="https://example.com/x">the offer</a><a href="javascript:evil()">click</a></body></html>')
    text = html_to_text(html)
    assert "Hello there" in text and "the offer (https://example.com/x)" in text and "logo" in text
    assert "alert" not in text and "tracker.example" not in text and "javascript" not in text and "color" not in text


def test_an_html_only_message_reaches_the_body_as_text():
    m = EmailMessage()
    m["From"], m["Subject"] = "a@example.com", "x"
    m.set_content("<p>Only <i>html</i></p>", subtype="html")
    assert parse(m.as_bytes()).body == "Only html"
    assert parse_message(1, m.as_bytes()).body == "Only html"


def test_an_empty_plain_part_gives_way_to_the_html_one():
    m = EmailMessage()
    m.set_content(" ")
    m.add_alternative("<p>the real text</p>", subtype="html")
    assert parse(m.as_bytes()).body == "the real text"


def test_an_unknown_charset_does_not_stop_the_parse():
    p = parse(raw("Subject: x", "Content-Type: text/plain; charset=no-such-charset", body=b"caf\xe9 hello"))
    assert "hello" in p.body


def test_broken_structure_and_garbage_never_raise():
    assert parse(b"").body == ""
    assert parse(b"\xff\xfe\x00garbage").sender == ""
    deep = b"".join(b"Content-Type: multipart/mixed; boundary=b%d\r\n\r\n--b%d\r\n" % (i, i) for i in range(400))
    parse(deep)


def test_attachments_are_named_and_never_opened():
    m = EmailMessage()
    m["Subject"] = "x"
    m.set_content("see the file")
    m.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="../../etc/passwd y.pdf")
    p = parse(m.as_bytes())
    assert p.body.strip() == "see the file"
    assert p.attachments == ({"name": "passwd y.pdf", "type": "application/pdf"},)
    assert attachment_name(None) == "unnamed"


def test_the_body_is_capped():
    assert len(parse(raw("Subject: x", body=b"a" * 300_000)).body) == 200_000


def test_threading_headers_and_an_auto_reply_are_read():
    got = parse_message(3, raw("Message-ID: <a@x>", "In-Reply-To: <b@y>", "References: <c@z> <b@y>",
                               "Auto-Submitted: auto-replied; owner=x"), seen=True)
    assert (got.message_id, got.in_reply_to, got.references, got.seen, got.auto_reply) == \
        ("<a@x>", "<b@y>", "<c@z> <b@y>", True, True)
    assert parse_message(3, raw("Auto-Submitted: no")).auto_reply is False
    assert parse_message(3, raw("Subject: x")).auto_reply is False


def test_only_whole_ids_leave_a_header_that_came_from_outside():
    assert message_ids('<a@x> junk <b y@z> "<c@w>"') == ["<a@x>", "<c@w>"]
