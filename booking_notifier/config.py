from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

APP_NAME = "Booking Check-in Hôm nay"
APP_VERSION = "1.7.0"
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
    "f92_enabled": True,
    "f92_port": "AUTO",
    "f92_sound_index": 4,
    "f92_builtin_sound_enabled": False,
    "quiet_hours_enabled": True,
    "start_with_windows": True,
    "start_minimized": False,
    "update_manifest_source": PUBLIC_UPDATE_MANIFEST_URL,
}


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
        config["poll_seconds"] = min(3600, max(30, int(config.get("poll_seconds", 60))))
        config["scan_days"] = min(365, max(1, int(config.get("scan_days", 90))))
        return config

    def save(self, config: dict[str, Any]) -> None:
        value = dict(DEFAULT_CONFIG)
        value.update({key: item for key, item in config.items() if key in DEFAULT_CONFIG})
        atomic_json_write(self.path, value)
