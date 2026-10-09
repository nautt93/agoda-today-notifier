# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_dir = Path(SPECPATH)

a = Analysis(
    [str(project_dir / "app.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[
        (str(project_dir / "assets" / "sounds" / filename), "assets/sounds")
        for filename in ("1-agoda.mp3", "2-expedia.mp3", "3-traveloka.mp3", "4-trip.mp3", "5-booking-com.mp3",
                         "1-agoda-pcm.wav", "2-expedia-pcm.wav", "3-traveloka-pcm.wav", "4-trip-pcm.wav", "5-booking-com-pcm.wav", "manifest.json")
    ],
    hiddenimports=[
        "PIL.ImageTk",
        "pystray._win32",
        "serial.tools.list_ports_windows",
        "cryptography.hazmat.primitives.asymmetric.ed25519",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pystray._darwin", "pystray._appindicator", "pystray._gtk", "pystray._xorg", "playwright"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Booking-Checkin-Hom-Nay",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(project_dir / "version_info.txt"),
)
