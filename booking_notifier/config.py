from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .quiet_hours import normalize_quiet_times

APP_NAME = "Booking Check-in Hôm nay"
APP_VERSION = "1.7.28"
APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "AgodaTodayNotifier"
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "state.json"
LOG_PATH = APP_DIR / "app.log"
PUBLIC_UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/nautt93/agoda-today-notifier/main/update.json"

PROVIDERS: dict[str, tuple[str, int]] = {
    "Gmail": ("imap.gmail.com", 993),
    "Yahoo Mail": ("imap.mail.yahoo.com", 993),
    "iCloud Mail": ("imap.mail.me.com", 993),
    "Khác (IMAP)": ("", 993),
}

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "Gmail",
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "email_address": "",
    "password_encrypted": "",
    "poll_seconds": 60,
    "scan_days": 90,
    "sound_file": "",
    "agoda_sound_file": "",
    "expedia_sound_file": "",
    "traveloka_sound_file": "",
    "trip_sound_file": "",
    "booking_com_sound_file": "",
    "source_sound_pack": "",
    "trip_sound_pack": "",
    "booking_com_sound_pack": "",
    "f92_enabled": True,
    "f92_port": "AUTO",
    "f92_sound_index": 4,
    "f92_builtin_sound_enabled": False,
    "quiet_hours_enabled": True,
    "quiet_start_time": "00:00",
    "quiet_end_time": "08:00",
    "start_with_windows": True,
    "start_minimized": False,
    "update_manifest_source": PUBLIC_UPDATE_MANIFEST_URL,
}


def _integer_or_default(value: Any, default: int) -> int:
    """Read legacy settings without allowing malformed numbers to prevent startup."""
    try:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return default
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ConfigStore:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        loaded: dict[str, Any] = {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                loaded = value
        except (OSError, ValueError):
            pass
        config = dict(DEFAULT_CONFIG)
        config.update({key: value for key, value in loaded.items() if key in DEFAULT_CONFIG})
        # Migrate the default OTA endpoint from the retired account without overriding custom/local sources.
        source = str(config.get("update_manifest_source", ""))
        if source == "https://raw.githubusercontent.com/xamtro/agoda-today-notifier/main/update.json":
            config["update_manifest_source"] = PUBLIC_UPDATE_MANIFEST_URL
        config["poll_seconds"] = min(3600, max(30, _integer_or_default(config.get("poll_seconds"), 60)))
        config["scan_days"] = min(365, max(1, _integer_or_default(config.get("scan_days"), 90)))
        port = _integer_or_default(config.get("imap_port"), DEFAULT_CONFIG["imap_port"])
        config["imap_port"] = port if 1 <= port <= 65535 else DEFAULT_CONFIG["imap_port"]
        try:
            config["quiet_start_time"], config["quiet_end_time"] = normalize_quiet_times(
                config["quiet_start_time"], config["quiet_end_time"],
            )
        except ValueError:
            # Repair the pair together; never combine one bad legacy endpoint
            # with the other and accidentally suppress an entire workday.
            config["quiet_start_time"] = DEFAULT_CONFIG["quiet_start_time"]
            config["quiet_end_time"] = DEFAULT_CONFIG["quiet_end_time"]
        return config

    def save(self, config: dict[str, Any]) -> None:
        value = dict(DEFAULT_CONFIG)
        value.update({key: item for key, item in config.items() if key in DEFAULT_CONFIG})
        # Validate before creating/replacing any file, even when quiet hours
        # are disabled, so an invalid draft cannot overwrite working settings.
        value["quiet_start_time"], value["quiet_end_time"] = normalize_quiet_times(
            value["quiet_start_time"], value["quiet_end_time"],
        )
        atomic_json_write(self.path, value)

    def install_source_sound_pack(self, directory: Path) -> dict[str, Any]:
        """Install each requested sound pack once, including existing OTA profiles.

        Subsequent custom source selections survive restarts/updates. Prepare all
        MP3/PCM files before changing settings, so a missing asset cannot partly
        replace the saved choices or mark an incomplete pack as installed.
        """
        from .audio import (
            BOOKING_COM_SOUND_PACK,
            SOURCE_SOUND_KEYS,
            SOURCE_SOUND_PACK,
            TRIP_SOUND_PACK,
            bundled_source_pcm,
            bundled_source_sound,
        )

        config = self.load()
        install_legacy = config.get("source_sound_pack") != SOURCE_SOUND_PACK
        install_trip = config.get("trip_sound_pack") != TRIP_SOUND_PACK
        install_booking_com = config.get("booking_com_sound_pack") != BOOKING_COM_SOUND_PACK
        if not install_legacy and not install_trip and not install_booking_com:
            return config
        paths: dict[str, str] = {}
        if install_legacy:
            # Adding a provider must never change the original three-source pack.
            legacy_sources = ("agoda", "expedia", "traveloka")
            paths.update({SOURCE_SOUND_KEYS[source]: str(bundled_source_sound(source, directory))
                          for source in legacy_sources})
            for source in legacy_sources:
                bundled_source_pcm(source, directory)
        if install_trip:
            trip_path = bundled_source_sound("Trip", directory)
            bundled_source_pcm("Trip", directory)
            if not str(config.get("trip_sound_file") or "").strip():
                paths["trip_sound_file"] = str(trip_path)
        if install_booking_com:
            booking_com_path = bundled_source_sound("Booking.com", directory)
            bundled_source_pcm("Booking.com", directory)
            if not str(config.get("booking_com_sound_file") or "").strip():
                paths["booking_com_sound_file"] = str(booking_com_path)
        config.update(paths)
        if install_legacy:
            config["source_sound_pack"] = SOURCE_SOUND_PACK
        if install_trip:
            config["trip_sound_pack"] = TRIP_SOUND_PACK
        if install_booking_com:
            config["booking_com_sound_pack"] = BOOKING_COM_SOUND_PACK
        self.save(config)
        return config
