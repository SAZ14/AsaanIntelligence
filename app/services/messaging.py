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
    """Append sent/skipped messages to a JSONL outbox file."""

    def __init__(self, outbox_path: Path, inner: MessageDispatcher | None = None):
        self.outbox_path = outbox_path
        self.inner = inner or ConsoleMessageDispatcher()
        self.outbox_path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, incentive: CustomerIncentive) -> SentMessage:
        result = self.inner.send(incentive)
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
