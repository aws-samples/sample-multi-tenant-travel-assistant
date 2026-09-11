"""Delegated tools send the authorized traveler to the backend, not the arranger."""

from __future__ import annotations

from tools.booking import handler as booking_handler
from tools.common.testing import GLOBEX_ARRANGER
from tools.entry import handler as entry_handler
from tools.profile import handler as profile_handler
from tools.search import handler as search_handler
from tools.trips import handler as trips_handler

TARGET = "trv_authorized_target"


def _capture_post(captured):
    def fake_post(base, path, context, *, body=None):
        captured.append((path, context, body))
        if path.endswith("/hold"):
            return {
                "offer_id": "off_0123456789",
                "display_price": {"amount": "100.00", "currency": "USD"},
                "description": "A held option",
                "policy_status": "in_policy",
                "expires_at": "2026-09-01T12:00:00",
            }
        return {"options": [], "summary": {}}

    return fake_post


def test_delegated_searches_use_the_target_profile(monkeypatch):
    captured = []
    monkeypatch.setattr(search_handler, "backend_url", lambda: "https://backend.example")
    monkeypatch.setattr(search_handler, "_resolve_traveler", lambda arguments, context: TARGET)
    monkeypatch.setattr(search_handler, "post", _capture_post(captured))

    search_handler.search_flights(
        {"destination": "London", "depart_on": "2026-09-15"},
        GLOBEX_ARRANGER,
    )
    search_handler.search_hotels(
        {
            "destination": "London",
            "check_in": "2026-09-15",
            "check_out": "2026-09-18",
        },
        GLOBEX_ARRANGER,
    )

    assert [context.backend_traveler_id for _, context, _ in captured] == [TARGET, TARGET]
    assert all(context.traveler_id == GLOBEX_ARRANGER.traveler_id for _, context, _ in captured)


def test_delegated_hold_is_owned_by_the_target(monkeypatch):
    captured = []
    monkeypatch.setattr(booking_handler, "backend_url", lambda: "https://backend.example")
    monkeypatch.setattr(
        booking_handler,
        "resolve_target_traveler",
        lambda context, traveler_name: (TARGET, "Target Traveler"),
    )
    monkeypatch.setattr(booking_handler, "ensure_can_act_for", lambda context, traveler_id: None)
    monkeypatch.setattr(booking_handler, "post", _capture_post(captured))
    monkeypatch.setattr(
        booking_handler,
        "_tenant_config",
        lambda context: {"booking_mode": booking_handler.CONFIRM_IN_CHAT},
    )
    monkeypatch.setattr(booking_handler, "_payment_label", lambda context, traveler_id: "Visa")

    booking_handler.prepare_booking(
        {
            "option_id": "opt_0123456789_0",
            "kind": "hotel",
            "destination": "London",
            "check_in": "2026-09-15",
            "check_out": "2026-09-18",
            "traveler_name": "Target Traveler",
        },
        GLOBEX_ARRANGER,
    )

    _, context, _ = captured[0]
    assert context.backend_traveler_id == TARGET
    assert context.traveler_id == GLOBEX_ARRANGER.traveler_id


def test_delegated_confirm_resolves_the_subject_then_preserves_both_identities(monkeypatch):
    reads = []
    writes = []
    monkeypatch.setattr(booking_handler, "backend_url", lambda: "https://backend.example")
    monkeypatch.setattr(
        booking_handler,
        "_tenant_config",
        lambda context: {"booking_mode": booking_handler.CONFIRM_IN_CHAT},
    )

    def fake_get(base, path, context):
        reads.append((path, context))
        return {"traveler_id": TARGET}

    def fake_post(base, path, context, *, body=None):
        writes.append((path, context, body))
        return {
            "booking_ref": "bkg_0123456789",
            "confirmation_number": "TRV123456",
            "kind": "hotel",
            "description": "A confirmed stay",
            "total": {"amount": "100.00", "currency": "USD"},
        }

    monkeypatch.setattr(booking_handler, "get", fake_get)
    monkeypatch.setattr(booking_handler, "post", fake_post)

    booking_handler.confirm_booking(
        {"booking_ref": "off_0123456789"},
        GLOBEX_ARRANGER,
    )

    _, resolving_context = reads[0]
    assert resolving_context.backend_traveler_id is None
    assert resolving_context.traveler_id == GLOBEX_ARRANGER.traveler_id
    _, write_context, _ = writes[0]
    assert write_context.traveler_id == GLOBEX_ARRANGER.traveler_id
    assert write_context.backend_traveler_id == TARGET


