"""One-page native Windows printing. No temporary email/PDF/card files."""
from __future__ import annotations

import ctypes as ct
import os
from typing import Any

from PIL import Image

from .expedia_print import ExpediaPrintError

DWORD, WORD, HANDLE = ct.c_uint32, ct.c_uint16, ct.c_void_p


class PRINTDLGW(ct.Structure):
    _pack_ = 8 if ct.sizeof(HANDLE) == 8 else 1  # commdlg.h packs 32-bit structs at one byte.
    _fields_ = [
        ("lStructSize", DWORD), ("hwndOwner", HANDLE), ("hDevMode", HANDLE),
        ("hDevNames", HANDLE), ("hDC", HANDLE), ("Flags", DWORD),
        ("nFromPage", WORD), ("nToPage", WORD), ("nMinPage", WORD), ("nMaxPage", WORD),
        ("nCopies", WORD), ("hInstance", HANDLE), ("lCustData", ct.c_ssize_t),
        ("lpfnPrintHook", HANDLE), ("lpfnSetupHook", HANDLE),
        ("lpPrintTemplateName", ct.c_wchar_p), ("lpSetupTemplateName", ct.c_wchar_p),
        ("hPrintTemplate", HANDLE), ("hSetupTemplate", HANDLE),
    ]


class DEVMODE_PREFIX(ct.Structure):
    # Public Unicode printer fields through dmPrintQuality; retain ALL driver extra bytes.
    _fields_ = [("dmDeviceName", WORD * 32), ("dmSpecVersion", WORD), ("dmDriverVersion", WORD),
                ("dmSize", WORD), ("dmDriverExtra", WORD), ("dmFields", DWORD)] + [
        (name, ct.c_int16) for name in ("dmOrientation", "dmPaperSize", "dmPaperLength", "dmPaperWidth",
                                      "dmScale", "dmCopies", "dmDefaultSource", "dmPrintQuality")
    ]


class DOCINFOW(ct.Structure):
    _fields_ = [("cbSize", ct.c_int32), ("lpszDocName", ct.c_wchar_p),
                ("lpszOutput", ct.c_wchar_p), ("lpszDatatype", ct.c_wchar_p), ("fwType", DWORD)]


class BITMAPINFOHEADER(ct.Structure):
    _fields_ = [("biSize", DWORD), ("biWidth", ct.c_int32), ("biHeight", ct.c_int32),
                ("biPlanes", WORD), ("biBitCount", WORD), ("biCompression", DWORD),
                ("biSizeImage", DWORD), ("biXPelsPerMeter", ct.c_int32),
                ("biYPelsPerMeter", ct.c_int32), ("biClrUsed", DWORD), ("biClrImportant", DWORD)]


def _native() -> tuple[Any, Any, Any]:
    if os.name != "nt":
        raise ExpediaPrintError("Tính năng in dùng máy in Windows.")
    gdi = ct.WinDLL("gdi32", use_last_error=True)
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    dialog = ct.WinDLL("comdlg32", use_last_error=True)
    dialog.PrintDlgW.argtypes, dialog.PrintDlgW.restype = [ct.POINTER(PRINTDLGW)], ct.c_int32
    dialog.CommDlgExtendedError.restype = DWORD
    kernel.GlobalLock.argtypes, kernel.GlobalLock.restype = [HANDLE], HANDLE
    kernel.GlobalSize.argtypes, kernel.GlobalSize.restype = [HANDLE], ct.c_size_t
    kernel.GlobalUnlock.argtypes, kernel.GlobalUnlock.restype = [HANDLE], ct.c_int32
    kernel.GlobalFree.argtypes, kernel.GlobalFree.restype = [HANDLE], HANDLE
    for name in ("DeleteDC", "StartPage", "EndPage", "EndDoc", "AbortDoc"):
        function = getattr(gdi, name)
        function.argtypes, function.restype = [HANDLE], ct.c_int32
    gdi.ResetDCW.argtypes, gdi.ResetDCW.restype = [HANDLE, HANDLE], HANDLE
    gdi.GetDeviceCaps.argtypes, gdi.GetDeviceCaps.restype = [HANDLE, ct.c_int32], ct.c_int32
    gdi.StartDocW.argtypes, gdi.StartDocW.restype = [HANDLE, ct.POINTER(DOCINFOW)], ct.c_int32
    gdi.StretchDIBits.argtypes = [HANDLE] + [ct.c_int32] * 8 + [HANDLE, ct.POINTER(BITMAPINFOHEADER), DWORD, DWORD]
    gdi.StretchDIBits.restype = ct.c_int32
    return gdi, kernel, dialog


def set_a4_mode(mode: DEVMODE_PREFIX) -> None:
    mode.dmOrientation, mode.dmPaperSize, mode.dmCopies, mode.dmScale = 1, 9, 1, 100
    mode.dmPaperLength = mode.dmPaperWidth = 0
    # Clear custom width/length/form-name; request portrait A4 and exactly one copy.
    mode.dmFields = (mode.dmFields & ~(0x4 | 0x8 | 0x10000)) | 0x1 | 0x2 | 0x100 | 0x10


