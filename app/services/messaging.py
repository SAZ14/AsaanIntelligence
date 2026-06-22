"""Message dispatch — console, file outbox, and Twilio WhatsApp."""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.agents.customer import CustomerIncentive

# Default outbox rotation policy, shared by the API dispatcher and approve_and_send.
DEFAULT_OUTBOX_MAX_BYTES = 5_000_000
DEFAULT_OUTBOX_BACKUP_COUNT = 3


@dataclass
class SentMessage:
    customer_ref: str
    display_name: str
    channel: str
    phone: str
    message: str
    incentive_type: str
    reward_text: str
    sent_at: str = ""
    status: str = "sent"  # sent | failed | skipped
    error: str = ""
    provider_id: str = ""


@dataclass
class DispatchReport:
    sent: list[SentMessage] = field(default_factory=list)
    skipped: list[SentMessage] = field(default_factory=list)
    failed: list[SentMessage] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.sent) + len(self.skipped) + len(self.failed)


class MessageDispatcher(ABC):
    @abstractmethod
    def send(self, incentive: CustomerIncentive) -> SentMessage:
        ...


def normalize_phone(phone: str) -> str:
    """Normalize to E.164-ish format for PK numbers."""
    p = re.sub(r"[\s\-()]", "", phone.strip())
    if p.startswith("00"):
        p = "+" + p[2:]
    if p.startswith("0") and len(p) == 11:
        p = "+92" + p[1:]
    if p.startswith("92") and not p.startswith("+"):
        p = "+" + p
    if not p.startswith("+"):
        p = "+" + p
    return p


def whatsapp_address(phone: str) -> str:
    normalized = normalize_phone(phone)
    if normalized.startswith("whatsapp:"):
        return normalized
    return f"whatsapp:{normalized}"


def parse_twilio_whatsapp_phone(raw: str) -> str:
    """Strip Twilio whatsapp: prefix and normalize to E.164."""
    value = raw.strip()
    if value.lower().startswith("whatsapp:"):
        value = value.split(":", 1)[1]
    return normalize_phone(value)


def twilio_whatsapp_digits(from_number: str | None = None) -> str:
    """Digits-only WhatsApp number for wa.me links (no + prefix)."""
    raw = from_number or os.environ.get("TWILIO_WHATSAPP_FROM", "")
    if not raw:
        return ""
    return parse_twilio_whatsapp_phone(raw).lstrip("+")


def send_whatsapp_text(
    to_phone: str,
    body: str,
    *,
    use_twilio: bool | None = None,
) -> str:
    """Send a plain WhatsApp text reply. Returns provider message sid or empty."""
    if use_twilio is None:
        use_twilio = bool(
            os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN"),
        )
    to_addr = whatsapp_address(parse_twilio_whatsapp_phone(to_phone))
    if not use_twilio:
        print(f"[WHATSAPP AGENT] → {to_addr}\n  {body}\n")
        return ""
    from twilio.rest import Client

    account_sid = os.environ["TWILIO_ACCOUNT_SID"]
    auth_token = os.environ["TWILIO_AUTH_TOKEN"]
    from_number = os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
    if not from_number.startswith("whatsapp:"):
        from_number = f"whatsapp:{from_number}"
    client = Client(account_sid, auth_token)
    msg = client.messages.create(body=body, from_=from_number, to=to_addr)
    return msg.sid


class ConsoleMessageDispatcher(MessageDispatcher):
    """Log messages to stdout — for dev and CLI demos."""

    def send(self, incentive: CustomerIncentive) -> SentMessage:
        if not incentive.phone:
            return SentMessage(
                customer_ref=incentive.customer_ref,
                display_name=incentive.display_name,
                channel=incentive.channel,
                phone="",
                message=incentive.message,
                incentive_type=incentive.incentive_type,
                reward_text=incentive.reward_text,
                sent_at=datetime.now(timezone.utc).isoformat(),
                status="skipped",
                error="no phone number",
            )
        print(f"[{incentive.channel.upper()}] → {incentive.phone} ({incentive.display_name})")
        print(f"  {incentive.message}\n")
        return SentMessage(
            customer_ref=incentive.customer_ref,
            display_name=incentive.display_name,
            channel=incentive.channel,
            phone=incentive.phone,
            message=incentive.message,
            incentive_type=incentive.incentive_type,
            reward_text=incentive.reward_text,
            sent_at=datetime.now(timezone.utc).isoformat(),
            status="sent",
        )


