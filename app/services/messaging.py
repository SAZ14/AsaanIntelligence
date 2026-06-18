"""Outbound message dispatch for Customer Agent incentives."""

from __future__ import annotations

import json
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


def dispatch_incentives(
    incentives: list[CustomerIncentive],
    dispatcher: MessageDispatcher | None = None,
    *,
    require_phone: bool = True,
    limit: int | None = None,
) -> DispatchReport:
    """Send a batch of incentive messages via the configured dispatcher."""
    disp = dispatcher or ConsoleMessageDispatcher()
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