class PrinterJob:
    def __init__(self, hdc: int, gdi: Any) -> None:
        self.hdc, self.gdi = hdc, gdi

    def close(self) -> None:
        if self.hdc:
            self.gdi.DeleteDC(self.hdc)
            self.hdc = 0

    def print_page(self, image: Image.Image, output: str | None = None) -> None:
        started = False
        try:
            width = self.gdi.GetDeviceCaps(self.hdc, 8)  # HORZRES: printable area
            height = self.gdi.GetDeviceCaps(self.hdc, 10)  # VERTRES
            if width <= 0 or height <= 0:
                raise ExpediaPrintError("Máy in không trả về khổ giấy hợp lệ.")
            factor = min(width / image.width, height / image.height)
            target_width, target_height = round(image.width * factor), round(image.height * factor)
            x, y = (width - target_width) // 2, (height - target_height) // 2
            # A neutral spool title avoids guest/card details in the Windows print queue.
            info = DOCINFOW(ct.sizeof(DOCINFOW), "Booking Desk - Expedia A4", output, None, 0)
            if self.gdi.StartDocW(self.hdc, ct.byref(info)) <= 0:
                raise ExpediaPrintError("Không tạo được lệnh in. Kiểm tra máy in và thử lại.")
            started = True
            if self.gdi.StartPage(self.hdc) <= 0:
                raise ExpediaPrintError("Không mở được trang in.")
            # Top-down uncompressed 32-bit BGRX DIB: no external images or HTML execution.
            bits = image.convert("RGB").tobytes("raw", "BGRX")
            buffer = ct.create_string_buffer(bits)
            header = BITMAPINFOHEADER(ct.sizeof(BITMAPINFOHEADER), image.width, -image.height,
                                      1, 32, 0, len(bits), 0, 0, 0, 0)
            copied = self.gdi.StretchDIBits(self.hdc, x, y, target_width, target_height,
                                           0, 0, image.width, image.height, buffer, ct.byref(header), 0, 0x00CC0020)
            if copied <= 0 or self.gdi.EndPage(self.hdc) <= 0 or self.gdi.EndDoc(self.hdc) <= 0:
                raise ExpediaPrintError("Máy in chưa nhận đủ trang. Lệnh in đã được hủy; hãy kiểm tra và thử lại.")
            started = False
        finally:
            if started:
                self.gdi.AbortDoc(self.hdc)
            self.close()


def choose_printer(owner: int = 0) -> PrinterJob | None:
    gdi, kernel, dialog = _native()
    selection = PRINTDLGW()
    selection.lStructSize = ct.sizeof(PRINTDLGW)
    selection.hwndOwner = owner
    selection.nFromPage = selection.nToPage = selection.nMinPage = selection.nMaxPage = selection.nCopies = 1
    # No page range/selection/Print-to-file checkbox; PDF printer is still available explicitly.
    flags = 0x100 | 0x4 | 0x8 | 0x40000 | 0x100000

    def configure_a4(reset: bool = False) -> None:
        if not selection.hDevMode or kernel.GlobalSize(selection.hDevMode) < ct.sizeof(DEVMODE_PREFIX):
            raise ExpediaPrintError("Máy in không hỗ trợ cấu hình giấy A4.")
        pointer = kernel.GlobalLock(selection.hDevMode)
        if not pointer:
            raise ExpediaPrintError("Không đọc được cấu hình máy in.")
        try:
            mode = DEVMODE_PREFIX.from_address(pointer)
            if mode.dmSize < ct.sizeof(DEVMODE_PREFIX):
                raise ExpediaPrintError("Cấu hình máy in không hợp lệ.")
            set_a4_mode(mode)
            if reset:
                hdc = gdi.ResetDCW(selection.hDC, pointer)
                if not hdc:
                    raise ExpediaPrintError("Máy in không nhận cấu hình giấy A4.")
                selection.hDC = hdc
        finally:
            kernel.GlobalUnlock(selection.hDevMode)

    try:
        # Prefer A4 before opening the dialog. No default printer? Still allow a choice.
        selection.Flags = 0x400  # PD_RETURNDEFAULT (no DC)
        if dialog.PrintDlgW(ct.byref(selection)):
            configure_a4()
        selection.Flags = flags
        if not dialog.PrintDlgW(ct.byref(selection)):
            if dialog.CommDlgExtendedError():
                raise ExpediaPrintError("Không mở được hộp chọn máy in. Hãy kiểm tra máy in Windows.")
            return None  # Cancel means no print, no booking acknowledgment.
        configure_a4(reset=True)
        if not selection.hDC:
            raise ExpediaPrintError("Không kết nối được máy in đã chọn.")
        dpi_x, dpi_y = gdi.GetDeviceCaps(selection.hDC, 88), gdi.GetDeviceCaps(selection.hDC, 90)
        physical_x, physical_y = gdi.GetDeviceCaps(selection.hDC, 110), gdi.GetDeviceCaps(selection.hDC, 111)
        if (dpi_x <= 0 or dpi_y <= 0 or abs(physical_x * 25.4 / dpi_x - 210) > 3
                or abs(physical_y * 25.4 / dpi_y - 297) > 3):
            raise ExpediaPrintError("Máy in chưa nhận khổ A4 dọc. Hãy kiểm tra thiết lập giấy và thử lại.")
        job = PrinterJob(selection.hDC, gdi)
        selection.hDC = None  # Ownership moves to PrinterJob.
        return job
    finally:
        if selection.hDC:
            gdi.DeleteDC(selection.hDC)
        for handle in (selection.hDevMode, selection.hDevNames):
            if handle:
                kernel.GlobalFree(handle)
