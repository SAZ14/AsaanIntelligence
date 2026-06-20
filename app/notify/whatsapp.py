"""Twilio WhatsApp notifier with typed, actionable error classification.

The Twilio SDK is imported lazily so this module (and its error taxonomy) can be
imported and unit-tested without the dependency installed or a network present.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from app.notify.config import WhatsAppSettings


class ErrorCategory(str, Enum):
    """High-level cause buckets, mapped to a concrete operator action."""

    AUTH = "auth"                    # bad SID/token
    SANDBOX_OPTIN = "sandbox_optin"  # recipient hasn't joined the sandbox
    SESSION_WINDOW = "session_window"  # outside 24h window; needs a template
    RECIPIENT = "recipient"          # number invalid / not on WhatsApp
    NETWORK = "network"              # could not reach Twilio
    CODE = "code"                    # malformed request / our bug
    UNKNOWN = "unknown"


# Twilio error code -> category. See twilio.com/docs/api/errors.
_CATEGORY_BY_CODE: dict[int, ErrorCategory] = {
    20003: ErrorCategory.AUTH,            # Authentication failed
    63007: ErrorCategory.SANDBOX_OPTIN,   # From not a valid/enabled WhatsApp sender
    63015: ErrorCategory.SANDBOX_OPTIN,   # Sandbox can only message numbers that joined
    63016: ErrorCategory.SESSION_WINDOW,  # Freeform outside 24h window — use a template
    63018: ErrorCategory.SESSION_WINDOW,  # Rate/template related window error
    63003: ErrorCategory.RECIPIENT,       # Channel could not find To address
    63013: ErrorCategory.RECIPIENT,       # To address not a WhatsApp user / blocked
    63024: ErrorCategory.CODE,            # Invalid message body / parameters
    21211: ErrorCategory.CODE,            # Invalid 'To' phone number
}

_ACTION_BY_CATEGORY: dict[ErrorCategory, str] = {
    ErrorCategory.AUTH: "Check TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN.",
    ErrorCategory.SANDBOX_OPTIN: (
        "Recipient must join the WhatsApp Sandbox: from their WhatsApp send the "
        "'join <code>' phrase (Console > Messaging > Try it out) to the sandbox "
        "number, then retry. Or move to a registered production sender."
    ),
    ErrorCategory.SESSION_WINDOW: (
        "Outside the 24h customer-service window — send an approved Message "
        "Template (use send_template) instead of freeform text."
    ),
    ErrorCategory.RECIPIENT: "Verify the recipient number is correct and on WhatsApp.",
    ErrorCategory.NETWORK: "Could not reach api.twilio.com — check network egress allowlist.",
    ErrorCategory.CODE: "Request was malformed — inspect the message parameters.",
    ErrorCategory.UNKNOWN: "Unrecognized failure — inspect the raw Twilio error.",
}


def classify_code(code: int | None) -> ErrorCategory:
    if code is None:
        return ErrorCategory.UNKNOWN
    return _CATEGORY_BY_CODE.get(int(code), ErrorCategory.UNKNOWN)


class WhatsAppSendError(RuntimeError):
    """A send that failed, tagged with a category and a remediation hint."""

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory,
        code: int | None = None,
        http_status: int | None = None,
        message_sid: str | None = None,
    ) -> None:
        self.category = category
        self.code = code
        self.http_status = http_status
        self.message_sid = message_sid
        self.action = _ACTION_BY_CATEGORY.get(category, "")
        super().__init__(
            f"{message} [category={category.value} code={code} "
            f"http_status={http_status} sid={message_sid}] {self.action}".strip()
        )


@dataclass
class SendResult:
    sid: str | None
    status: str
    error_code: int | None = None
    error_message: str | None = None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        # 'failed'/'undelivered' are terminal failures even with a sid.
        return self.status not in ("failed", "undelivered") and self.error_code is None


# Terminal Twilio message statuses.
_TERMINAL = {"delivered", "read", "failed", "undelivered"}


class WhatsAppNotifier:
    """Sends WhatsApp messages via Twilio. Inject ``client`` in tests."""

    def __init__(self, settings: WhatsAppSettings, client=None) -> None:
        self.settings = settings
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from twilio.rest import Client  # lazy: avoid hard dep at import time

            self._client = Client(self.settings.account_sid, self.settings.auth_token)
        return self._client

    # ── sending ──

    def send_text(self, body: str) -> SendResult:
        """Send a freeform WhatsApp text. Honors ``dry_run``."""
        return self._create(dict(from_=self.settings.sender, to=self.settings.recipient, body=body))

    def send_template(self, content_sid: str, variables: dict[str, str] | None = None) -> SendResult:
        """Send an approved template (required outside the 24h session window)."""
        import json

        params = dict(from_=self.settings.sender, to=self.settings.recipient, content_sid=content_sid)
        if variables:
            params["content_variables"] = json.dumps(variables)
        return self._create(params)

    def _create(self, params: dict) -> SendResult:
        if self.settings.dry_run:
            return SendResult(sid=None, status="dry_run", dry_run=True)

        from twilio.base.exceptions import TwilioRestException

        try:
            msg = self.client.messages.create(**params)
        except TwilioRestException as e:
            raise WhatsAppSendError(
                e.msg or "Twilio rejected the request",
                category=classify_code(e.code),
                code=e.code,
                http_status=e.status,
            ) from e
        except Exception as e:  # connectivity / DNS / TLS — never reached Twilio
            raise WhatsAppSendError(
                str(e) or e.__class__.__name__,
                category=ErrorCategory.NETWORK,
            ) from e

        return SendResult(
            sid=msg.sid,
            status=msg.status,
            error_code=msg.error_code,
            error_message=msg.error_message,
        )

    # ── confirmation ──

    def fetch_status(self, sid: str) -> SendResult:
        msg = self.client.messages(sid).fetch()
        return SendResult(
            sid=msg.sid,
            status=msg.status,
            error_code=msg.error_code,
            error_message=msg.error_message,
        )

    def send_and_confirm(
        self,
        body: str,
        *,
        attempts: int = 6,
        interval_s: float = 5.0,
    ) -> SendResult:
        """Send, then poll until a terminal status. Raise on terminal failure.

        In dry-run mode returns immediately without contacting Twilio.
        """
        result = self.send_text(body)
        if result.dry_run:
            return result

        for i in range(attempts):
            if result.status in _TERMINAL:
                break
            if i:
                time.sleep(interval_s)
            result = self.fetch_status(result.sid)

        if result.status in ("failed", "undelivered") or result.error_code:
            raise WhatsAppSendError(
                result.error_message or f"Message {result.status}",
                category=classify_code(result.error_code),
                code=result.error_code,
                message_sid=result.sid,
            )
        return result
