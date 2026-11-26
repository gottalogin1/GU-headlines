"""Sending email through an SMTP server (the standard way that programs hand
email to an email service), such as Resend's (smtp.resend.com).

SMTP_HOST, SMTP_PORT, SMTP_USER and SMTP_PASSWORD come from the email
service. MAIL_FROM is the sender, such as "GU Headlines <alerts@example.com>":
the service sends only from an address on a domain that you proved is yours.
Port 465 uses an encrypted connection from the start; other ports start
plain and switch to encryption (STARTTLS). The password never goes over a
connection that is not encrypted.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from .settings import Settings

TIMEOUT = 30.0


class MailError(Exception):
    """The email could not be sent. The message says why, for the log."""


def enabled(settings: Settings) -> bool:
    return bool(settings.smtp_host and sender_address(settings))


def sender_address(settings: Settings) -> str:
    """alerts@example.com from MAIL_FROM "GU Headlines <alerts@example.com>",
    or "" when MAIL_FROM has no address."""
    address = parseaddr(settings.mail_from)[1]
    return address if "@" in address else ""


def message(
    settings: Settings,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    headers: dict[str, str] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender_address(settings).rpartition("@")[2])
    for name, value in (headers or {}).items():
        msg[name] = value
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def send(
    settings: Settings,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """Send one email (plain text, with an HTML copy when `html` is given).
    Raises MailError."""
    if not enabled(settings):
        raise MailError("email is off: SMTP_HOST and MAIL_FROM must both be set")
    msg = message(settings, to, subject, text, html, headers)
    context = ssl.create_default_context()
    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, 465, timeout=TIMEOUT, context=context)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT)
        with server:
            if settings.smtp_port != 465:
                server.starttls(context=context)
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(
            "the email service refused SMTP_USER and SMTP_PASSWORD"
            f" ({exc.smtp_code}). For Resend, SMTP_USER is resend and SMTP_PASSWORD is"
            " an API key"
        ) from exc
    except smtplib.SMTPNotSupportedError as exc:
        raise MailError(
            f"{settings.smtp_host} port {settings.smtp_port} cannot encrypt the connection"
            " (STARTTLS). Use the port that the email service gives for TLS, or 465"
        ) from exc
    except smtplib.SMTPResponseException as exc:
        reason = exc.smtp_error.decode("utf-8", "replace")[:200]
        raise MailError(f"the email service answered {exc.smtp_code}: {reason}") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise MailError("the email service did not accept the address") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(
            f"could not send through {settings.smtp_host} port {settings.smtp_port}:"
            f" {type(exc).__name__}: {exc}"
        ) from exc
