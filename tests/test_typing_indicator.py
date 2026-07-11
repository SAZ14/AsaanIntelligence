"""WhatsApp native typing indicator (app.core.meta_send.send_typing_
indicator) and its wiring into gateway/main.py's shared staff/customer
router (_process_async_message). Meta-only -- OpenWA has no equivalent
API, so its call site never passes typing_fn and behavior there is
unchanged (the pre-existing guessed text ack still fires).
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from tests.conftest import seed_chain, seed_store, seed_member


# ── send_typing_indicator: HTTP payload correctness ─────────────────────────

def test_send_typing_indicator_sends_read_status_and_typing_type():
    from app.core.meta_send import send_typing_indicator

    mock_client = MagicMock()
    mock_response = MagicMock(status_code=200)
    mock_client.__enter__.return_value.post.return_value = mock_response

    with (
        patch("app.core.meta_send._token_for", return_value="fake-token"),
        patch("httpx.Client", return_value=mock_client),
    ):
        send_typing_indicator("123456", "wamid.ABC123")

    call = mock_client.__enter__.return_value.post.call_args
    assert call.kwargs["json"] == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.ABC123",
        "typing_indicator": {"type": "text"},
    }
    assert "123456/messages" in call.args[0]


def test_send_typing_indicator_noop_without_token():
    from app.core.meta_send import send_typing_indicator
    with (
        patch("app.core.meta_send._token_for", return_value=""),
        patch("httpx.Client") as mock_httpx,
    ):
        send_typing_indicator("123456", "wamid.ABC123")
    mock_httpx.assert_not_called()


def test_send_typing_indicator_swallows_request_failures():
    """Best-effort -- a failure here must never propagate and block/fail
    the actual reply."""
    from app.core.meta_send import send_typing_indicator
    with (
        patch("app.core.meta_send._token_for", return_value="fake-token"),
        patch("httpx.Client", side_effect=RuntimeError("network down")),
    ):
        send_typing_indicator("123456", "wamid.ABC123")  # must not raise


# ── _process_async_message: typing_fn wiring ────────────────────────────────

PHONE = "whatsapp:+923001234567"


@pytest.fixture
def internal_store():
    from app.core.db import set_user_session
    from app.gateway.main import MODE_INTERNAL

    chain_id = seed_chain("Typing Chain")
    store_id = seed_store(chain_id, name="Typing Cafe", location="F-8, Islamabad")
    seed_member(store_id, PHONE, role="owner")
    set_user_session(PHONE, store_id, active_agent=MODE_INTERNAL)

    from app.core.db import SessionLocal, Store
    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        db.expunge(store)
    return store


def test_typing_fn_scheduled_as_background_task_for_catch_all_message(internal_store):
    """A natural-language question not caught by scout/reputation-check
    keywords -- the catch-all path that used to rely solely on a guessed
    text ack."""
    from app.gateway.main import _process_async_message

    bt = BackgroundTasks()
    sent = []
    typing_calls = []

    def _typing():
        typing_calls.append(1)

    with patch("app.gateway.main._staff_dispatch_ok", return_value=True):
        status = _process_async_message(
            internal_store, PHONE, "how did we do this week", sent.append, bt,
            {"provider": "meta", "phone_number_id": "123", "to": "923001234567"},
            "test.webhook", typing_fn=_typing,
        )

    assert status == "ok"
    assert _typing in [t.func for t in bt.tasks]


def test_catch_all_ack_is_skipped_when_typing_fn_provided(internal_store):
    from app.gateway.main import _process_async_message, _bg_internal

    bt = BackgroundTasks()
    sent = []
    with patch("app.gateway.main._staff_dispatch_ok", return_value=True):
        _process_async_message(
            internal_store, PHONE, "how did we do this week", sent.append, bt,
            {"provider": "meta", "phone_number_id": "123", "to": "923001234567"},
            "test.webhook", typing_fn=lambda: None,
        )

    internal_task = next(t for t in bt.tasks if t.func is _bg_internal)
    # _bg_internal(store_id, from_number, send_fn, body, ack=...) -- ack
    # must be absent/None when a native typing indicator already covers it.
    ack_arg = internal_task.kwargs.get("ack") if internal_task.kwargs else (
        internal_task.args[4] if len(internal_task.args) > 4 else None
    )
    assert ack_arg is None


def test_catch_all_ack_still_guessed_when_no_typing_fn(internal_store):
    """OpenWA path: no typing_fn passed, so the pre-existing guessed ack
    text must still fire -- unchanged, since OpenWA has no typing-
    indicator equivalent to fall back on."""
    from app.gateway.main import _process_async_message, _bg_internal

    bt = BackgroundTasks()
    sent = []
    with patch("app.gateway.main._staff_dispatch_ok", return_value=True):
        _process_async_message(
            internal_store, PHONE, "how did we do this week", sent.append, bt,
            {"provider": "openwa", "session_id": "s1", "jid": "923001234567@c.us"},
            "test.webhook",
        )

    internal_task = next(t for t in bt.tasks if t.func is _bg_internal)
    ack_arg = internal_task.kwargs.get("ack") if internal_task.kwargs else (
        internal_task.args[4] if len(internal_task.args) > 4 else None
    )
    assert ack_arg is not None
    assert isinstance(ack_arg, str)


def test_scout_dispatch_ack_unaffected_by_typing_fn(internal_store):
    """Deterministic, keyword-matched acks (scout's live-scrape wait
    time) must survive regardless of typing_fn -- a 25s-max typing
    indicator can't convey a 7-10 minute wait."""
    from app.gateway.main import _process_async_message, _bg_scout

    bt = BackgroundTasks()
    sent = []
    _process_async_message(
        internal_store, PHONE, "scout", sent.append, bt,
        {"provider": "meta", "phone_number_id": "123", "to": "923001234567"},
        "test.webhook", typing_fn=lambda: None,
    )

    scout_task = next((t for t in bt.tasks if t.func is _bg_scout), None)
    assert scout_task is not None
    ack_arg = scout_task.kwargs.get("ack") if scout_task.kwargs else (
        scout_task.args[4] if len(scout_task.args) > 4 else None
    )
    assert ack_arg and "minutes" in ack_arg.lower()
