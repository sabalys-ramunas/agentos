"""Email as a built-in (docs/EMAIL.md): connect a mailbox in a minute, read it, send from it.

    providers   the built-in table of large providers, by domain and by MX
    discovery   finding the servers from an address, as pure steps over a `Net` port
    net         the real `Net`: DNS, a pinned https fetch, a TLS probe
    message     what a stranger sent, made safe to read: headers, HTML to text, attachments
    session     opening IMAP and SMTP over verified TLS, and the IMAP commands
    cursor      where a mailbox's poll stands, across a changed UIDVALIDITY
    connect     sign in to both, then save: nothing is kept on a failure
    form        the short-lived page a person types a password into for one job
    outbox      `mail_send`: the rules a single message keeps
    tools, api, cli   how agents and people reach it

`runner/workflows/mail.py` holds the mailbox, the transport and `MailService`, which the workflow
engine already uses, so a send is one operation whichever path it took.
"""
