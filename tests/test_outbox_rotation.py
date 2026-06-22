"""Tests for FileOutboxDispatcher size-based rotation."""

from __future__ import annotations

from app.agents.customer import CustomerIncentive
from app.services.messaging import FileOutboxDispatcher


def _incentive(ref: str) -> CustomerIncentive:
    return CustomerIncentive(
        customer_ref=ref,
        display_name="Guest " + ref,
        incentive_type="visit_milestone",
        discount_pct=10,
        reward_text="10% off",
        message="Thanks for visiting!" * 5,
        channel="whatsapp",
        phone="+923001112222",
        priority=3,
        trigger_reason="test",
    )


def test_no_rotation_when_disabled(tmp_path):
    outbox = tmp_path / "out.jsonl"
    disp = FileOutboxDispatcher(outbox, max_bytes=0)
    for i in range(10):
        disp.send(_incentive(f"C{i}"))
    assert outbox.exists()
    assert not (tmp_path / "out.1.jsonl").exists()
    assert len(outbox.read_text().splitlines()) == 10


def test_rotation_creates_backup(tmp_path):
    outbox = tmp_path / "out.jsonl"
    # Tiny threshold so each send after the first triggers a rotation.
    disp = FileOutboxDispatcher(outbox, max_bytes=50, backup_count=3)
    for i in range(5):
        disp.send(_incentive(f"C{i}"))
    assert outbox.exists()
    assert (tmp_path / "out.1.jsonl").exists()


def test_backup_count_capped(tmp_path):
    outbox = tmp_path / "out.jsonl"
    disp = FileOutboxDispatcher(outbox, max_bytes=50, backup_count=2)
    for i in range(10):
        disp.send(_incentive(f"C{i}"))
    backups = sorted(tmp_path.glob("out.*.jsonl"))
    # Only out.1.jsonl and out.2.jsonl may exist — never out.3.jsonl.
    assert not (tmp_path / "out.3.jsonl").exists()
    assert len(backups) <= 2
