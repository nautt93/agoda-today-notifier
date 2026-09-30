"""A frozen old-app stand-in for the Windows packaged OTA integration check."""

import sys
from pathlib import Path

from booking_notifier.ota_update import launch_self_update

if __name__ == "__main__":
    launch_self_update(Path(sys.argv[1]), Path(sys.executable), sys.argv[2])
