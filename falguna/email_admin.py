"""TTT Digital Workforce v1 -- Email/Admin foundation (Section 7, Pass B).

Same posture as `falguna/outbound.py`'s `OutreachService`: drafting,
classifying, and summarizing an email is a real, verifiable local action, so
a draft always completes; actually sending one to a real recipient is not --
there is no send adapter here at all in v1, real or simulated. A draft
always escalates to Needs Aryan before it could ever be sent, and
`mark_sent` only ever records that the owner sent it themselves outside this
system. Classification reuses `conversations.classify_intent` rather than a
second, diverging rule set for the same problem.
"""

import imaplib
import os
import smtplib
import ssl
from email.message import EmailMessage as _StdEmailMessage
from email.parser import BytesParser
from email.policy import default as _email_policy
from email.utils import make_msgid as _make_msgid
from typing import Any, Dict, List, Optional

from .audit import AuditLog
from .conversations import classify_intent
from .store import StateStore, utcnow

EMAIL_STATUSES = {"PREPARED", "APPROVED", "SENT", "RECEIVED"}


class EmailError(ValueError):
    pass


def _env(name: str) -> Optional[str]:
    """Reads exactly one configuration value from the process environment
    (or whatever secret-configuration mechanism populates it) -- never from
    a file, a DB row, or a hardcoded default. Empty string counts as unset.
    This is the only place any adapter below reads configuration from, and
    nothing in this module ever writes an env value to a log, an audit
    entry, a report, or the database."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


class EmailProvider:
    """Production-ready, provider-independent send/receive boundary for
    future official @twentytwotechnologies.com mailboxes (TTT Communications
    V2, Milestone 1). A concrete adapter (SMTP, IMAP, a provider API, a
    future TTT Mail service) implements the methods below; nothing else in
    this codebase changes when a real provider is wired in later. No
    adapter here hardcodes a specific vendor (e.g. Titan) -- every adapter
    reads its own host/credentials from environment/secret configuration
    via `_env()`, and no credential is ever stored in source, the database,
    logs, reports, or screenshots."""

    def provider_name(self) -> str:
        return type(self).__name__

    def capabilities(self) -> Dict[str, bool]:
        """What this adapter class structurally implements once configured
        -- not whether it's configured right now. Every key defaults to
        False here so a new adapter must explicitly claim a capability
        rather than silently inheriting one it doesn't actually support."""
        return {
            "send": False, "poll_inbound": False, "delivery_status": False,
            "attachments": False, "cc_bcc": False, "reply_to": False,
        }

    def is_configured(self) -> bool:
        raise NotImplementedError

    def validate_configuration(self) -> Dict[str, Any]:
        """Checks whatever configuration this adapter needs (all read via
        `_env()`) without ever including a raw secret value in the result.
        Returns {"valid": bool, "errors": [str, ...]}."""
        raise NotImplementedError

    def health_check(self) -> Dict[str, Any]:
        """A side-effect-free reachability check (e.g. "can this adapter
        even attempt to authenticate right now"). Returns
        {"healthy": bool, "detail": str}. Never sends or receives a real
        message as part of this check."""
        raise NotImplementedError

    def send(
        self, to_address: str, subject: str, body: str, from_address: Optional[str] = None,
        cc: Optional[List[str]] = None, bcc: Optional[List[str]] = None, reply_to: Optional[str] = None,
        thread_id: Optional[str] = None, in_reply_to_message_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """On success returns delivery evidence:
        {"status": "SENT", "provider_message_id": str, "thread_id": Optional[str],
         "failure_reason": None}.
        On failure, raises EmailError with a human-readable, secret-free
        failure reason -- callers never see a partial/ambiguous result."""
        raise NotImplementedError

    def poll_inbound(self, since: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns metadata for up to `limit` messages received since the
        ISO-8601 timestamp `since` (None = everything currently visible).
        Each item: {"provider_message_id", "thread_id", "from_address",
        "to_address", "subject", "body", "received_at",
        "in_reply_to_message_id", "attachments": [...]}. Never invents a
        message -- an empty list means genuinely nothing new, and an
        adapter that cannot poll at all raises EmailError instead of
        silently returning []."""
        raise NotImplementedError


class NullEmailProvider(EmailProvider):
    """The only provider wired in as a default anywhere in this codebase.
    Deliberately incapable of sending or receiving anything: every
    capability is False, `is_configured()` is always False, and every
    action method raises. This is what keeps `mark_sent` a manual,
    owner-performed action -- this code can never send or poll email on its
    own -- and it never assumes an unprovisioned
    @twentytwotechnologies.com mailbox is actually live."""

    def capabilities(self) -> Dict[str, bool]:
        return {
            "send": False, "poll_inbound": False, "delivery_status": False,
            "attachments": False, "cc_bcc": False, "reply_to": False,
        }

    def is_configured(self) -> bool:
        return False

    def validate_configuration(self) -> Dict[str, Any]:
        return {"valid": False, "errors": ["NullEmailProvider is the safe default and is never configured -- wire in a real adapter to enable sending."]}

    def health_check(self) -> Dict[str, Any]:
        return {"healthy": False, "detail": "no email provider is configured"}

    def send(self, to_address: str, subject: str, body: str, from_address: Optional[str] = None,
              cc: Optional[List[str]] = None, bcc: Optional[List[str]] = None, reply_to: Optional[str] = None,
              thread_id: Optional[str] = None, in_reply_to_message_id: Optional[str] = None,
              attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        raise EmailError(
            "no email provider is configured -- this system cannot send email on its own. "
            "Send this message yourself, then call mark_sent()."
        )

    def poll_inbound(self, since: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        raise EmailError("no email provider is configured -- this system cannot receive/poll email on its own.")


# Planned departmental mailboxes -- addresses only, never asserted as live,
# provisioned inboxes. A department shows here once it has a planned
# identity in the rest of the app (see falguna.comms.DEPARTMENTS); treat as
# operational only once a human confirms it and wires a real EmailProvider
# to it below.
DEPARTMENT_MAILBOXES: Dict[str, str] = {
    "general": "hello@twentytwotechnologies.com",
    "sales": "sales@twentytwotechnologies.com",
    "projects": "projects@twentytwotechnologies.com",
    "support": "support@twentytwotechnologies.com",
    "billing": "billing@twentytwotechnologies.com",
    "careers": "careers@twentytwotechnologies.com",
    "media": "media@twentytwotechnologies.com",
}


class SMTPEmailProvider(EmailProvider):
    """Send-only adapter over stdlib smtplib. Not hardcoded to any vendor
    (Titan or otherwise) -- every value comes from the environment, and
    this adapter is simply unconfigured (is_configured() False) until all
    required variables are present. Never wired in as a default; a caller
    must explicitly construct and pass this to EmailStore(provider=...).

    Required env vars: SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD.
    Optional: SMTP_USE_TLS ("true"/"false", default "true"),
    SMTP_DEFAULT_FROM_ADDRESS."""

    _REQUIRED = ["SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD"]

    def capabilities(self) -> Dict[str, bool]:
        return {
            "send": True, "poll_inbound": False, "delivery_status": False,
            "attachments": True, "cc_bcc": True, "reply_to": True,
        }

    def _config_errors(self) -> List[str]:
        return [f"{name} is not set" for name in self._REQUIRED if not _env(name)]

    def is_configured(self) -> bool:
        return not self._config_errors()

    def validate_configuration(self) -> Dict[str, Any]:
        errors = self._config_errors()
        port = _env("SMTP_PORT")
        if port and not port.isdigit():
            errors.append("SMTP_PORT must be numeric")
        return {"valid": not errors, "errors": errors}

    def health_check(self) -> Dict[str, Any]:
        if not self.is_configured():
            return {"healthy": False, "detail": "SMTP is not configured"}
        try:
            host, port = _env("SMTP_HOST"), int(_env("SMTP_PORT"))
            use_tls = (_env("SMTP_USE_TLS") or "true").lower() != "false"
            with smtplib.SMTP(host, port, timeout=10) as client:
                if use_tls:
                    client.starttls(context=ssl.create_default_context())
                client.login(_env("SMTP_USERNAME"), _env("SMTP_PASSWORD"))
            return {"healthy": True, "detail": "authenticated successfully"}
        except Exception as exc:
            # Never include exc's raw text if it could echo back a password;
            # smtplib exceptions do not, but we keep the message generic
            # regardless so a future smtplib change can't leak one.
            return {"healthy": False, "detail": f"could not connect/authenticate ({type(exc).__name__})"}

    def send(self, to_address: str, subject: str, body: str, from_address: Optional[str] = None,
              cc: Optional[List[str]] = None, bcc: Optional[List[str]] = None, reply_to: Optional[str] = None,
              thread_id: Optional[str] = None, in_reply_to_message_id: Optional[str] = None,
              attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        errors = self._config_errors()
        if errors:
            raise EmailError("SMTP is not configured: " + "; ".join(errors))
        sender = from_address or _env("SMTP_DEFAULT_FROM_ADDRESS") or _env("SMTP_USERNAME")
        msg = _StdEmailMessage()
        msg["Subject"] = subject or ""
        msg["From"] = sender
        msg["To"] = to_address
        msg["Message-Id"] = _make_msgid()
        if cc:
            msg["Cc"] = ", ".join(cc)
        if reply_to:
            msg["Reply-To"] = reply_to
        if in_reply_to_message_id:
            msg["In-Reply-To"] = in_reply_to_message_id
            msg["References"] = in_reply_to_message_id
        msg.set_content(body or "")
        for att in (attachments or []):
            # Attachment content is never embedded here from a raw secret
            # source -- callers pass already-read file bytes, not credentials.
            if att.get("content_bytes") is not None:
                maintype, _, subtype = (att.get("content_type") or "application/octet-stream").partition("/")
                msg.add_attachment(
                    att["content_bytes"], maintype=maintype or "application", subtype=subtype or "octet-stream",
                    filename=att.get("filename") or "attachment",
                )
        recipients = [to_address] + list(cc or []) + list(bcc or [])
        try:
            host, port = _env("SMTP_HOST"), int(_env("SMTP_PORT"))
            use_tls = (_env("SMTP_USE_TLS") or "true").lower() != "false"
            with smtplib.SMTP(host, port, timeout=20) as client:
                if use_tls:
                    client.starttls(context=ssl.create_default_context())
                client.login(_env("SMTP_USERNAME"), _env("SMTP_PASSWORD"))
                client.send_message(msg, from_addr=sender, to_addrs=recipients)
        except Exception as exc:
            raise EmailError(f"SMTP send failed ({type(exc).__name__})") from exc
        return {
            "status": "SENT", "provider_message_id": msg.get("Message-Id"),
            "thread_id": thread_id, "failure_reason": None,
        }

    def poll_inbound(self, since: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        raise EmailError("SMTPEmailProvider is send-only -- pair it with IMAPEmailProvider (or use SMTPIMAPEmailProvider) to poll inbound mail.")


class IMAPEmailProvider(EmailProvider):
    """Poll-only adapter over stdlib imaplib. Required env vars: IMAP_HOST,
    IMAP_PORT, IMAP_USERNAME, IMAP_PASSWORD. Optional: IMAP_USE_SSL
    ("true"/"false", default "true"), IMAP_MAILBOX_FOLDER (default
    "INBOX"). Never hardcodes a vendor host."""

    _REQUIRED = ["IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD"]

    def capabilities(self) -> Dict[str, bool]:
        return {
            "send": False, "poll_inbound": True, "delivery_status": False,
            "attachments": True, "cc_bcc": False, "reply_to": False,
        }

    def _config_errors(self) -> List[str]:
        return [f"{name} is not set" for name in self._REQUIRED if not _env(name)]

    def is_configured(self) -> bool:
        return not self._config_errors()

    def validate_configuration(self) -> Dict[str, Any]:
        errors = self._config_errors()
        port = _env("IMAP_PORT")
        if port and not port.isdigit():
            errors.append("IMAP_PORT must be numeric")
        return {"valid": not errors, "errors": errors}

    def _connect(self) -> "imaplib.IMAP4":
        host, port = _env("IMAP_HOST"), int(_env("IMAP_PORT"))
        use_ssl = (_env("IMAP_USE_SSL") or "true").lower() != "false"
        client = imaplib.IMAP4_SSL(host, port, timeout=10) if use_ssl else imaplib.IMAP4(host, port, timeout=10)
        client.login(_env("IMAP_USERNAME"), _env("IMAP_PASSWORD"))
        return client

    def health_check(self) -> Dict[str, Any]:
        if not self.is_configured():
            return {"healthy": False, "detail": "IMAP is not configured"}
        try:
            client = self._connect()
            client.logout()
            return {"healthy": True, "detail": "authenticated successfully"}
        except Exception as exc:
            return {"healthy": False, "detail": f"could not connect/authenticate ({type(exc).__name__})"}

    def send(self, to_address: str, subject: str, body: str, from_address: Optional[str] = None,
              cc: Optional[List[str]] = None, bcc: Optional[List[str]] = None, reply_to: Optional[str] = None,
              thread_id: Optional[str] = None, in_reply_to_message_id: Optional[str] = None,
              attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        raise EmailError("IMAPEmailProvider is poll-only -- pair it with SMTPEmailProvider (or use SMTPIMAPEmailProvider) to send.")

    def poll_inbound(self, since: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        errors = self._config_errors()
        if errors:
            raise EmailError("IMAP is not configured: " + "; ".join(errors))
        folder = _env("IMAP_MAILBOX_FOLDER") or "INBOX"
        try:
            client = self._connect()
            try:
                client.select(folder)
                criteria = "ALL"
                if since:
                    # IMAP SINCE is date-granularity only -- callers must
                    # de-duplicate by provider_message_id, which they do
                    # (Milestone 3's ingestion handles retries/duplicates).
                    imap_date = since[:10].replace("-", "-")
                    criteria = f'(SINCE "{imap_date}")'
                status, data = client.search(None, criteria)
                if status != "OK":
                    raise EmailError("IMAP search failed")
                message_nums = data[0].split()[:limit]
                results: List[Dict[str, Any]] = []
                for num in message_nums:
                    status, msg_data = client.fetch(num, "(RFC822)")
                    if status != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    parsed = BytesParser(policy=_email_policy).parsebytes(raw)
                    body_part = parsed.get_body(preferencelist=("plain", "html"))
                    attachments_meta = [
                        {"filename": part.get_filename(), "content_type": part.get_content_type()}
                        for part in parsed.iter_attachments()
                    ]
                    results.append({
                        "provider_message_id": parsed.get("Message-Id"),
                        "thread_id": parsed.get("References") or parsed.get("In-Reply-To"),
                        "from_address": parsed.get("From"),
                        "to_address": parsed.get("To"),
                        "subject": parsed.get("Subject") or "",
                        "body": body_part.get_content() if body_part else "",
                        "received_at": parsed.get("Date"),
                        "in_reply_to_message_id": parsed.get("In-Reply-To"),
                        "attachments": attachments_meta,
                    })
                return results
            finally:
                client.logout()
        except EmailError:
            raise
        except Exception as exc:
            raise EmailError(f"IMAP poll failed ({type(exc).__name__})") from exc


class SMTPIMAPEmailProvider(EmailProvider):
    """Composes SMTPEmailProvider + IMAPEmailProvider into a single
    send-and-receive adapter -- the shape a real, fully-provisioned mailbox
    (a future TTT Mail address, or any standard SMTP+IMAP account) needs.
    Configured only when both halves are configured."""

    def __init__(self) -> None:
        self._smtp = SMTPEmailProvider()
        self._imap = IMAPEmailProvider()

    def capabilities(self) -> Dict[str, bool]:
        return {
            "send": True, "poll_inbound": True, "delivery_status": False,
            "attachments": True, "cc_bcc": True, "reply_to": True,
        }

    def is_configured(self) -> bool:
        return self._smtp.is_configured() and self._imap.is_configured()

    def validate_configuration(self) -> Dict[str, Any]:
        smtp_result = self._smtp.validate_configuration()
        imap_result = self._imap.validate_configuration()
        errors = smtp_result["errors"] + imap_result["errors"]
        return {"valid": not errors, "errors": errors}

    def health_check(self) -> Dict[str, Any]:
        smtp_health = self._smtp.health_check()
        imap_health = self._imap.health_check()
        healthy = smtp_health["healthy"] and imap_health["healthy"]
        return {"healthy": healthy, "detail": f"smtp: {smtp_health['detail']}; imap: {imap_health['detail']}"}

    def send(self, *args, **kwargs) -> Dict[str, Any]:
        return self._smtp.send(*args, **kwargs)

    def poll_inbound(self, since: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        return self._imap.poll_inbound(since=since, limit=limit)


class ProviderAPIEmailProvider(EmailProvider):
    """Template for a future HTTP-API-based provider (e.g. a transactional
    email API, or the Gmail API) once TTT actually selects one -- not a
    working integration today. Deliberately never configured: building a
    specific API contract before a provider is chosen would be a guess, not
    a real adapter. To use this path later: subclass this class, implement
    is_configured/validate_configuration/health_check/send/poll_inbound
    against the chosen provider's real API, reading its base URL and API
    key from environment/secret configuration exactly like the adapters
    above, and pass an instance to EmailStore(provider=...)."""

    def capabilities(self) -> Dict[str, bool]:
        return {
            "send": False, "poll_inbound": False, "delivery_status": False,
            "attachments": False, "cc_bcc": False, "reply_to": False,
        }

    def is_configured(self) -> bool:
        return False

    def validate_configuration(self) -> Dict[str, Any]:
        return {"valid": False, "errors": ["no provider API has been selected yet -- subclass ProviderAPIEmailProvider once one is chosen."]}

    def health_check(self) -> Dict[str, Any]:
        return {"healthy": False, "detail": "no provider API has been selected yet"}

    def send(self, to_address: str, subject: str, body: str, from_address: Optional[str] = None,
              cc: Optional[List[str]] = None, bcc: Optional[List[str]] = None, reply_to: Optional[str] = None,
              thread_id: Optional[str] = None, in_reply_to_message_id: Optional[str] = None,
              attachments: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        raise EmailError("no provider API has been selected/implemented yet")

    def poll_inbound(self, since: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        raise EmailError("no provider API has been selected/implemented yet")


def _first_sentence(text: Optional[str]) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    for sep in [". ", "! ", "? ", "\n"]:
        if sep in text:
            return text.split(sep)[0].strip() + sep.strip()
    return text[:160]


class EmailStore:
    def __init__(self, store: StateStore, audit: AuditLog, needs_aryan=None, provider: Optional[EmailProvider] = None):
        self.store = store
        self.audit = audit
        self.needs_aryan = needs_aryan
        # Defaults to a provider that structurally cannot send anything --
        # see NullEmailProvider above. Passing a real provider here is the
        # only way this class's send-readiness ever changes.
        self.provider = provider or NullEmailProvider()

    def provider_status(self) -> Dict[str, Any]:
        """What TTT HQ shows for email-send readiness -- never claims a
        department mailbox is live just because it's in DEPARTMENT_MAILBOXES.
        `validation`/`health` are best-effort: a provider that doesn't
        implement one of those checks (NotImplementedError) shows None
        rather than crashing this status call, and `health` is only
        actually probed when the provider reports itself configured, since
        an unconfigured provider's health is already known."""
        configured = self.provider.is_configured()
        try:
            validation = self.provider.validate_configuration()
        except NotImplementedError:
            validation = None
        health = None
        if configured:
            try:
                health = self.provider.health_check()
            except NotImplementedError:
                health = None
        return {
            "provider": self.provider.provider_name(),
            "configured": configured,
            "capabilities": self.provider.capabilities(),
            "validation": validation,
            "health": health,
            "planned_mailboxes": dict(DEPARTMENT_MAILBOXES),
        }

    def draft(
        self, to_address: str, subject: str, body: str, department: Optional[str] = None,
        thread_id: Optional[str] = None, source_task_id: Optional[str] = None, actor: str = "system",
    ) -> Dict[str, Any]:
        if not body or not body.strip():
            raise EmailError("body is required")
        now = utcnow()
        email_id = self.store.create("wf_email_messages", {
            "department": department, "direction": "OUTBOUND", "to_address": to_address, "from_address": None,
            "subject": subject, "body": body, "intent": classify_intent(body), "status": "PREPARED",
            "needs_aryan_id": None, "thread_id": thread_id, "follow_up_date": None,
            "source_task_id": source_task_id, "actor": actor, "created_at": now, "updated_at": now,
        })
        needs_aryan_id = None
        if self.needs_aryan is not None:
            needs_aryan_id = self.needs_aryan.create_item(
                "workforce_action_approval", f"Email ready for review: {subject or '(no subject)'}",
                "An email has been drafted. Nothing has been sent -- this system has no capability to "
                "send email on its own. Review it and, if it's appropriate to send, send it yourself "
                "and mark it sent.",
                actor=actor, ref_type="wf_email_message", ref_id=email_id,
            )
            self.store.update("wf_email_messages", email_id, needs_aryan_id=needs_aryan_id)
        self.audit.append("WF_EMAIL_DRAFTED", {"email_id": email_id, "to": to_address, "actor": actor})
        return self.get(email_id)

    def record_received(
        self, from_address: str, subject: str, body: str, department: Optional[str] = None,
        thread_id: Optional[str] = None, actor: str = "system",
    ) -> Dict[str, Any]:
        """Logs an email that actually arrived through some real channel
        outside this system -- mirrors ConversationStore.record_inbound.
        This never claims an inbound polling/monitoring capability that
        doesn't exist."""
        if not body or not body.strip():
            raise EmailError("body is required")
        now = utcnow()
        email_id = self.store.create("wf_email_messages", {
            "department": department, "direction": "INBOUND", "to_address": None, "from_address": from_address,
            "subject": subject, "body": body, "intent": classify_intent(body), "status": "RECEIVED",
            "needs_aryan_id": None, "thread_id": thread_id, "follow_up_date": None,
            "source_task_id": None, "actor": actor, "created_at": now, "updated_at": now,
        })
        self.audit.append("WF_EMAIL_RECEIVED", {"email_id": email_id, "from": from_address, "actor": actor})
        return self.get(email_id)

    def summarize(self, email_id: str) -> str:
        email = self._require(email_id)
        return _first_sentence(email["body"])

    def mark_sent(self, email_id: str, actor: str) -> Dict[str, Any]:
        email = self._require(email_id)
        if email["direction"] != "OUTBOUND":
            raise EmailError("only an outbound draft can be marked sent")
        if email["status"] != "PREPARED":
            raise EmailError(f"email is already {email['status']}, not PREPARED")
        if email.get("needs_aryan_id"):
            item = self.store.get("needs_aryan_items", email["needs_aryan_id"])
            if not item or item["status"] != "APPROVED":
                raise EmailError("this email has not been approved yet")
        self.store.update("wf_email_messages", email_id, status="SENT")
        self.audit.append("WF_EMAIL_SENT", {"email_id": email_id, "actor": actor})
        return self.get(email_id)

    def schedule_follow_up(self, email_id: str, follow_up_date: str, actor: str) -> Dict[str, Any]:
        self._require(email_id)
        self.store.update("wf_email_messages", email_id, follow_up_date=follow_up_date)
        self.audit.append("WF_EMAIL_FOLLOWUP_SCHEDULED", {"email_id": email_id, "follow_up_date": follow_up_date, "actor": actor})
        return self.get(email_id)

    def due_follow_ups(self, as_of: Optional[str] = None) -> List[Dict[str, Any]]:
        as_of = (as_of or utcnow())[:10]
        return [
            e for e in self.store.list("wf_email_messages")
            if e.get("follow_up_date") and e["follow_up_date"] <= as_of and e["status"] != "SENT"
        ]

    def _require(self, email_id: str) -> Dict[str, Any]:
        email = self.store.get("wf_email_messages", email_id)
        if not email:
            raise EmailError("email not found")
        return email

    def get(self, email_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get("wf_email_messages", email_id)

    def list(self, department: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if department and status:
            rows = self.store.list("wf_email_messages", "department=? AND status=?", (department, status))
        elif department:
            rows = self.store.list("wf_email_messages", "department=?", (department,))
        elif status:
            rows = self.store.list("wf_email_messages", "status=?", (status,))
        else:
            rows = self.store.list("wf_email_messages")
        return list(reversed(rows))
