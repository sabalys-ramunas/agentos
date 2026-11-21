"""The built-in table of large providers (docs/EMAIL.md, "Finding the servers", step 1).

Ours, so it is trusted: a person is still shown what it says, but nothing a stranger controls can
put a host here. An MX record can only ever point at one of these rows, so a spoofed one sends a
password to the real provider and no further.
"""

from __future__ import annotations

from dataclasses import dataclass

TLS = "tls"
STARTTLS = "starttls"


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    imap: tuple[str, int, str]
    smtp: tuple[str, int, str]
    domains: tuple[str, ...] = ()
    mx: tuple[str, ...] = ()              # a host, or a parent domain: `.google.com` matches below it
    needs_app_password: bool = False
    help_url: str | None = None
    words: str = ""                       # what a person reads before they type anything
    files_sent: bool = False              # the server keeps its own copy of what SMTP sent
    spaced_password: bool = False         # shown in groups of four, and the spaces are not part of it


GMAIL_WORDS = ("Gmail needs an app password, not your normal password. Turn on 2-Step Verification, then make one at "
               "https://myaccount.google.com/apppasswords. Advanced Protection blocks app passwords.")
WORKSPACE_WORDS = (f"{GMAIL_WORDS} This mailbox is on Google Workspace, where an admin can block IMAP.")

PROVIDERS: tuple[Provider, ...] = (
    Provider("gmail", "Gmail", ("imap.gmail.com", 993, TLS), ("smtp.gmail.com", 465, TLS),
             domains=("gmail.com", "googlemail.com"), needs_app_password=True,
             help_url="https://myaccount.google.com/apppasswords", words=GMAIL_WORDS, files_sent=True,
             spaced_password=True),
    Provider("google-workspace", "Google Workspace", ("imap.gmail.com", 993, TLS), ("smtp.gmail.com", 465, TLS),
             mx=("smtp.google.com", ".google.com", ".googlemail.com"), needs_app_password=True,
             help_url="https://myaccount.google.com/apppasswords", words=WORKSPACE_WORDS, files_sent=True,
             spaced_password=True),
    Provider("icloud", "iCloud", ("imap.mail.me.com", 993, TLS), ("smtp.mail.me.com", 587, STARTTLS),
             domains=("icloud.com", "me.com", "mac.com"), mx=(".mail.icloud.com",), needs_app_password=True,
             help_url="https://account.apple.com",
             words=("iCloud needs an app-specific password. Make one at account.apple.com, under Sign-In and Security. "
                    "It needs two-factor authentication.")),
    Provider("yahoo", "Yahoo", ("imap.mail.yahoo.com", 993, TLS), ("smtp.mail.yahoo.com", 465, TLS),
             domains=("yahoo.com", "ymail.com", "rocketmail.com"), mx=(".yahoodns.net",), needs_app_password=True,
             help_url="https://login.yahoo.com/account/security",
             words="Yahoo needs an app password, not your normal password. Make one under Account Security."),
    Provider("microsoft", "Microsoft 365", ("outlook.office365.com", 993, TLS), ("smtp.office365.com", 587, STARTTLS),
             domains=("outlook.com", "hotmail.com", "live.com", "msn.com"),
             mx=(".mail.protection.outlook.com", ".olc.protection.outlook.com"),
             words=("Microsoft 365 often turns password sign-in off. Signing in that way needs OAuth, which is not here "
                    "yet, so a password may be refused.")),
)


def by_id(provider_id: str | None) -> Provider | None:
    return next((p for p in PROVIDERS if p.id == provider_id), None)


def by_domain(domain: str) -> Provider | None:
    domain = domain.lower().rstrip(".")
    return next((p for p in PROVIDERS if domain in p.domains), None)


def _mx_matches(host: str, pattern: str) -> bool:
    return host.endswith(pattern) and len(host) > len(pattern) if pattern.startswith(".") else host == pattern


def by_mx(hosts: list[str]) -> Provider | None:
    """The row the domain's own mail exchangers point at. A pattern matches on a label boundary, so
    `evil-google.com` is not `.google.com`."""
    for host in hosts:
        name = host.lower().rstrip(".")
        for provider in PROVIDERS:
            if any(_mx_matches(name, p) for p in provider.mx):
                return provider
    return None
