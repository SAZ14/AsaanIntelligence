"""Meta Cloud API webhook + document (CSV) upload on the OpenWA and Meta paths."""
import base64
import hashlib
import hmac
import json

import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient

from tests.conftest import TestSession, seed_chain, seed_store, seed_member

VERIFY_TOKEN = "test-verify-token"
APP_SECRET = "test-app-secret"
PNID = "111222333444555"
STAFF_WA_ID = "923001112223"
CUSTOMER_WA_ID = "923009998887"

CANONICAL_SALES = (
    "order_id,datetime,staff_id,staff_name,table,channel,item_sku,item_name,"
    "category,qty,unit_price,line_amount,discount_amount,is_void,void_after_fire,"
    "is_comp,order_status,payment_method,payment_amount,tax_rate,customer_ref\n"
    "O1,2026-06-04 14:30:00,S1,Ali,5,dine-in,SKU1,Burger,Food,2,500,1000,0,false,"
    "false,false,closed,cash,1000,0.16,\n"
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("META_VERIFY_TOKEN", VERIFY_TOKEN)
    monkeypatch.setenv("META_APP_SECRET", APP_SECRET)
    from app.gateway.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def meta_store():
    from app.core.db import StoreMetaNumber
    chain_id = seed_chain("Meta Chain")
    store_id = seed_store(chain_id, name="Meta Cafe")
    seed_member(store_id, f"whatsapp:+{STAFF_WA_ID}", role="owner")
    with TestSession() as db:
        db.add(StoreMetaNumber(
            store_id=store_id, phone_number_id=PNID,
            waba_id="waba-1", access_token="token-abc",
            display_number="+923330001111",
        ))
        db.commit()
    return store_id


def _signed_post(client, payload: dict):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/meta/webhook", content=raw,
                       headers={"Content-Type": "application/json",
                                "X-Hub-Signature-256": sig})


def _message_payload(msg: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "waba-1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "923330001111",
                                 "phone_number_id": PNID},
                    "messages": [msg],
                },
            }],
        }],
    }


# ── Verification handshake ────────────────────────────────────────────────────

def test_verify_handshake_echoes_challenge(client):
    r = client.get("/meta/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
        "hub.challenge": "12345chal",
    })
    assert r.status_code == 200
    assert r.text == "12345chal"


def test_verify_handshake_rejects_wrong_token(client):
    r = client.get("/meta/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong",
        "hub.challenge": "x",
    })
    assert r.status_code == 403


# ── Signature enforcement ─────────────────────────────────────────────────────

def test_unsigned_post_rejected_when_secret_set(client, meta_store):
    r = client.post("/meta/webhook", json=_message_payload({
        "id": "m1", "from": CUSTOMER_WA_ID, "type": "text",
        "text": {"body": "hi"},
    }))
    assert r.status_code == 403


def test_bad_signature_rejected(client, meta_store):
    raw = json.dumps(_message_payload({
        "id": "m2", "from": CUSTOMER_WA_ID, "type": "text", "text": {"body": "hi"},
    })).encode()
    r = client.post("/meta/webhook", content=raw,
                    headers={"Content-Type": "application/json",
                             "X-Hub-Signature-256": "sha256=deadbeef"})
    assert r.status_code == 403


# ── Message routing through the shared flow ───────────────────────────────────

def test_customer_text_routed_and_replied_via_meta(client, meta_store):
    with (
        patch("app.gateway.customer.handle_customer_for_store", return_value="Welcome!") as mock_agent,
        patch("app.core.meta_send.send_meta") as mock_send,
    ):
        r = _signed_post(client, _message_payload({
            "id": "m3", "from": CUSTOMER_WA_ID, "type": "text",
            "text": {"body": "hello"},
        }))
    assert r.status_code == 200
    mock_agent.assert_called_once_with(f"whatsapp:+{CUSTOMER_WA_ID}", "hello", meta_store)
    mock_send.assert_called_once_with(PNID, CUSTOMER_WA_ID, "Welcome!")


def test_staff_first_text_gets_mode_menu(client, meta_store):
    with patch("app.core.meta_send.send_meta") as mock_send:
        r = _signed_post(client, _message_payload({
            "id": "m4", "from": STAFF_WA_ID, "type": "text",
            "text": {"body": "hi"},
        }))
    assert r.status_code == 200
    mock_send.assert_called_once()
    body = mock_send.call_args[0][2]
    assert "1" in body and "2" in body  # mode-selection menu


def test_duplicate_message_id_dropped(client, meta_store):
    msg = {"id": "m5", "from": CUSTOMER_WA_ID, "type": "text", "text": {"body": "hi"}}
    with (
        patch("app.gateway.customer.handle_customer_for_store", return_value="ok") as mock_agent,
        patch("app.core.meta_send.send_meta"),
    ):
        _signed_post(client, _message_payload(msg))
        _signed_post(client, _message_payload(msg))
    mock_agent.assert_called_once()


def test_status_only_webhook_ignored(client, meta_store):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba-1", "changes": [{
            "field": "messages",
            "value": {"metadata": {"phone_number_id": PNID},
                      "statuses": [{"id": "x", "status": "delivered"}]},
        }]}],
    }
    r = _signed_post(client, payload)
    assert r.status_code == 200


