"""Where a notification actually goes.

Four transports and a rule about all of them: **a channel that is not
configured does not exist**, and asking for one that does not exist is a
configuration error raised at startup, not a send that quietly goes nowhere.
The failure this whole package exists to prevent is "nobody was told and
nobody knew", so a channel that swallows is worse than no channel.

`log` is always available and is the default. On a laptop, in CI, and in any
deployment where SMTP has not been set up yet, notifications are written to the
structured log with every field intact. That is a real delivery to a real place
somebody can grep -- it is not a pretend send, and the notification row records
`channel="log"` so nobody later mistakes it for an email.

Nothing here retries. A channel reports what happened and the caller records
it; retry policy belongs with the record, not with the socket.
"""
from __future__ import annotations

import json
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import ClassVar, Protocol

import httpx

from app.config import get_settings
from app.observability.logs import get_logger

log = get_logger("merchantops.notify")


class DeliveryRefused(Exception):
    """The channel declined on purpose. Not an outage -- SUPPRESSED, not FAILED."""


@dataclass(frozen=True)
class Message:
    """What a channel is asked to deliver. Rendered already: a channel decides
    how to put it on a wire, never what it says."""
    recipient: str
    title: str
    body: str
    severity: str
    kind: str
    subject_type: str
    subject_id: str
    correlation_id: str | None = None


class Channel(Protocol):
    name: str

    def send(self, message: Message) -> None:
        """Deliver, or raise. Returning is the only success signal."""


# --------------------------------------------------------------------------
class LogChannel:
    """Always available. Writes one structured line per notification.

    Deliberately not a no-op. The line carries the recipient, the subject and
    the body, so a deployment running on `log` can still answer "was the
    approver told, and what did it say?" -- which is the question, and the one
    a silently-dropped notification cannot answer.
    """
    name = "log"

    def send(self, message: Message) -> None:
        log.info("notification", extra={"notification": {
            "kind": message.kind, "severity": message.severity,
            "recipient": message.recipient, "title": message.title,
            "subject": f"{message.subject_type}:{message.subject_id}",
            "body": message.body,
            "correlation_id": message.correlation_id,
        }})


# --------------------------------------------------------------------------
class EmailChannel:
    """SMTP. Configured or absent -- there is no half-configured.

    STARTTLS unless the port is the implicit-TLS one. Credentials are optional
    because a relay inside a VPC often has none, and requiring them would push
    somebody toward putting the whole thing on `log` instead.
    """
    name = "email"

    def __init__(self, host: str, port: int, sender: str,
                 username: str | None, password: str | None, timeout: float):
        self._host, self._port, self._sender = host, port, sender
        self._username, self._password = username, password
        self._timeout = timeout

    def send(self, message: Message) -> None:
        msg = EmailMessage()
        msg["From"] = self._sender
        msg["To"] = message.recipient
        msg["Subject"] = message.title
        # So a mail client can thread an approval's chase-ups with its request
        # instead of showing three unrelated messages.
        msg["References"] = f"<{message.subject_type}.{message.subject_id}@merchantops>"
        msg.set_content(message.body)

        if self._port == 465:
            smtp = smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout,
                                    context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        with smtp:
            if self._port != 465:
                try:
                    smtp.starttls(context=ssl.create_default_context())
                except smtplib.SMTPNotSupportedError:
                    # A relay with no TLS is a decision somebody made about
                    # their own network. Recorded, not overridden.
                    log.warning("smtp_no_starttls", extra={"host": self._host})
            if self._username:
                smtp.login(self._username, self._password or "")
            smtp.send_message(msg)


# --------------------------------------------------------------------------
class SlackChannel:
    """A Slack incoming webhook.

    The webhook URL *is* the credential and it names the channel, so `recipient`
    here is informational -- it goes in the text rather than the routing. That
    is stated because a reader who assumes otherwise would expect per-person
    Slack delivery and get a shared channel.
    """
    name = "slack"

    _EMOJI: ClassVar[dict[str, str]] = {
        "CRITICAL": ":rotating_light:", "WARNING": ":warning:",
        "INFO": ":information_source:"}

    def __init__(self, webhook_url: str, timeout: float):
        self._url, self._timeout = webhook_url, timeout

    def send(self, message: Message) -> None:
        emoji = self._EMOJI.get(message.severity, "")
        payload = {
            "text": f"{emoji} *{message.title}*",
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn",
                                             "text": f"{emoji} *{message.title}*"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": message.body}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text":
                    f"`{message.subject_type}:{message.subject_id}` · for {message.recipient}"}]},
            ],
        }
        r = httpx.post(self._url, json=payload, timeout=self._timeout)
        r.raise_for_status()


# --------------------------------------------------------------------------
class WebhookChannel:
    """A JSON POST to a URL the customer owns.

    The escape hatch for an enterprise that wants notifications in their own
    system -- PagerDuty, Opsgenie, a ticket queue -- without us integrating with
    each one. The body is the notification's fields, not a rendered email, so
    the receiver can route on them.
    """
    name = "webhook"

    def __init__(self, url: str, timeout: float, secret: str | None = None):
        self._url, self._timeout, self._secret = url, timeout, secret

    def send(self, message: Message) -> None:
        body = {
            "kind": message.kind, "severity": message.severity,
            "title": message.title, "body": message.body,
            "recipient": message.recipient,
            "subject": {"type": message.subject_type, "id": message.subject_id},
            "correlation_id": message.correlation_id,
        }
        headers = {"Content-Type": "application/json"}
        if self._secret:
            # Same shape as the Razorpay webhook we verify inbound: HMAC over
            # the exact bytes sent, so the receiver can check we sent it.
            import hashlib
            import hmac
            raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            headers["X-MerchantOps-Signature"] = hmac.new(
                self._secret.encode(), raw, hashlib.sha256).hexdigest()
            r = httpx.post(self._url, content=raw, headers=headers, timeout=self._timeout)
        else:
            r = httpx.post(self._url, json=body, headers=headers, timeout=self._timeout)
        r.raise_for_status()


# --------------------------------------------------------------------------
class UnconfiguredChannel(ValueError):
    """Asked for a channel this deployment has not set up."""


def build_channels() -> dict[str, Channel]:
    """Every channel this deployment can actually use.

    Built from settings each call rather than cached at import, so a test can
    change the configuration and a deployment can be reconfigured by restart
    without a stale transport surviving in a module global.
    """
    s = get_settings()
    channels: dict[str, Channel] = {"log": LogChannel()}

    if s.smtp_host and s.notify_email_from:
        channels["email"] = EmailChannel(
            s.smtp_host, s.smtp_port, s.notify_email_from,
            s.smtp_username, s.smtp_password, s.notify_timeout_seconds)
    if s.slack_webhook_url:
        channels["slack"] = SlackChannel(s.slack_webhook_url, s.notify_timeout_seconds)
    if s.notify_webhook_url:
        channels["webhook"] = WebhookChannel(
            s.notify_webhook_url, s.notify_timeout_seconds, s.notify_webhook_secret)
    return channels


def active_channel_names() -> list[str]:
    """What `/health` reports. A deployment that thinks it is emailing and is
    not should be able to find that out without sending a test approval."""
    return sorted(build_channels())
