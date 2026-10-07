"""Headless popup actions preserve clipboard, alert ownership and muted sound."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

import app as desktop
from app import BookingNotifierApp
from booking_notifier.excel_export import excel_tsv
from booking_notifier.models import BookingEvent
from booking_notifier.popup_state import booking_com_popup_state


def complete_event():
    return BookingEvent(
        source="Booking.com", booking_id="5550000001", checkin_date=date.today(),
        checkout_date=date.today() + timedelta(days=2), guest_name="SYNTHETIC FULL GUEST",
        room_type="Deluxe Room x2", total_revenue="VND 800.000", details_loaded_at="2026-10-07T12:00:00",
        details_url="https://admin.booking.com/hotel/hoteladmin/extranet_ng/manage/booking.html?res_id=5550000001&hotel_id=12345&lang=vi",
    )


def popup_app(event, *, enrichment_enabled=True):
    """Instantiate no Tk widgets and call no app/network/audio startup code."""
    instance = BookingNotifierApp.__new__(BookingNotifierApp)
    instance.root, instance.state, instance.f92_worker = Mock(), Mock(), Mock()
    instance.active_alert, instance.active_popup = event, Mock()
    instance.config = {"booking_com_enrichment": enrichment_enabled}
    instance.booking_com_popup_status = ""
    instance.sound_active, instance.active_sound_muted = True, False
    next_event = BookingEvent(source="Agoda", booking_id="NEXT-QUEUED", checkin_date=date.today())
    instance.alert_queue = [next_event]
    instance.queued_ids = {next_event.storage_id, *([event.storage_id] if event else [])}
    for attribute in (
        "active_booking_title_var", "active_booking_details_var", "active_booking_status_label",
        "active_copy_button", "active_mute_button", "active_menu", "copy_feedback_var",
        "active_guest_var", "active_room_var", "active_revenue_var", "active_checkout_var", "active_nights_var",
        "play_sound", "_show_next_alert", "acknowledge_alert", "_fit_popup_guest_header", "set_status",
    ):
        setattr(instance, attribute, Mock())

    def stop():
        instance.sound_active = False

    instance.stop_sound = Mock(side_effect=stop)
    return instance


def assert_no_ack_or_advance(instance, alert, popup, queued, ids):
    assert instance.active_alert is alert and instance.active_popup is popup
    assert instance.alert_queue == queued and instance.queued_ids == ids
    instance.acknowledge_alert.assert_not_called()
    instance.state.acknowledge.assert_not_called()
    instance._show_next_alert.assert_not_called()
    popup.destroy.assert_not_called()


def test_mute_stops_once_disables_button_and_never_acknowledges_or_advances_pending_notification():
    event = replace(complete_event(), guest_name="", room_type="")
    instance = popup_app(event)
    popup, queued, ids = instance.active_popup, list(instance.alert_queue), set(instance.queued_ids)
    instance.mute_active_alert()
    instance.mute_active_alert()
    instance.stop_sound.assert_called_once()
    assert instance.active_sound_muted and not instance.sound_active
    instance.active_mute_button.configure.assert_called_once_with(text="Đã tắt chuông", state="disabled")
    message = instance.active_booking_details_var.set.call_args.args[0]
    assert "Chuông đã tắt" in message and "vẫn chờ thông tin" in message
    assert "Bạn có thể tắt âm" not in message
    instance.play_sound.assert_not_called()
    instance.f92_worker.notify.assert_not_called()
    assert_no_ack_or_advance(instance, event, popup, queued, ids)


@pytest.mark.parametrize("source", [None, "Agoda", "Expedia", "Traveloka", "Trip"])
def test_mute_is_guarded_to_active_booking_com_source_only(source):
    event = replace(complete_event(), source=source) if source else None
    instance = popup_app(event)
    popup, queued, ids = instance.active_popup, list(instance.alert_queue), set(instance.queued_ids)
    instance.mute_active_alert()
    assert not instance.active_sound_muted and instance.sound_active
    instance.stop_sound.assert_not_called()
    instance.active_mute_button.configure.assert_not_called()
    assert_no_ack_or_advance(instance, event, popup, queued, ids)


def test_copy_pending_preserves_existing_clipboard_sound_and_popup_without_modal(monkeypatch):
    instance = popup_app(replace(complete_event(), room_type=""))
    instance.root.clipboard_get.return_value = "existing\tclipboard\tcontent"
    delegate, modal = Mock(), Mock()
    instance._copy_alerts_to_clipboard = delegate
    monkeypatch.setattr(desktop.messagebox, "showinfo", modal)
    event, popup = instance.active_alert, instance.active_popup
    queued, ids = list(instance.alert_queue), set(instance.queued_ids)
    instance.copy_active_alert()
    delegate.assert_not_called()
    modal.assert_not_called()
    instance.root.clipboard_clear.assert_not_called()
    instance.root.clipboard_append.assert_not_called()
    assert instance.root.clipboard_get() == "existing\tclipboard\tcontent"
    instance.active_copy_button.configure.assert_called_once_with(state="disabled")
    instance.copy_feedback_var.set.assert_called_once_with("Sao chép sẽ mở khi đã lấy đủ thông tin booking.")
    instance.stop_sound.assert_not_called()
    assert instance.sound_active
    assert_no_ack_or_advance(instance, event, popup, queued, ids)


def test_copy_without_active_notification_does_nothing(monkeypatch):
    instance = popup_app(None)
    delegate, modal = Mock(), Mock()
    instance._copy_alerts_to_clipboard = delegate
    monkeypatch.setattr(desktop.messagebox, "showinfo", modal)
    instance.copy_active_alert()
    delegate.assert_not_called()
    modal.assert_not_called()
    instance.root.clipboard_clear.assert_not_called()
    instance.root.clipboard_append.assert_not_called()
    instance.copy_feedback_var.set.assert_not_called()


@pytest.mark.parametrize("copied", [False, True])
def test_copy_ready_delegates_existing_excel_workflow_without_ack_or_audio_change(copied):
    event = replace(complete_event(), details_loaded_at="")
    instance = popup_app(event)
    delegate = Mock(return_value=copied)
    instance._copy_alerts_to_clipboard = delegate
    popup, queued, ids = instance.active_popup, list(instance.alert_queue), set(instance.queued_ids)
    instance.copy_active_alert()
    delegate.assert_called_once_with([event], "Đã sao chép booking sang Excel")
    if copied:
        instance.copy_feedback_var.set.assert_called_once_with("Đã sao chép • Mở Excel và nhấn Ctrl+V")
    else:
        instance.copy_feedback_var.set.assert_not_called()
    assert instance.sound_active and not instance.active_sound_muted
    instance.stop_sound.assert_not_called()
    instance.play_sound.assert_not_called()
    assert_no_ack_or_advance(instance, event, popup, queued, ids)


def test_ready_copy_uses_exact_existing_nine_column_excel_row_without_modal(monkeypatch):
    event = replace(complete_event(), details_loaded_at="")
    instance, modal = popup_app(event), Mock()
    monkeypatch.setattr(desktop.messagebox, "showinfo", modal)
    instance.copy_active_alert()
    expected = excel_tsv(event)
    assert len(expected.split("\t")) == 9
    instance.root.clipboard_clear.assert_called_once()
    instance.root.clipboard_append.assert_called_once_with(expected)
    instance.root.update_idletasks.assert_called_once()
    instance.set_status.assert_called_once_with("Đã sao chép booking sang Excel")
    modal.assert_not_called()
    instance.stop_sound.assert_not_called()
    instance.state.acknowledge.assert_not_called()


def test_detail_completion_updates_same_popup_and_excel_state_without_restarting_muted_sound():
    complete = complete_event()
    event = replace(complete, guest_name="", room_type="", checkout_date=None, total_revenue="", details_loaded_at="")
    instance = popup_app(event)
    popup, queued, ids = instance.active_popup, list(instance.alert_queue), set(instance.queued_ids)
    instance.state.pending_for_date.return_value = [complete]
    instance.booking_com_popup_status = "Booking.com 5550000002: cần đăng nhập"
    instance.mute_active_alert()
    instance._refresh_pending_details()
    assert instance.active_sound_muted and not instance.sound_active
    instance.stop_sound.assert_called_once()
    instance.play_sound.assert_not_called()
    instance.active_mute_button.configure.assert_called_once_with(text="Đã tắt chuông", state="disabled")
    instance.active_booking_title_var.set.assert_called_with("Đã lấy đủ thông tin")
    instance.active_copy_button.configure.assert_called_with(state="normal")
    instance.active_menu.entryconfigure.assert_called_with(0, state="normal")
    instance.active_guest_var.set.assert_called_once_with(complete.guest_name)
    instance.active_room_var.set.assert_called_once_with(complete.room_type)
    instance.f92_worker.notify.assert_called_once_with(event, play_sound=False)
    instance._fit_popup_guest_header.assert_called()
    assert event.guest_name == complete.guest_name and event.room_type == complete.room_type
    assert_no_ack_or_advance(instance, event, popup, queued, ids)
    instance.copy_active_alert()
    instance.root.clipboard_append.assert_called_once_with(excel_tsv(event))
    assert instance.active_sound_muted and not instance.sound_active
    instance.stop_sound.assert_called_once()
    instance.play_sound.assert_not_called()
    assert_no_ack_or_advance(instance, event, popup, queued, ids)


@pytest.mark.parametrize("field", ["guest_name", "room_type", "checkout_date", "total_revenue"])
def test_popup_timestamp_cannot_enable_copy_when_actual_field_is_missing(field):
    event = replace(complete_event(), **{field: None if field == "checkout_date" else ""})
    instance = popup_app(event)
    instance._refresh_booking_popup_state()
    instance.active_copy_button.configure.assert_called_once_with(state="disabled")
    instance.active_menu.entryconfigure.assert_called_once_with(0, state="disabled")
    instance.active_booking_title_var.set.assert_called_once_with("Vui lòng đợi lấy thông tin")
    instance.stop_sound.assert_not_called()
    instance.active_mute_button.configure.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_popup_complete_legacy_fields_enable_copy_even_without_timestamp_or_with_config_disabled(enabled):
    event = replace(complete_event(), details_loaded_at="")
    instance = popup_app(event, enrichment_enabled=enabled)
    instance.booking_com_popup_status = "Booking.com 5550000002: chưa lấy được chi tiết"
    instance._refresh_booking_popup_state()
    instance.active_copy_button.configure.assert_called_once_with(state="normal")
    instance.active_menu.entryconfigure.assert_called_once_with(0, state="normal")
    instance.active_booking_title_var.set.assert_called_once_with("Đã lấy đủ thông tin")
    instance.stop_sound.assert_not_called()


def test_popup_disabled_config_gives_honest_enable_guidance_not_other_id_or_raw_status():
    event = replace(complete_event(), room_type="")
    instance = popup_app(event, enrichment_enabled=False)
    instance.booking_com_popup_status = "Booking.com 5550000002: cần đăng nhập https://admin.booking.com/?ses=secret otp=654321"
    instance._refresh_booking_popup_state()
    expected = booking_com_popup_state(event, enrichment_enabled=False, worker_status=instance.booking_com_popup_status)
    instance.active_booking_title_var.set.assert_called_once_with(expected.title)
    instance.active_booking_details_var.set.assert_called_once_with(expected.message)
    assert "Cài đặt" in expected.message and "bật" in expected.message
    assert all(raw not in expected.message for raw in ("5550000002", "https://", "secret", "654321"))
    instance.active_copy_button.configure.assert_called_once_with(state="disabled")
    instance.state.acknowledge.assert_not_called()


def test_popup_without_details_link_is_unavailable_not_automatically_loading_or_retrying():
    event = replace(complete_event(), room_type="", details_url="")
    instance = popup_app(event)
    instance.booking_com_popup_status = "Booking.com: chưa lấy được chi tiết"
    instance._refresh_booking_popup_state()
    instance.active_booking_title_var.set.assert_called_once_with("Chưa có liên kết chi tiết")
    message = instance.active_booking_details_var.set.call_args.args[0]
    assert "Extranet Booking.com" in message and "chưa thể tự bổ sung" in message
    assert "tự thử lại" not in message
    instance.active_copy_button.configure.assert_called_once_with(state="disabled")


def test_popup_known_manual_login_status_explicitly_requests_config_login_not_loading():
    event = replace(complete_event(), room_type="")
    instance = popup_app(event)
    instance.booking_com_popup_status = "Booking.com: hoàn tất đăng nhập/2FA trong cửa sổ Edge/Chrome của app. Chi tiết sẽ tự bổ sung."
    instance._refresh_booking_popup_state()
    instance.active_booking_title_var.set.assert_called_once_with("Cần đăng nhập Booking.com")
    message = instance.active_booking_details_var.set.call_args.args[0]
    assert "Cài đặt → Đăng nhập Booking.com" in message and "OTP" in message
    instance.active_copy_button.configure.assert_called_once_with(state="disabled")


@pytest.mark.parametrize("status", [
    "Booking.com: cần đăng nhập https://admin.booking.com/?ses=secret otp=654321",
    "Booking.com 5550000002: lỗi kết nối; Cookie=secret",
    "Booking.com 5550000002: đã lấy đầy đủ thông tin",
])
def test_popup_status_variables_receive_only_safe_static_state_messages(status):
    event = replace(complete_event(), room_type="")
    instance = popup_app(event)
    instance.booking_com_popup_status = status
    instance._refresh_booking_popup_state()
    expected = booking_com_popup_state(event, worker_status=status)
    instance.active_booking_title_var.set.assert_called_once_with(expected.title)
    instance.active_booking_details_var.set.assert_called_once_with(expected.message)
    assert all(raw not in expected.title + expected.message for raw in ("5550000002", "https://", "secret", "654321"))


@pytest.mark.parametrize("source", [None, "Agoda", "Expedia", "Traveloka", "Trip"])
def test_booking_popup_state_refresh_does_not_mutate_other_sources(source):
    event = replace(complete_event(), source=source) if source else None
    instance = popup_app(event)
    instance._refresh_booking_popup_state()
    instance.active_booking_title_var.set.assert_not_called()
    instance.active_booking_details_var.set.assert_not_called()
    instance.active_copy_button.configure.assert_not_called()
    instance.active_menu.entryconfigure.assert_not_called()
    instance.stop_sound.assert_not_called()


def test_popup_state_refresh_with_legacy_new_mock_without_config_defaults_to_loading():
    instance = popup_app(replace(complete_event(), room_type=""))
    del instance.config
    instance._refresh_booking_popup_state()
    instance.active_booking_title_var.set.assert_called_once_with("Vui lòng đợi lấy thông tin")
    instance.active_copy_button.configure.assert_called_once_with(state="disabled")
