"""Phase 5 Final Client Experience, Section 14 -- WhatsApp Business
architecture.

Mirrors falguna/email_admin.py's EmailProvider boundary exactly, for the
same reason: production-ready, provider-independent send/receive
architecture for one official Twenty Two Technologies WhatsApp Business
account, with nothing in this assignment activating a real account or
sending a real message. `NullWhatsAppProvider` is the only provider wired
in anywhere by default -- every capability False, every action raises --
so this system structurally cannot message a real person on WhatsApp
until a human explicitly configures and wires in a real adapter.

Explicitly out of scope here, per the standing instruction: using Aryan's
personal WhatsApp as the automated business channel, any unofficial
automation/scraping/account hijacking/ToS bypass, and activating a real
WhatsApp Business API account or sending any real message. This module
only defines the architecture a real integration would later plug into.
"""

import os
from typing import Any, Dict, List, Optional

WHATSAPP_MESSAGE_STATUSES = {"PREPARED", "APPROVED", "SENT", "RECEIVED"}


class WhatsAppError(ValueError):
    pass


def _env(name: str) -> Optional[str]:
    """Same convention as email_admin.py's _env(): the only place any
    adapter below reads configuration from, and nothing here ever logs,
    persists, or reports a raw credential value."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


class WhatsAppProvider:
    """Provider-independent send/receive boundary for a future official
    Twenty Two Technologies WhatsApp Business account. A concrete adapter
    (WhatsApp Cloud API, an approved BSP, a future TTT messaging service)
    implements the methods below; nothing else in this codebase changes
    when a real provider is wired in later. No adapter hardcodes a
    specific vendor -- every adapter reads its own credentials from
    environment/secret configuration via `_env()`."""

    def provider_name(self) -> str:
        return type(self).__name__

    def capabilities(self) -> Dict[str, bool]:
        return {
            "send": False, "poll_inbound": False, "delivery_status": False,
            "media": False, "templates": False,
        }

    def is_configured(self) -> bool:
        raise NotImplementedError

    def validate_configuration(self) -> Dict[str, Any]:
        """Returns {"valid": bool, "errors": [str, ...]} without ever
        including a raw secret value."""
        raise NotImplementedError

    def health_check(self) -> Dict[str, Any]:
        """Side-effect-free reachability check. Returns
        {"healthy": bool, "detail": str}. Never sends a real message."""
        raise NotImplementedError

    def send(
        self, to_phone: str, body: str, thread_id: Optional[str] = None,
        in_reply_to_message_id: Optional[str] = None,
        template_name: Optional[str] = None, media: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """On success returns delivery evidence:
        {"status": "SENT", "provider_message_id": str, "thread_id": Optional[str],
         "failure_reason": None}.
        On failure, raises WhatsAppError with a human-readable, secret-free
        failure reason."""
        raise NotImplementedError

    def poll_inbound(self, since: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns metadata for up to `limit` messages received since the
        ISO-8601 timestamp `since`. Each item: {"provider_message_id",
        "thread_id", "from_phone", "to_phone", "body", "received_at",
        "in_reply_to_message_id", "media": [...]}. Never invents a message
        -- an empty list means genuinely nothing new, and an adapter that
        cannot poll at all raises WhatsAppError instead of silently
        returning []."""
        raise NotImplementedError


class NullWhatsAppProvider(WhatsAppProvider):
    """The only provider wired in as a default anywhere in this codebase.
    Deliberately incapable of sending or receiving anything: every
    capability is False, `is_configured()` is always False, and every
    action method raises. This is what keeps WhatsApp a planned,
    architecture-only channel in this assignment -- this code can never
    send or poll a real WhatsApp message on its own."""

    def is_configured(self) -> bool:
        return False

    def validate_configuration(self) -> Dict[str, Any]:
        return {"valid": False, "errors": ["NullWhatsAppProvider is the safe default and is never configured -- wire in a real adapter to enable sending."]}

    def health_check(self) -> Dict[str, Any]:
        return {"healthy": False, "detail": "no WhatsApp provider is configured"}

    def send(self, to_phone: str, body: str, thread_id: Optional[str] = None,
              in_reply_to_message_id: Optional[str] = None,
              template_name: Optional[str] = None, media: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        raise WhatsAppError(
            "no WhatsApp provider is configured -- this system cannot send WhatsApp messages on its own. "
            "This assignment does not authorize activating a real WhatsApp Business account."
        )

    def poll_inbound(self, since: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        raise WhatsAppError("no WhatsApp provider is configured -- this system cannot receive/poll WhatsApp messages on its own.")


class WhatsAppBusinessCloudAPIProvider(WhatsAppProvider):
    """Template for the official WhatsApp Business Cloud API (Meta) once
    Twenty Two Technologies actually provisions one official business
    number -- not a working integration today, and never activated by
    this assignment. To use this path later: implement
    is_configured/validate_configuration/health_check/send/poll_inbound
    against the real Cloud API (phone_number_id, a permanent access
    token, and a verified webhook for inbound messages), reading every
    credential from environment/secret configuration exactly like
    email_admin.py's adapters, then pass an instance to
    resolve_configured_whatsapp_provider()'s precedence chain below."""

    def capabilities(self) -> Dict[str, bool]:
        return {
            "send": False, "poll_inbound": False, "delivery_status": False,
            "media": False, "templates": False,
        }

    def is_configured(self) -> bool:
        return bool(_env("TTT_WHATSAPP_CLOUD_API_TOKEN") and _env("TTT_WHATSAPP_PHONE_NUMBER_ID"))

    def validate_configuration(self) -> Dict[str, Any]:
        return {"valid": False, "errors": ["WhatsApp Business Cloud API integration has not been implemented yet -- this is an architecture template only."]}

    def health_check(self) -> Dict[str, Any]:
        return {"healthy": False, "detail": "WhatsApp Business Cloud API integration has not been implemented yet"}

    def send(self, to_phone: str, body: str, thread_id: Optional[str] = None,
              in_reply_to_message_id: Optional[str] = None,
              template_name: Optional[str] = None, media: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        raise WhatsAppError("WhatsApp Business Cloud API integration has not been implemented/activated yet")

    def poll_inbound(self, since: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        raise WhatsAppError("WhatsApp Business Cloud API integration has not been implemented/activated yet")


def resolve_configured_whatsapp_provider() -> WhatsAppProvider:
    """The one place that decides which real WhatsAppProvider (if any)
    this environment has been configured for -- explicit and opt-in,
    never a silent default, exactly mirroring
    email_admin.resolve_configured_email_provider(). Reads only from the
    environment. In this assignment, TTT_WHATSAPP_CLOUD_API_TOKEN /
    TTT_WHATSAPP_PHONE_NUMBER_ID are never set, so this always returns
    NullWhatsAppProvider -- there is no path in this codebase that
    activates a real WhatsApp account on its own."""
    cloud_api = WhatsAppBusinessCloudAPIProvider()
    if cloud_api.is_configured():
        return cloud_api
    return NullWhatsAppProvider()