class TwilioWhatsAppDispatcher(MessageDispatcher):
    """Send WhatsApp messages via Twilio."""

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        from_number: str | None = None,
    ):
        from twilio.rest import Client

        self.account_sid = account_sid or os.environ["TWILIO_ACCOUNT_SID"]
        self.auth_token = auth_token or os.environ["TWILIO_AUTH_TOKEN"]
        self.from_number = from_number or os.environ.get(
            "TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886",
        )
        if not self.from_number.startswith("whatsapp:"):
            self.from_number = f"whatsapp:{self.from_number}"
        self.client = Client(self.account_sid, self.auth_token)

    def send(self, incentive: CustomerIncentive) -> SentMessage:
        base = SentMessage(
            customer_ref=incentive.customer_ref,
            display_name=incentive.display_name,
            channel=incentive.channel,
            phone=incentive.phone,
            message=incentive.message,
            incentive_type=incentive.incentive_type,
            reward_text=incentive.reward_text,
            sent_at=datetime.now(timezone.utc).isoformat(),
        )
        if not incentive.phone:
            base.status = "skipped"
            base.error = "no phone number"
            return base
        if incentive.channel != "whatsapp":
            base.status = "skipped"
            base.error = f"Twilio dispatcher only supports whatsapp, got {incentive.channel}"
            return base
        try:
            msg = self.client.messages.create(
                body=incentive.message,
                from_=self.from_number,
                to=whatsapp_address(incentive.phone),
            )
            base.status = "sent"
            base.provider_id = msg.sid
            return base
        except Exception as e:
            base.status = "failed"
            base.error = str(e)
            return base


class FileOutboxDispatcher(MessageDispatcher):
    """Append sent/skipped messages to a JSONL outbox file.

    When ``max_bytes`` is set, the active file is rotated logrotate-style once it
    grows past that size (``outbox.jsonl`` → ``outbox.1.jsonl`` → ...), keeping at
    most ``backup_count`` backups. ``max_bytes=0`` disables rotation. Note that
    ``load_comms_history`` reads only the active file, so callers that rely on it
    (history view, send-cooldown dedup) see only post-rotation entries — acceptable
    at the default multi-MB threshold (tens of thousands of messages).
    """

    def __init__(
        self,
        outbox_path: Path,
        inner: MessageDispatcher | None = None,
        *,
        max_bytes: int = 0,
        backup_count: int = 3,
    ):
        self.outbox_path = outbox_path
        self.inner = inner or ConsoleMessageDispatcher()
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)

    def _rotate_if_needed(self) -> None:
        if self.max_bytes <= 0 or not self.outbox_path.exists():
            return
        if self.outbox_path.stat().st_size < self.max_bytes:
            return
        # Shift backups: .(n-1) → .n, dropping anything beyond backup_count.
        for i in range(self.backup_count, 0, -1):
            src = (self.outbox_path.with_suffix(f".{i - 1}.jsonl")
                   if i > 1 else self.outbox_path)
            dst = self.outbox_path.with_suffix(f".{i}.jsonl")
            if not src.exists():
                continue
            if i == self.backup_count and dst.exists():
                dst.unlink()
            src.rename(dst)

    def send(self, incentive: CustomerIncentive) -> SentMessage:
        result = self.inner.send(incentive)
        self._rotate_if_needed()
        with open(self.outbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
        return result


def get_dispatcher(use_twilio: bool | None = None) -> MessageDispatcher:
    """Return Twilio dispatcher when credentials are configured, else console."""
    if use_twilio is None:
        use_twilio = bool(
            os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN"),
        )
    if use_twilio:
        return TwilioWhatsAppDispatcher()
    return ConsoleMessageDispatcher()


def dispatch_incentives(
    incentives: list[CustomerIncentive],
    dispatcher: MessageDispatcher | None = None,
    *,
    require_phone: bool = True,
    limit: int | None = None,
) -> DispatchReport:
    """Send a batch of incentive messages via the configured dispatcher."""
    disp = dispatcher or get_dispatcher()
    report = DispatchReport()
    batch = sorted(incentives, key=lambda i: i.priority)
    if limit is not None:
        batch = batch[:limit]

    for inc in batch:
        if require_phone and not inc.phone:
            report.skipped.append(SentMessage(
                customer_ref=inc.customer_ref,
                display_name=inc.display_name,
                channel=inc.channel,
                phone="",
                message=inc.message,
                incentive_type=inc.incentive_type,
                reward_text=inc.reward_text,
                sent_at=datetime.now(timezone.utc).isoformat(),
                status="skipped",
                error="no phone number",
            ))
            continue
        result = disp.send(inc)
        if result.status == "sent":
            report.sent.append(result)
        elif result.status == "skipped":
            report.skipped.append(result)
        else:
            report.failed.append(result)

    return report
