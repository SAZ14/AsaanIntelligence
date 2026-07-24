"""Staff web dashboard for the live queue -- does everything the WhatsApp
staff commands (app/gateway/internal.py, app.agents.maitre_d.staff) do,
through server-rendered pages instead of chat commands. Every action here
calls the SAME underlying functions the WhatsApp path uses (staff.py,
config.py) -- no business logic is duplicated, only a web front end is
added on top.

Auth is WhatsApp OTP + a Redis-backed session (see app.core.dashboard_auth)
-- login proves you hold a phone number already registered as staff for a
store (app.core.db.StoreMember), the same trust boundary the WhatsApp
channel already uses. A phone managing more than one store picks which one
after logging in; every route below is scoped to that ONE store_id for the
rest of the session, exactly like the WhatsApp channel is scoped to
whichever store texted it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.dashboard_auth import (
    SESSION_COOKIE_NAME, SESSION_TTL_SECONDS,
    create_session, destroy_session, get_session,
    request_otp, set_session_store, verify_otp,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates" / "dashboard"))


# ── session helper ───────────────────────────────────────────────────────────

def _session_or_redirect(request: Request):
    """Returns (session_dict, None) if the request has a full session
    (logged in AND a store chosen) for a store that's actually licensed for
    maitre_d, or (None, Response) pointing wherever they need to go next --
    login, store selection, (a store is chosen but its package doesn't
    include the queue) a plain "not included in your plan" page, or (a
    manager/staff member of a multi-branch store with no branch assigned
    yet) a "not assigned" page. Plain helper, not a FastAPI Depends -- a
    dependency can't redirect without extra exception-handler plumbing, and
    every route here needs the same branches.

    Every single dashboard route is maitre_d functionality (queue, VIPs,
    branches, settings all wrap app.agents.maitre_d.* directly) -- so this
    one chokepoint is enough to gate the entire feature, unlike internal.py
    where each agent's commands need their own check.

    Also resolves branch scoping here, once, for every route to reuse:
    session["effective_location_id"] is None for an owner or a single-
    location store (unrestricted -- see all/act on all), or the ONE
    location_id a manager/staff member is confined to. session["locations"]
    is this store's full branch list (VenueConfig), used by the queue/
    settings pages to build owner-only tabs/per-branch controls."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    session = get_session(token)
    if not session:
        return None, RedirectResponse("/dashboard/login", status_code=303)
    if not session.get("store_id"):
        return None, RedirectResponse("/dashboard/select-store", status_code=303)
    from app.core.entitlements import has_agent_access
    if not has_agent_access(session["store_id"], "maitre_d"):
        return None, templates.TemplateResponse(
            request, "not_licensed.html", {}, status_code=403,
        )

    from app.agents.maitre_d.config import VenueConfig
    locations = VenueConfig.list_locations(session["store_id"])
    session["locations"] = locations
    if len(locations) <= 1 or session.get("role") == "owner":
        session["effective_location_id"] = None
    else:
        loc_id = session.get("location_id")
        if not loc_id:
            return None, templates.TemplateResponse(
                request, "not_assigned.html",
                {"store_name": _store_name(session["store_id"])}, status_code=403,
            )
        session["effective_location_id"] = loc_id
    return session, None


def _store_name(store_id: int) -> str:
    from app.core.db import SessionLocal, Store
    with SessionLocal() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        return store.name if store else "your restaurant"


# ── login / OTP / store selection ───────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(request: Request, phone: str = Form(...)):
    ok, result = request_otp(phone)
    if not ok:
        message = "Please wait a little before requesting another code." if result == "cooldown" \
            else "That doesn't look like a valid phone number."
        return templates.TemplateResponse(
            request, "login.html", {"error": message}, status_code=400,
        )
    # result is a canonical "whatsapp:+<digits>" id -- MUST be percent-encoded
    # here, not just interpolated raw: an un-encoded "+" in a query string is
    # decoded back to a space by the standard application/x-www-form-urlencoded
    # convention (confirmed live -- every login silently failed verification
    # because the phone round-tripped through this redirect as "whatsapp:
    # 9233..." instead of "whatsapp:+9233...", so the cache lookup key in
    # verify_otp() never matched what request_otp() actually stored).
    from urllib.parse import quote
    return RedirectResponse(f"/dashboard/verify?phone={quote(result, safe='')}", status_code=303)


@router.get("/verify", response_class=HTMLResponse)
async def verify_page(request: Request, phone: str = ""):
    return templates.TemplateResponse(request, "verify.html", {"phone": phone, "error": None})