# ── Document (CSV) upload via Meta ────────────────────────────────────────────

def test_staff_csv_document_ingested(client, meta_store):
    with (
        patch("app.core.meta_send.download_media",
              return_value=(CANONICAL_SALES.encode(), "text/csv", "sales.csv")),
        patch("app.core.meta_send.send_meta") as mock_send,
    ):
        r = _signed_post(client, _message_payload({
            "id": "m6", "from": STAFF_WA_ID, "type": "document",
            "document": {"id": "media-1", "mime_type": "text/csv",
                         "filename": "sales.csv", "caption": "sales"},
        }))
    assert r.status_code == 200
    mock_send.assert_called_once()
    reply = mock_send.call_args[0][2]
    assert "Saved sales data" in reply

    from app.core.db import UploadedFile
    with TestSession() as db:
        row = db.query(UploadedFile).filter(
            UploadedFile.store_id == meta_store,
            UploadedFile.file_type == "pos_sales",
        ).first()
        assert row is not None
        assert "Burger" in row.content


def test_customer_document_ignored(client, meta_store):
    with (
        patch("app.core.meta_send.download_media") as mock_dl,
        patch("app.core.meta_send.send_meta") as mock_send,
    ):
        _signed_post(client, _message_payload({
            "id": "m7", "from": CUSTOMER_WA_ID, "type": "document",
            "document": {"id": "media-2", "mime_type": "text/csv",
                         "filename": "sales.csv", "caption": "sales"},
        }))
    mock_dl.assert_not_called()
    mock_send.assert_not_called()


def test_non_csv_document_gets_guidance(client, meta_store):
    with patch("app.core.meta_send.send_meta") as mock_send:
        _signed_post(client, _message_payload({
            "id": "m8", "from": STAFF_WA_ID, "type": "document",
            "document": {"id": "media-3", "mime_type": "application/pdf",
                         "filename": "report.pdf"},
        }))
    mock_send.assert_called_once()
    assert "CSV" in mock_send.call_args[0][2]


# ── Document (CSV) upload via OpenWA ─────────────────────────────────────────

OWA_SESSION = "owa-doc-session"


@pytest.fixture
def openwa_store():
    from app.core.db import StoreOpenWASession
    chain_id = seed_chain("OWA Chain")
    store_id = seed_store(chain_id, name="OWA Cafe")
    seed_member(store_id, f"whatsapp:+{STAFF_WA_ID}", role="owner")
    with TestSession() as db:
        db.add(StoreOpenWASession(store_id=store_id, session_id=OWA_SESSION,
                                  phone_number="+923330002222"))
        db.commit()
    return store_id


def _owa_doc_payload(b64: str, mimetype="text/csv", filename="sales.csv",
                     caption="sales", sender=STAFF_WA_ID, msg_id="D1"):
    return {
        "event": "message.received",
        "sessionId": OWA_SESSION,
        "idempotencyKey": f"idem-{msg_id}",
        "data": {
            "id": msg_id,
            "from": f"{sender}@c.us",
            "to": "923330002222@c.us",
            "body": caption,
            "type": "document",
            "fromMe": False,
            "isGroup": False,
            "metadata": {"media": {"mimetype": mimetype, "data": b64,
                                   "filename": filename}},
        },
    }


def test_openwa_staff_csv_document_ingested(client, openwa_store):
    b64 = base64.b64encode(CANONICAL_SALES.encode()).decode()
    with patch("app.core.openwa_send.send_openwa") as mock_send:
        r = client.post("/openwa/webhook", json=_owa_doc_payload(b64))
    assert r.status_code == 200
    mock_send.assert_called_once()
    reply = mock_send.call_args[0][2]
    assert "Saved sales data" in reply

    from app.core.db import UploadedFile
    with TestSession() as db:
        row = db.query(UploadedFile).filter(
            UploadedFile.store_id == openwa_store,
            UploadedFile.file_type == "pos_sales",
        ).first()
        assert row is not None


def test_openwa_customer_document_ignored(client, openwa_store):
    b64 = base64.b64encode(CANONICAL_SALES.encode()).decode()
    with patch("app.core.openwa_send.send_openwa") as mock_send:
        r = client.post("/openwa/webhook",
                        json=_owa_doc_payload(b64, sender=CUSTOMER_WA_ID, msg_id="D2"))
    assert r.status_code == 200
    mock_send.assert_not_called()


def test_openwa_non_csv_document_gets_guidance(client, openwa_store):
    with patch("app.core.openwa_send.send_openwa") as mock_send:
        client.post("/openwa/webhook", json=_owa_doc_payload(
            "aGk=", mimetype="application/pdf", filename="menu.pdf", msg_id="D3"))
    mock_send.assert_called_once()
    assert "CSV" in mock_send.call_args[0][2]


# ── Job queue meta provider ───────────────────────────────────────────────────

def test_jobqueue_builds_meta_sender():
    from app.core.jobqueue import _build_send_fn
    send = _build_send_fn({"provider": "meta", "phone_number_id": PNID, "to": STAFF_WA_ID})
    with patch("app.core.meta_send.send_meta") as mock_send:
        send("queued reply")
    mock_send.assert_called_once_with(PNID, STAFF_WA_ID, "queued reply")
