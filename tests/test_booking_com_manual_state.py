"""Manual Booking.com repair must preserve booking identity, links and alert delivery."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta

import pytest

from booking_notifier.excel_export import excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.popup_state import booking_com_details_ready
from booking_notifier.state import StateStore


def basic_event(*, linked=False, booking_id="5550000001"):
    return BookingEvent(
        source="Booking.com", booking_id=booking_id, checkin_date=date.today(),
        details_url=("https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/booking.html"
                     f"?res_id={booking_id}&hotel_id=12345&lang=vi") if linked else "",
    )


def complete(event):
    return replace(event, status="new", guest_name="NGUYEN VAN SYNTHETIC", room_type="Deluxe Room x2",
                   checkout_date=event.checkin_date + timedelta(days=2), total_revenue="VND 1.200.000",
                   details_loaded_at="2026-10-08T12:00:00")


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("closed", [False, True])
def test_manual_repair_updates_known_pending_or_closed_stay_without_replay(tmp_path, linked, closed):
    state = StateStore(tmp_path / "state.json")
    original = basic_event(linked=linked)
    pending = state.register_today_confirmation(original, ("synthetic-mail",), date.today())
    state.stage_mailbox_reads("synthetic-mailbox", [41, 42], parser_version="p14")
    if closed:
        state.acknowledge(pending)
    saved_delivery = {key: deepcopy(state.data[key]) for key in ("processed_keys", "mailbox_reads")}
    before_pending_count = len(state.data["pending_alerts"])
    before_history_count = len(state.data["history"])
    full = complete(original)

    assert state.enrich_booking_com_manual(full, date.today())
    assert len(state.data["pending_alerts"]) == before_pending_count
    assert len(state.data["history"]) == before_history_count
    assert {key: state.data[key] for key in saved_delivery} == saved_delivery
    records = [*state.data["pending_alerts"], *state.data["history"], *state.data["bookings"].values()]
    for record in records:
        restored = BookingEvent.from_dict(record)
        assert booking_com_details_ready(restored)
        assert restored.guest_name == full.guest_name and restored.room_type == full.room_type
        assert restored.details_url == original.details_url
        row = excel_tsv(restored).split("\t")
        assert len(row) == 9 and row[5] == "1200000"
        assert row[8] == "Booking.com Deluxe Room x2"
    before_repeat = state.path.read_bytes()
    assert state.enrich_booking_com_manual(full, date.today())  # An unchanged save is successful.
    assert state.path.read_bytes() == before_repeat
    reloaded = StateStore(state.path)
    assert not reloaded.booking_com_candidates(date.today(), include_unlinked=True)
    assert reloaded.register_today_confirmation(original, ("synthetic-resend",), date.today()) is None
    assert len(reloaded.data["pending_alerts"]) == before_pending_count
    assert len(reloaded.data["history"]) == before_history_count


def test_manual_candidates_include_unlinked_without_changing_automated_candidates(tmp_path):
    state = StateStore(tmp_path / "state.json")
    linked = basic_event(linked=True)
    unlinked = basic_event(booking_id="5550000002")
    for event in (linked, unlinked):
        state.register_today_confirmation(event, (event.booking_id,), date.today())
    assert [event.booking_id for event in state.booking_com_candidates(date.today())] == [linked.booking_id]
    assert {event.booking_id for event in state.booking_com_candidates(date.today(), include_unlinked=True)} == {
        linked.booking_id, unlinked.booking_id,
    }
    assert state.booking_com_candidates(date.today(), limit=0, include_unlinked=True) == []
    assert not state.enrich_booking_com(complete(unlinked), date.today())
    assert state.enrich_booking_com_manual(complete(unlinked), date.today())
    assert [event.booking_id for event in state.booking_com_candidates(date.today())] == [linked.booking_id]


@pytest.mark.parametrize("invalid_detail", [
    {"total_revenue": "VND"}, {"checkout_date": date.today()}, {"guest_name": " "},
])
def test_invalid_legacy_details_remain_candidates_for_manual_repair(tmp_path, invalid_detail):
    state = StateStore(tmp_path / "state.json")
    damaged = replace(complete(basic_event()), **invalid_detail)
    state.register_today_confirmation(damaged, ("synthetic-legacy-mail",), date.today())
    assert [event.booking_id for event in state.booking_com_candidates(date.today(), include_unlinked=True)] == [
        damaged.booking_id,
    ]
    assert not state.booking_com_candidates(date.today())
    assert state.enrich_booking_com_manual(complete(basic_event()), date.today())
    assert not state.booking_com_candidates(date.today(), include_unlinked=True)


@pytest.mark.parametrize("changed", [
    {"source": "Agoda"}, {"booking_id": "5550000099"}, {"booking_id": ""},
    {"status": "cancelled"}, {"status": "modified"}, {"guest_name": " "}, {"room_type": ""},
    {"total_revenue": "VND"}, {"total_revenue": ""}, {"details_loaded_at": ""},
    {"checkin_date": date.today().isoformat()}, {"checkin_date": date.today() - timedelta(days=1)},
    {"checkin_date": date.today() + timedelta(days=1)},
    {"checkout_date": date.today()}, {"checkout_date": None},
    {"checkout_date": (date.today() + timedelta(days=2)).isoformat()},
])
def test_invalid_or_unrecognized_manual_details_leave_all_state_untouched(tmp_path, changed):
    state = StateStore(tmp_path / "state.json")
    original = basic_event()
    state.register_today_confirmation(original, ("synthetic-mail",), date.today())
    before = deepcopy(state.data), state.path.read_bytes()
    assert not state.enrich_booking_com_manual(replace(complete(original), **changed), date.today())
    assert state.data == before[0] and state.path.read_bytes() == before[1]


@pytest.mark.parametrize("lifecycle", ["cancelled", "modified", "different-stay"])
def test_manual_repair_rejects_stale_form_after_saved_booking_changes(tmp_path, lifecycle):
    state = StateStore(tmp_path / "state.json")
    original = basic_event(linked=True)
    pending = state.register_today_confirmation(original, ("synthetic-mail",), date.today())
    state.acknowledge(pending)
    current = state.data["bookings"][original.storage_id]
    if lifecycle == "different-stay":
        current["checkin_date"] = (date.today() + timedelta(days=1)).isoformat()
    else:
        current["status"] = lifecycle
    before = deepcopy(state.data)
    assert not state.enrich_booking_com_manual(complete(original), date.today())
    assert state.data == before


def test_manual_repair_never_overwrites_links_with_user_supplied_or_other_hotel_url(tmp_path):
    state = StateStore(tmp_path / "state.json")
    original = basic_event(linked=True)
    state.register_today_confirmation(original, ("synthetic-mail",), date.today())
    full = replace(complete(original), details_url="https://example.invalid/?token=synthetic-secret")
    assert state.enrich_booking_com_manual(full, date.today())
    assert all(record["details_url"] == original.details_url for record in
               [*state.data["pending_alerts"], *state.data["bookings"].values()])
    assert "synthetic-secret" not in state.path.read_text(encoding="utf-8")


def test_manual_repair_supports_legacy_closed_history_without_creating_cache_or_alert(tmp_path):
    state = StateStore(tmp_path / "state.json")
    original = basic_event()
    state.data["history"].append({**original.to_dict(), "acknowledged_at": "legacy-closed"})
    assert state.enrich_booking_com_manual(complete(original), date.today())
    assert not state.data["bookings"] and not state.data["pending_alerts"]
    assert len(state.history()) == 1 and state.history()[0]["acknowledged_at"] == "legacy-closed"


def test_manual_repair_cannot_create_an_unknown_booking(tmp_path):
    state = StateStore(tmp_path / "state.json")
    before = deepcopy(state.data)
    assert not state.enrich_booking_com_manual(complete(basic_event()), date.today())
    assert state.data == before and not state.path.exists()


def test_immediate_stale_popup_close_keeps_manual_corrections_in_history_and_excel(tmp_path):
    state = StateStore(tmp_path / "state.json")
    original = replace(basic_event(), guest_name="WRONG OLD NAME THAT IS LONGER", room_type="Old Room x1",
                       checkout_date=date.today() + timedelta(days=1), total_revenue="VND 300,000")
    stale_popup = state.register_today_confirmation(original, ("synthetic-mail",), date.today())
    full = replace(complete(original), guest_name="CORRECT NAME")
    assert state.enrich_booking_com_manual(full, date.today())
    state.acknowledge(stale_popup)  # Before the UI has consumed history_changed.
    history = BookingEvent.from_dict(StateStore(state.path).history()[0])
    assert excel_tsv(history) == excel_tsv(full)
    assert history.details_loaded_at == full.details_loaded_at
    assert not state.pending_for_date(date.today()) and len(state.history()) == 1
