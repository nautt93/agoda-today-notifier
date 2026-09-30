from __future__ import annotations

import re
import tomllib
from pathlib import Path

from booking_notifier.config import APP_VERSION
from booking_notifier.ota_update import USER_AGENT

ROOT = Path(__file__).resolve().parents[1]


def test_release_version_is_consistent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version_info = (ROOT / "version_info.txt").read_text(encoding="utf-8")
    numeric = ", ".join(APP_VERSION.split(".")) + ", 0"

    assert project["project"]["version"] == APP_VERSION
    assert USER_AGENT.endswith("/" + APP_VERSION)
    assert f"filevers=({numeric})" in version_info
    assert f"prodvers=({numeric})" in version_info
    assert len(re.findall(rf"u'{re.escape(APP_VERSION)}'", version_info)) == 2