@router.post("/verify")
async def verify_submit(request: Request, phone: str = Form(...), code: str = Form(...)):
    if not verify_otp(phone, code):
        return templates.TemplateResponse(
            request, "verify.html",
            {"phone": phone, "error": "That code is wrong or has expired. Try again."},
            status_code=400,
        )
    from app.core.db import get_stores_for_number
    stores = get_stores_for_number(phone)
    token = create_session(phone)
    response = RedirectResponse(
        "/dashboard/select-store" if len(stores) != 1 else "/dashboard/queue",
        status_code=303,
    )
    if len(stores) == 1:
        from app.core.db import SessionLocal, StoreMember
        with SessionLocal() as db:
            member = db.query(StoreMember).filter(
                StoreMember.store_id == stores[0].id, StoreMember.whatsapp == phone,
            ).first()
            role = member.role if member else "staff"
            location_id = member.location_id if member else None
        set_session_store(token, stores[0].id, role, location_id)
    response.set_cookie(
        SESSION_COOKIE_NAME, token, max_age=SESSION_TTL_SECONDS,
        httponly=True, samesite="strict", secure=True,
    )
    return response


@router.get("/select-store", response_class=HTMLResponse)
async def select_store_page(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    session = get_session(token)
    if not session:
        return RedirectResponse("/dashboard/login", status_code=303)
    from app.core.db import get_stores_for_number
    stores = get_stores_for_number(session["whatsapp_id"])
    return templates.TemplateResponse(request, "select_store.html", {"stores": stores})


@router.post("/select-store")
async def select_store_submit(request: Request, store_id: int = Form(...)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    session = get_session(token)
    if not session:
        return RedirectResponse("/dashboard/login", status_code=303)
    from app.core.db import get_stores_for_number, SessionLocal, StoreMember
    stores = get_stores_for_number(session["whatsapp_id"])
    if not any(s.id == store_id for s in stores):
        return RedirectResponse("/dashboard/select-store", status_code=303)
    with SessionLocal() as db:
        member = db.query(StoreMember).filter(
            StoreMember.store_id == store_id, StoreMember.whatsapp == session["whatsapp_id"],
        ).first()
        role = member.role if member else "staff"
        location_id = member.location_id if member else None
    set_session_store(token, store_id, role, location_id)
    return RedirectResponse("/dashboard/queue", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    destroy_session(token)
    response = RedirectResponse("/dashboard/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


# ── queue board ──────────────────────────────────────────────────────────────

def _grouped_queue(store_id: int, only_location_id: int | None = None) -> list[dict]:
    """Waiting entries grouped by branch, each group carrying its own
    location_id (None for a single-location store) -- one "Admit
    next"/"add walk-in" action per group, since position numbers and
    admit/remove are all scoped to ONE location at a time (see
    app.agents.maitre_d.store's per-location locking). only_location_id
    restricts to one branch's rows (an owner's active tab, or a branch-
    scoped staff/manager's one assignment) -- None means "the store's
    single implicit location" OR "don't filter" depending on the caller,
    both of which are the same underlying query either way."""
    from app.agents.maitre_d.store import Store
    rows = Store(store_id).list_queue(status="waiting", location_id=only_location_id)
    groups: dict[str, dict] = {}
    for r in rows:
        key = r.branch_name or ""
        if key not in groups:
            groups[key] = {"branch_name": r.branch_name, "location_id": r.location_id or None, "entries": []}
        groups[key]["entries"].append(r)
    return list(groups.values())


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request, branch: str = ""):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    from app.agents.maitre_d.config import is_booking_enabled
    store_id = session["store_id"]
    effective_location_id = session["effective_location_id"]
    locations = session["locations"]

    branch_tabs = []
    if effective_location_id is None and len(locations) > 1:
        # Owner viewing a multi-branch store: one tab per branch, picking
        # which single branch's board is shown -- never all of them
        # stacked together, so "Admit"/"Add walk-in" is never ambiguous
        # about which branch it acts on.
        active_loc = next((l for l in locations if l.branch_key == branch), None) or locations[0]
        view_location_id = active_loc.location_id
        branch_tabs = [
            {"branch_key": l.branch_key, "branch_name": l.branch_name,
             "active": l.branch_key == active_loc.branch_key}
            for l in locations
        ]
    else:
        view_location_id = effective_location_id

    return templates.TemplateResponse(request, "queue.html", {
        "store_name": _store_name(store_id),
        "role": session.get("role", "staff"),
        "active_tab": "queue",
        "groups": _grouped_queue(store_id, view_location_id),
        "booking_enabled": is_booking_enabled(store_id, view_location_id),
        "branch_tabs": branch_tabs,
    })


@router.get("/queue/fragment", response_class=HTMLResponse)
async def queue_fragment(request: Request, branch: str = ""):
    """Polled every few seconds by the queue page's own JS to refresh the
    ticket board without a full page reload -- the poll passes along
    whatever `branch` tab is currently selected (see queue.html's
    refreshQueue), so an owner's tab choice survives each refresh."""
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    from app.agents.maitre_d.config import is_booking_enabled
    store_id = session["store_id"]
    effective_location_id = session["effective_location_id"]
    locations = session["locations"]

    if effective_location_id is None and len(locations) > 1:
        active_loc = next((l for l in locations if l.branch_key == branch), None) or locations[0]
        view_location_id = active_loc.location_id
    else:
        view_location_id = effective_location_id

    return templates.TemplateResponse(request, "_queue_fragment.html", {
        "groups": _grouped_queue(store_id, view_location_id),
        "booking_enabled": is_booking_enabled(store_id, view_location_id),
    })


def _branch_suffix(branch_name: str) -> str:
    return f" at {branch_name}" if branch_name else ""


@router.post("/queue/admit")
async def queue_admit(request: Request, branch_name: str = Form("")):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    from app.agents.maitre_d.staff import admit_next_in_queue
    effective_location_id = session["effective_location_id"]
    if effective_location_id is not None:
        # Branch-scoped staff/manager -- the form's branch_name (from
        # whichever group was on screen) is never trusted as an override.
        admit_next_in_queue(session["store_id"], "", forced_location_id=effective_location_id)
    else:
        admit_next_in_queue(session["store_id"], _branch_suffix(branch_name).strip())
    return RedirectResponse("/dashboard/queue", status_code=303)


@router.post("/queue/remove")
async def queue_remove(request: Request, position: int = Form(...), branch_name: str = Form("")):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    from app.agents.maitre_d.staff import remove_queue_position
    effective_location_id = session["effective_location_id"]
    if effective_location_id is not None:
        remove_queue_position(session["store_id"], str(position), forced_location_id=effective_location_id)
    else:
        remove_queue_position(session["store_id"], f"{position}{_branch_suffix(branch_name)}")
    return RedirectResponse("/dashboard/queue", status_code=303)


@router.post("/queue/insert")
async def queue_insert(
    request: Request, position: int = Form(...), name: str = Form(...),
    party_size: int = Form(1), phone: str = Form(""), branch_name: str = Form(""),
):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    from app.agents.maitre_d.staff import insert_queue_position
    phone_part = f"{phone} " if phone.strip() else ""
    effective_location_id = session["effective_location_id"]
    if effective_location_id is not None:
        insert_queue_position(
            session["store_id"], f"{position} {phone_part}{name}, party {party_size}",
            forced_location_id=effective_location_id,
        )
    else:
        insert_queue_position(
            session["store_id"],
            f"{position} {phone_part}{name}, party {party_size}{_branch_suffix(branch_name)}",
        )
    return RedirectResponse("/dashboard/queue", status_code=303)


# ── VIPs ─────────────────────────────────────────────────────────────────────

@router.get("/vips", response_class=HTMLResponse)
async def vips_page(request: Request):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    from app.agents.maitre_d.config import VenueConfig
    cfg = VenueConfig.load(session["store_id"])
    return templates.TemplateResponse(request, "vips.html", {
        "store_name": _store_name(session["store_id"]),
        "role": session.get("role", "staff"),
        "active_tab": "vips",
        "vips": cfg.vips,
    })


@router.post("/vips/add")
async def vips_add(
    request: Request, phone: str = Form(...), name: str = Form(...), notes: str = Form(""),
):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    from app.agents.maitre_d.staff import add_vip
    rest = f"{phone} {name}" + (f", {notes}" if notes.strip() else "")
    add_vip(session["store_id"], rest)
    return RedirectResponse("/dashboard/vips", status_code=303)


# ── branches (read-only) ─────────────────────────────────────────────────────

@router.get("/branches", response_class=HTMLResponse)
async def branches_page(request: Request):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    from app.agents.maitre_d.config import VenueConfig
    return templates.TemplateResponse(request, "branches.html", {
        "store_name": _store_name(session["store_id"]),
        "role": session.get("role", "staff"),
        "active_tab": "branches",
        "locations": VenueConfig.list_locations(session["store_id"]),
    })


# ── staff branch assignment (owner-only) ────────────────────────────────────

@router.get("/staff", response_class=HTMLResponse)
async def staff_page(request: Request):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    if session.get("role") != "owner":
        return templates.TemplateResponse(request, "not_licensed.html", {}, status_code=403)
    from app.core.db import SessionLocal, StoreMember
    store_id = session["store_id"]
    locations = session["locations"]
    with SessionLocal() as db:
        members = db.query(StoreMember).filter(
            StoreMember.store_id == store_id,
        ).order_by(StoreMember.role, StoreMember.whatsapp).all()
        member_rows = [
            {"id": m.id, "whatsapp": m.whatsapp, "role": m.role, "location_id": m.location_id}
            for m in members
        ]
    return templates.TemplateResponse(request, "staff.html", {
        "store_name": _store_name(store_id),
        "role": "owner",
        "active_tab": "staff",
        "members": member_rows,
        "locations": locations,
        "multi_location": len(locations) > 1,
    })


@router.post("/staff/assign")
async def staff_assign(request: Request, member_id: int = Form(...), location_id: str = Form("")):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    if session.get("role") != "owner":
        return templates.TemplateResponse(request, "not_licensed.html", {}, status_code=403)
    from app.core.db import SessionLocal, StoreMember
    store_id = session["store_id"]
    with SessionLocal() as db:
        member = db.query(StoreMember).filter(
            StoreMember.id == member_id, StoreMember.store_id == store_id,
        ).first()
        if member and member.role != "owner":
            member.location_id = int(location_id) if location_id else None
            db.commit()
    return RedirectResponse("/dashboard/staff", status_code=303)


# ── settings ─────────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    from app.agents.maitre_d.config import (
        is_booking_enabled, get_seated_grace_minutes, get_queue_stale_minutes,
    )
    store_id = session["store_id"]
    effective_location_id = session["effective_location_id"]
    locations = session["locations"]

    branch_settings = None
    booking_enabled = None
    if effective_location_id is None and len(locations) > 1:
        # Owner, multi-branch store: one independent toggle per branch
        # instead of a single store-wide switch.
        branch_settings = [
            {"location_id": l.location_id, "branch_name": l.branch_name,
             "booking_enabled": is_booking_enabled(store_id, l.location_id)}
            for l in locations
        ]
    else:
        booking_enabled = is_booking_enabled(store_id, effective_location_id)

    return templates.TemplateResponse(request, "settings.html", {
        "store_name": _store_name(store_id),
        "role": session.get("role", "staff"),
        "active_tab": "settings",
        "branch_settings": branch_settings,
        "booking_enabled": booking_enabled,
        "seated_grace_minutes": get_seated_grace_minutes(store_id),
        "queue_stale_minutes": get_queue_stale_minutes(store_id),
        "error": None,
    })


@router.post("/settings/booking")
async def settings_booking(request: Request, enabled: str = Form(""), location_id: str = Form("")):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    from app.agents.maitre_d.config import set_booking_enabled
    effective_location_id = session["effective_location_id"]
    # A branch-scoped staff/manager always toggles their OWN branch; an
    # owner's form tells us which branch's button was actually clicked
    # (only present on a multi-branch store's settings page).
    target_location_id = effective_location_id
    if effective_location_id is None and location_id:
        target_location_id = int(location_id)
    set_booking_enabled(session["store_id"], enabled == "on", location_id=target_location_id)
    return RedirectResponse("/dashboard/settings", status_code=303)


@router.post("/settings/thresholds")
async def settings_thresholds(
    request: Request, seated_grace_minutes: int = Form(...), queue_stale_minutes: int = Form(...),
):
    session, redirect = _session_or_redirect(request)
    if redirect:
        return redirect
    from app.gateway.internal import _MIN_THRESHOLD_MINUTES, _MAX_THRESHOLD_MINUTES
    store_id = session["store_id"]
    if not (_MIN_THRESHOLD_MINUTES <= seated_grace_minutes <= _MAX_THRESHOLD_MINUTES) or \
       not (_MIN_THRESHOLD_MINUTES <= queue_stale_minutes <= _MAX_THRESHOLD_MINUTES):
        from app.agents.maitre_d.config import is_booking_enabled
        return templates.TemplateResponse(request, "settings.html", {
            "store_name": _store_name(store_id),
            "role": session.get("role", "staff"),
            "active_tab": "settings",
            "booking_enabled": is_booking_enabled(store_id),
            "seated_grace_minutes": seated_grace_minutes,
            "queue_stale_minutes": queue_stale_minutes,
            "error": f"Please pick values between {_MIN_THRESHOLD_MINUTES} and {_MAX_THRESHOLD_MINUTES} minutes.",
        }, status_code=400)
    from app.agents.maitre_d.config import set_seated_grace_minutes, set_queue_stale_minutes
    set_seated_grace_minutes(store_id, seated_grace_minutes)
    set_queue_stale_minutes(store_id, queue_stale_minutes)
    return RedirectResponse("/dashboard/settings", status_code=303)
