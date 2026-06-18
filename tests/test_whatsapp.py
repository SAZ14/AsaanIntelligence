from pathlib import Path

import pytest

from app.pos import RestaurantConfig
from app.whatsapp.service import IntegrityWhatsAppService

DATA = Path(__file__).resolve().parent.parent / "data"


# ── Fake Anthropic client (no network) ──

class _FakeMessage:
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]


class _FakeClient:
    def __init__(self, text="ANSWER"):
        self._text = text

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            return _FakeMessage(self._outer._text)

    @property
    def messages(self):
        return _FakeClient._Messages(self)


def _service(llm_client=None):
    restaurants = {
        "roastery": RestaurantConfig(
            venue_name="Roastery", pos_type="csv",
            connection={"base_dir": str(DATA)}, mapping="cafe_generic",
        )
    }
    return IntegrityWhatsAppService(
        restaurants=restaurants,
        owner_map={"whatsapp:+923001234567": "roastery"},
        default_venue="roastery",
        llm_client=llm_client,
    )


def test_venue_resolution():
    svc = _service()
    assert svc.resolve_venue("whatsapp:+923001234567") == "roastery"
    # unknown number falls back to default venue
    assert svc.resolve_venue("whatsapp:+10000000000") == "roastery"


def test_unregistered_number_when_no_default():
    svc = _service()
    svc.default_venue = None
    reply = svc.handle_message("whatsapp:+19998887777", "summary")
    assert "isn't linked to a venue" in reply


def test_help_command():
    svc = _service()
    assert "integrity agent" in svc.handle_message("whatsapp:+923001234567", "help").lower()
    # empty body -> help
    assert "summary" in svc.handle_message("whatsapp:+923001234567", "").lower()


def test_revenue_command():
    svc = _service()
    reply = svc.handle_message("whatsapp:+923001234567", "revenue")
    assert "Revenue" in reply
    assert "PKR" in reply
    assert "cash" in reply  # payment-method breakdown present


def test_profit_command():
    svc = _service()
    reply = svc.handle_message("whatsapp:+923001234567", "profit")
    assert "Profit" in reply and "margin" in reply
    assert "Gross profit" in reply


def test_leakage_command():
    svc = _service()
    reply = svc.handle_message("whatsapp:+923001234567", "leakage")
    assert "Leakage" in reply
    assert "Bilal" in reply  # planted worst offender surfaced


def test_findings_command():
    svc = _service()
    reply = svc.handle_message("whatsapp:+923001234567", "findings")
    assert "findings" in reply.lower()
    assert "S03" in reply


def test_summary_command():
    svc = _service()
    reply = svc.handle_message("whatsapp:+923001234567", "summary")
    assert "Roastery" in reply
    assert "PKR" in reply


def test_freeform_question_uses_llm():
    svc = _service(llm_client=_FakeClient("Your worst staff member is Bilal (S03)."))
    reply = svc.handle_message("whatsapp:+923001234567", "who is my worst staff member?")
    assert reply == "Your worst staff member is Bilal (S03)."


def test_staff_list_command():
    svc = _service()
    reply = svc.handle_message("whatsapp:+923001234567", "staff")
    assert "Team integrity" in reply
    # all eight staff listed
    for sid in ("S01", "S02", "S03", "S08"):
        assert sid in reply


def test_staff_detail_by_name():
    svc = _service()
    reply = svc.handle_message("whatsapp:+923001234567", "staff Bilal")
    assert "Bilal (S03)" in reply
    assert "Integrity score" in reply
    assert "Void rate" in reply and "Comp rate" in reply


def test_staff_detail_by_id():
    svc = _service()
    reply = svc.handle_message("whatsapp:+923001234567", "staff S03")
    assert "Bilal (S03)" in reply
    assert "Total leakage" in reply


def test_staff_detail_unknown():
    svc = _service()
    reply = svc.handle_message("whatsapp:+923001234567", "staff Nobody")
    assert "No staff matching" in reply


def test_report_is_cached():
    svc = _service()
    r1 = svc.get_report("roastery")
    r2 = svc.get_report("roastery")
    assert r1 is r2  # same object -> served from cache
    r3 = svc.get_report("roastery", force=True)
    assert r3 is not r1


def test_refresh_command():
    svc = _service()
    reply = svc.handle_message("whatsapp:+923001234567", "refresh")
    assert "POS data" in reply


# ── Webhook (skips cleanly if FastAPI / test client unavailable) ──

def test_webhook_returns_twiml():
    pytest.importorskip("fastapi")
    starlette_testclient = pytest.importorskip("starlette.testclient")
    from app.whatsapp.webhook import create_app

    app = create_app(_service())
    client = starlette_testclient.TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200 and health.json()["status"] == "ok"

    resp = client.post("/whatsapp", data={"From": "whatsapp:+923001234567", "Body": "profit"})
    assert resp.status_code == 200
    assert "application/xml" in resp.headers["content-type"]
    assert "<Response><Message>" in resp.text
    assert "Profit" in resp.text


def test_report_command_attaches_pdf_media():
    pytest.importorskip("fastapi")
    starlette_testclient = pytest.importorskip("starlette.testclient")
    from app.whatsapp.webhook import create_app

    client = starlette_testclient.TestClient(create_app(_service()))
    resp = client.post("/whatsapp", data={"From": "whatsapp:+923001234567", "Body": "report"})
    assert resp.status_code == 200
    assert "<Media>" in resp.text
    assert "/report/roastery.pdf" in resp.text


def test_pdf_endpoint_serves_pdf():
    pytest.importorskip("fastapi")
    starlette_testclient = pytest.importorskip("starlette.testclient")
    from app.whatsapp.webhook import create_app

    client = starlette_testclient.TestClient(create_app(_service()))
    resp = client.get("/report/roastery.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF-1.")

    missing = client.get("/report/nope.pdf")
    assert missing.status_code == 404