def test_delegated_cancel_attributes_terms_and_write_to_the_target(monkeypatch):
    reads = []
    writes = []
    monkeypatch.setattr(booking_handler, "backend_url", lambda: "https://backend.example")

    def fake_get(base, path, context):
        reads.append((path, context))
        if path.endswith("/subject"):
            return {"traveler_id": TARGET}
        return {
            "description": "A confirmed stay",
            "penalties": [],
            "fully_refundable": True,
            "refund_estimate": {"amount": "100.00", "currency": "USD"},
        }

    def fake_post(base, path, context, *, body=None):
        writes.append((path, context, body))
        return {"description": "A confirmed stay", "status": "canceled"}

    monkeypatch.setattr(booking_handler, "get", fake_get)
    monkeypatch.setattr(booking_handler, "post", fake_post)

    booking_handler.cancel_reservation(
        {"booking_ref": "bkg_0123456789", "confirm": True},
        GLOBEX_ARRANGER,
    )

    assert reads[0][1].backend_traveler_id is None
    assert reads[1][1].backend_traveler_id == TARGET
    assert reads[1][1].traveler_id == GLOBEX_ARRANGER.traveler_id
    assert writes[0][1].backend_traveler_id == TARGET
    assert writes[0][1].traveler_id == GLOBEX_ARRANGER.traveler_id


def test_delegated_entry_check_uses_the_targets_passport(monkeypatch):
    captured = []
    monkeypatch.setattr(entry_handler, "backend_url", lambda: "https://backend.example")
    monkeypatch.setattr(
        entry_handler,
        "resolve_target_traveler",
        lambda context, traveler_name: (TARGET, "Target Traveler"),
    )
    monkeypatch.setattr(entry_handler, "ensure_can_act_for", lambda context, traveler_id: None)

    def fake_get(base, path, context):
        captured.append(context)
        return {
            "requirement": "none",
            "passport_country": "GB",
            "disclaimer": "Verify before travel.",
        }

    monkeypatch.setattr(entry_handler, "get", fake_get)

    entry_handler.check_entry_requirements(
        {"destination_country": "US", "traveler_name": "Target Traveler"},
        GLOBEX_ARRANGER,
    )

    assert captured[0].backend_traveler_id == TARGET
    assert captured[0].traveler_id == GLOBEX_ARRANGER.traveler_id


def test_delegated_profile_read_attributes_the_target(monkeypatch):
    captured = []
    monkeypatch.setattr(profile_handler, "backend_url", lambda: "https://backend.example")
    monkeypatch.setattr(
        profile_handler,
        "resolve_target_traveler",
        lambda context, traveler_name: (TARGET, "Target Traveler"),
    )
    monkeypatch.setattr(profile_handler, "ensure_can_act_for", lambda context, traveler_id: None)

    def fake_get(base, path, context):
        captured.append(context)
        return {"full_name": "Target Traveler", "role": "traveler"}

    monkeypatch.setattr(profile_handler, "get", fake_get)

    profile_handler.get_traveler_profile(
        {"traveler_name": "Target Traveler"},
        GLOBEX_ARRANGER,
    )

    assert captured[0].backend_traveler_id == TARGET
    assert captured[0].traveler_id == GLOBEX_ARRANGER.traveler_id


def test_delegated_trip_read_attributes_the_target(monkeypatch):
    captured = []
    monkeypatch.setattr(trips_handler, "backend_url", lambda: "https://backend.example")
    monkeypatch.setattr(
        trips_handler,
        "resolve_target_traveler",
        lambda context, traveler_name: (TARGET, "Target Traveler"),
    )
    monkeypatch.setattr(trips_handler, "ensure_can_act_for", lambda context, traveler_id: None)

    def fake_get(base, path, context, *, params=None):
        captured.append((context, params))
        return []

    monkeypatch.setattr(trips_handler, "get", fake_get)

    trips_handler.get_trips(
        {"traveler_name": "Target Traveler"},
        GLOBEX_ARRANGER,
    )

    context, params = captured[0]
    assert context.backend_traveler_id == TARGET
    assert context.traveler_id == GLOBEX_ARRANGER.traveler_id
    assert params == {"traveler": TARGET}
