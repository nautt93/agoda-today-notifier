from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from booking_notifier.audio import (
    SOURCE_SOUND_FILES,
    SOURCE_SOUND_KEYS,
    WindowsMciAudioPlayer,
    bundled_source_pcm,
    bundled_source_sound,
    configured_sound_paths,
    default_source_sound,
)
from booking_notifier.booking_com_browser import BookingComWorker
from booking_notifier.config import (
    APP_DIR,
    APP_NAME,
    APP_VERSION,
    LOG_PATH,
    PROVIDERS,
    ConfigStore,
)
from booking_notifier.excel_export import excel_tsv, excel_tsv_rows  # noqa: F401 (public compatibility)
from booking_notifier.expedia_print import ExpediaPrintError, fetch_expedia_print, render_booking_a4, render_expedia_a4
from booking_notifier.f92_device import F92Worker
from booking_notifier.mail_monitor import ImapMonitor, is_quiet_hours, test_imap_connection
from booking_notifier.models import BOOKING_SOURCES, BookingEvent
from booking_notifier.ota_update import (
    check_for_update,
    download_update,
    launch_self_update,
    report_update_startup,
    run_update_helper_from_argv,
)
from booking_notifier.security import protect_secret, unprotect_secret
from booking_notifier.state import StateStore
from booking_notifier.system_tray import SystemTray
from booking_notifier.traveloka_print import fetch_traveloka_print
from booking_notifier.windows_print import choose_printer

LOGGER = logging.getLogger("booking_notifier")


def setup_logging() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)


class WindowsSingleInstance:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str = "Local\\BookingDesk.SingleInstance") -> None:
        self.name = name
        self.handle: Any = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        import ctypes

        self.handle = ctypes.windll.kernel32.CreateMutexW(None, False, self.name)
        return bool(self.handle) and ctypes.windll.kernel32.GetLastError() != self.ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle and os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def executable_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def set_start_with_windows(enabled: bool) -> None:
    if os.name != "nt":
        return
    import winreg

    path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "BookingDesk", 0, winreg.REG_SZ, executable_command())
        else:
            try:
                winreg.DeleteValue(key, "BookingDesk")
            except FileNotFoundError:
                pass


class BookingNotifierApp:
    COLORS = {
        "bg": "#F5F2EC",
        "surface": "#FFFFFF",
        "surface_alt": "#FAF8F4",
        "text": "#1B2430",
        "muted": "#697386",
        "border": "#DED8CE",
        "primary": "#172A42",
        "primary_hover": "#213B5B",
        "gold": "#B68A3A",
        "gold_hover": "#98712E",
        "success": "#48C79A",
        "warning": "#D49A45",
        "danger": "#C95B5B",
        "agoda": "#D94A43",
        "expedia": "#2563A9",
        "traveloka": "#08869B",
        "trip": "#565FBC",
        "booking.com": "#003B95",
        "header_text": "#F7F2E8",
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self._fit_desktop_window(self.root, 1080, 740)
        self.root.minsize(920, 640)
        self.root.configure(bg=self.COLORS["bg"])
        self.config_store = ConfigStore()
        self.config = self.config_store.load()
        if os.name == "nt":
            try:
                self.config = self.config_store.install_source_sound_pack(APP_DIR / "sounds")
            except Exception:
                LOGGER.exception("Cannot install bundled MP3 pack; preserve settings and keep fallback audio")
        self.state = StateStore()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.tray = SystemTray(self.events)
        self.hidden_to_tray = False
        self.startup_hide_job: str | None = None
        self.monitor: ImapMonitor | None = None
        self.monitor_generation = 0
        self.alert_queue: list[BookingEvent] = []
        self.active_alert: BookingEvent | None = None
        self.active_popup: tk.Toplevel | None = None
        self.active_guest_var: tk.StringVar | None = None
        self.active_room_var: tk.StringVar | None = None
        self.active_revenue_var: tk.StringVar | None = None
        self.active_checkout_var: tk.StringVar | None = None
        self.active_nights_var: tk.StringVar | None = None
        self.active_booking_details_var: tk.StringVar | None = None
        self.queued_ids: set[str] = set()
        self.sound_active = False
        self.sound_uses_file = False
        self.sound_repeat_job: str | None = None
        self.sound_preview_job: str | None = None
        self.mp3_player = WindowsMciAudioPlayer()
        self.closing = False
        self.update_busy = False
        self.print_loading = False
        self.print_spooling = False
        self.print_preview: tk.Toplevel | None = None
        self.print_preview_image: Any = None
        self.print_preview_photo: Any = None
        self.f92_clock_job: str | None = None
        self._build_styles()
        self._build_ui()
        self._load_config()
        if os.name == "nt":
            try:
                for source in self.source_sound_vars:
                    default_source_sound(source, APP_DIR / "sounds")
            except Exception:
                LOGGER.exception("Cannot prepare built-in source sounds; alerts retain fallback audio")
        self.f92_worker = F92Worker(self.events, self.config)
        self.f92_worker.start()
        self.booking_com_worker = BookingComWorker(self.events, self.state, self.config, APP_DIR / "booking-com-browser")
        self.booking_com_worker.start()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Unmap>", self._on_main_unmap, add="+")
        self.tray.start()
        self.root.after(150, self._drain_events)
        self.root.after(1000, self._minute_tick)
        self.f92_clock_job = self.root.after(1500, self._f92_clock_tick)
        self.refresh_history()
        for alert in self.state.pending_for_date(date.today()):
            self.enqueue_alert(alert)
        if self._has_complete_config():
            self.start_monitoring()
        if self.config.get("start_minimized"):
            self.startup_hide_job = self.root.after(200, self._minimize_if_no_alert)
        self.root.after(3500, lambda: self.check_for_updates(silent=True))

    @staticmethod
    def _fit_desktop_window(window: tk.Toplevel | tk.Tk, width: int, height: int) -> None:
        # Leave space for window chrome and the Windows taskbar on small screens.
        screen_width, screen_height = window.winfo_screenwidth(), window.winfo_screenheight()
        width = min(width, max(320, screen_width - 64))
        height = min(height, max(240, screen_height - 96))
        x = max(8, (screen_width - width) // 2)
        y = max(8, (screen_height - height - 80) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        self.root.option_add("*Font", "{Segoe UI} 10")
        self.root.option_add("*Menu.Font", "{Segoe UI} 10")
        style.configure("TFrame", background=self.COLORS["bg"])
        style.configure("Card.TFrame", background=self.COLORS["surface"])
        style.configure("Alt.TFrame", background=self.COLORS["surface_alt"])
        style.configure("TLabel", background=self.COLORS["bg"], foreground=self.COLORS["text"], font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["text"], font=("Segoe UI", 10))
        style.configure("Alt.TLabel", background=self.COLORS["surface_alt"], foreground=self.COLORS["text"], font=("Segoe UI", 10))
        style.configure("SectionTitle.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["primary"], font=("Segoe UI Semibold", 14))
        style.configure("Muted.TLabel", background=self.COLORS["bg"], foreground=self.COLORS["muted"], font=("Segoe UI", 9))
        style.configure("CardMuted.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["muted"], font=("Segoe UI", 9))
        style.configure("Field.TLabel", background=self.COLORS["surface"], foreground=self.COLORS["muted"], font=("Segoe UI Semibold", 9))
        style.configure("TButton", font=("Segoe UI Semibold", 9), padding=(14, 9), borderwidth=1)
        style.configure(
            "Accent.TButton", background=self.COLORS["gold"], foreground="#FFFFFF",
            bordercolor=self.COLORS["gold"], lightcolor=self.COLORS["gold"], darkcolor=self.COLORS["gold"],
        )
        style.map(
            "Accent.TButton",
            background=[("active", self.COLORS["gold_hover"]), ("pressed", self.COLORS["gold_hover"])],
            foreground=[("disabled", "#E5DFD2"), ("!disabled", "#FFFFFF")],
        )
        style.configure(
            "Primary.TButton", background=self.COLORS["primary"], foreground="#FFFFFF",
            bordercolor=self.COLORS["primary"], lightcolor=self.COLORS["primary"], darkcolor=self.COLORS["primary"],
        )
        style.map(
            "Primary.TButton",
            background=[("active", self.COLORS["primary_hover"]), ("pressed", self.COLORS["primary_hover"])],
        )
        style.configure(
            "Secondary.TButton", background=self.COLORS["surface"], foreground=self.COLORS["primary"],
            bordercolor=self.COLORS["border"], lightcolor=self.COLORS["surface"], darkcolor=self.COLORS["surface"],
        )
        style.map("Secondary.TButton", background=[("active", self.COLORS["surface_alt"])])
        # Popup-only styles: large targets without changing the main toolbar.
        for name in ("Popup.Primary.TButton", "Popup.Secondary.TButton"):
            style.configure(name, font=("Segoe UI Semibold", 18), padding=(20, 24), focuscolor=self.COLORS["gold"])
        style.configure(
            "Danger.TButton", background=self.COLORS["surface"], foreground="#9E3F3F",
            bordercolor=self.COLORS["border"], lightcolor=self.COLORS["surface"], darkcolor=self.COLORS["surface"],
        )
        style.map("Danger.TButton", background=[("active", "#FFF1F0")])
        style.configure("Premium.TNotebook", background=self.COLORS["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure(
            "Premium.TNotebook.Tab", background=self.COLORS["bg"], foreground=self.COLORS["muted"],
            font=("Segoe UI Semibold", 10), padding=(20, 11), borderwidth=0,
        )
        style.map(
            "Premium.TNotebook.Tab",
            background=[("selected", self.COLORS["surface"]), ("active", self.COLORS["surface_alt"])],
            foreground=[("selected", self.COLORS["primary"]), ("active", self.COLORS["text"])],
        )
        style.configure(
            "Treeview", background=self.COLORS["surface"], fieldbackground=self.COLORS["surface"],
            foreground=self.COLORS["text"], rowheight=38, font=("Segoe UI", 9), borderwidth=0,
        )
        style.map("Treeview", background=[("selected", "#E8E1D4")], foreground=[("selected", self.COLORS["primary"])])
        style.configure(
            "Treeview.Heading", background=self.COLORS["surface_alt"], foreground=self.COLORS["primary"],
            font=("Segoe UI Semibold", 9), padding=(8, 10), relief="flat", borderwidth=0,
        )
        style.map("Treeview.Heading", background=[("active", "#EFEAE1")])
        style.configure("TEntry", fieldbackground=self.COLORS["surface"], foreground=self.COLORS["text"], padding=(9, 8))
        style.configure("TCombobox", fieldbackground=self.COLORS["surface"], foreground=self.COLORS["text"], padding=(9, 7))
        style.configure("Card.TCheckbutton", background=self.COLORS["surface"], foreground=self.COLORS["text"], font=("Segoe UI", 9))
        style.map("Card.TCheckbutton", background=[("active", self.COLORS["surface"])])
        style.configure(
            "Section.TLabelframe", background=self.COLORS["surface"], bordercolor=self.COLORS["border"],
            lightcolor=self.COLORS["border"], darkcolor=self.COLORS["border"], borderwidth=1, relief="solid",
        )
        style.configure(
            "Section.TLabelframe.Label", background=self.COLORS["surface"], foreground=self.COLORS["primary"],
            font=("Segoe UI Semibold", 11), padding=(4, 0),
        )

    def _build_ui(self) -> None:
        shell = tk.Frame(self.root, bg=self.COLORS["bg"])
        shell.pack(fill="both", expand=True)

        header = tk.Frame(shell, bg=self.COLORS["primary"], height=112)
        header.pack(fill="x")
        header.pack_propagate(False)
        brand = tk.Frame(header, bg=self.COLORS["primary"])
        brand.pack(side="left", padx=28, pady=20)
        tk.Label(
            brand, text="M", width=3, height=1, bg=self.COLORS["gold"], fg="#FFFFFF",
            font=("Georgia", 18, "bold"), padx=4, pady=7,
        ).pack(side="left", padx=(0, 14))
        brand_text = tk.Frame(brand, bg=self.COLORS["primary"])
        brand_text.pack(side="left")
        tk.Label(
            brand_text, text="BOOKING DESK", bg=self.COLORS["primary"], fg=self.COLORS["header_text"],
            font=("Segoe UI Semibold", 21),
        ).pack(anchor="w")
        tk.Label(
            brand_text, text="Agoda · Expedia · Traveloka · Trip · Booking.com",
            bg=self.COLORS["primary"], fg="#BFC9D8", font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(3, 0))

        self.status_var = tk.StringVar(value="Chưa khởi động")
        status_box = tk.Frame(header, bg=self.COLORS["primary"])
        status_box.pack(side="right", padx=28, pady=22)
        tk.Label(
            status_box, text=f"PHIÊN BẢN {APP_VERSION}", bg=self.COLORS["primary"], fg="#97A7BB",
            font=("Segoe UI Semibold", 8),
        ).pack(anchor="e")
        self.status_label = tk.Label(
            status_box, textvariable=self.status_var, bg=self.COLORS["primary"], fg=self.COLORS["success"],
            font=("Segoe UI Semibold", 10), anchor="e", justify="right", wraplength=340,
        )
        self.status_label.pack(anchor="e", pady=(8, 0))

        self.history_tab = ttk.Frame(shell, style="Card.TFrame", padding=18)
        self.history_tab.pack(fill="both", expand=True, padx=26, pady=(18, 22))
        self._build_history()

        # Build once, keep hidden: config variables and the live log remain
        # available to background monitoring without cluttering the main page.
        self.settings_window = tk.Toplevel(self.root)
        self.settings_window.withdraw()
        self.settings_window.title("Cài đặt • Booking Desk")
        self._fit_desktop_window(self.settings_window, 980, 700)
        self.settings_window.minsize(840, 600)
        self.settings_window.configure(bg=self.COLORS["bg"])
        self.settings_window.transient(self.root)
        self.settings_window.protocol("WM_DELETE_WINDOW", self.close_settings)
        self.settings_window.bind("<Escape>", lambda _event: self.close_settings())
        settings_shell = ttk.Frame(self.settings_window, padding=18)
        settings_shell.pack(fill="both", expand=True)
        ttk.Label(settings_shell, text="Cài đặt", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(
            settings_shell, text="Kết nối email, công cụ vận hành và nhật ký hệ thống.", style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 14))
        toolbar_card = tk.Frame(
            settings_shell, bg=self.COLORS["surface"], highlightbackground=self.COLORS["border"], highlightthickness=1,
        )
        toolbar_card.pack(fill="x", pady=(0, 14))
        toolbar = ttk.Frame(toolbar_card, style="Card.TFrame", padding=(14, 12))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Lưu & khởi động", command=self.save_and_start, style="Accent.TButton").pack(side="left")
        ttk.Button(toolbar, text="Quét email ngay", command=self.check_now, style="Primary.TButton").pack(side="left", padx=8)
        ttk.Button(toolbar, text="Kiểm tra IMAP", command=self.test_connection, style="Secondary.TButton").pack(side="left")
        ttk.Button(toolbar, text="Thoát", command=self.exit_app, style="Danger.TButton").pack(side="right")
        ttk.Button(
            toolbar, text="Kiểm tra cập nhật", command=lambda: self.check_for_updates(False), style="Secondary.TButton",
        ).pack(side="right", padx=(0, 8))

        ttk.Button(
            settings_shell, text="Đóng Cài đặt", command=self.close_settings, style="Secondary.TButton",
        ).pack(side="bottom", anchor="e", pady=(12, 0))
        notebook = ttk.Notebook(settings_shell, style="Premium.TNotebook")
        self.settings_notebook = notebook
        notebook.pack(fill="both", expand=True)
        self.settings_tab = ttk.Frame(notebook, style="Card.TFrame", padding=0)
        self.log_tab = ttk.Frame(notebook, style="Card.TFrame", padding=18)
        notebook.add(self.settings_tab, text="CẤU HÌNH")
        notebook.add(self.log_tab, text="NHẬT KÝ")
        self._build_settings()
        self._build_log()

    def open_settings(self) -> None:
        self.restore_main_window()
        self.settings_window.deiconify()
        self.settings_window.lift()
        self.settings_window.focus_force()

    def close_settings(self) -> None:
        # Closing settings must not stop monitoring or discard unsaved fields.
        self.stop_sound_preview()
        self.settings_window.withdraw()
        if self.active_popup is not None:
            self.active_popup.lift()
            self.active_popup.focus_force()
        else:
            self.settings_button.focus_set()

    def _build_history(self) -> None:
        heading = ttk.Frame(self.history_tab, style="Card.TFrame")
        heading.pack(fill="x", pady=(0, 14))
        title_box = ttk.Frame(heading, style="Card.TFrame")
        title_box.pack(side="left")
        ttk.Label(title_box, text="Booking hôm nay", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(
            title_box, text="Nhấp đúp hoặc nhấn chuột phải vào một dòng để sao chép sang Excel.",
            style="CardMuted.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        self.settings_button = ttk.Button(
            heading, text="\u2699\ufe0e Cài đặt", command=self.open_settings, style="Secondary.TButton", takefocus=True,
        )
        self.settings_button.pack(side="right", anchor="n")
        self.history_date_var = tk.StringVar()
        ttk.Label(heading, textvariable=self.history_date_var, style="CardMuted.TLabel").pack(
            side="right", anchor="n", padx=(0, 18), pady=10,
        )

        table = ttk.Frame(self.history_tab, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        columns = ("source", "booking", "guest", "room", "checkin", "revenue")
        self.history_tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="extended")
        headings = {
            "source": "Nguồn", "booking": "Mã booking", "guest": "Khách",
            "room": "Phòng", "checkin": "Check-in", "revenue": "Tổng thu",
        }
        widths = {"source": 105, "booking": 125, "guest": 185, "room": 255, "checkin": 105, "revenue": 145}
        for column in columns:
            self.history_tree.heading(column, text=headings[column])
            self.history_tree.column(column, width=widths[column], minwidth=60)
        scrollbar = ttk.Scrollbar(table, command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=scrollbar.set)
        # Reserve the scrollbar first; otherwise the table's requested width
        # can consume its space on a smaller desktop window.
        scrollbar.pack(side="right", fill="y")
        self.history_tree.pack(side="left", fill="both", expand=True)

        def fit_columns(event: tk.Event) -> None:
            available = max(60 * len(columns), event.width - 4)
            remaining = available - 60 * len(columns)
            weight_total = sum(width - 60 for width in widths.values())
            allocated = 0
            for index, column in enumerate(columns):
                width = (available - allocated if index == len(columns) - 1
                         else 60 + remaining * (widths[column] - 60) // weight_total)
                self.history_tree.column(column, width=width)
                allocated += width

        self.history_tree.bind("<Configure>", fit_columns)
        self.history_tree.tag_configure("even", background=self.COLORS["surface"])
        self.history_tree.tag_configure("odd", background=self.COLORS["surface_alt"])
        self.history_tree.bind("<Double-1>", self.copy_selected_history)
        self.history_tree.bind("<Return>", self.copy_selected_history)
        self.history_tree.bind("<Control-c>", self.copy_selected_history)
        self.history_tree.bind("<Button-3>", self.show_history_context_menu)
        self.history_tree.bind("<Button-2>", self.show_history_context_menu)
        self.history_menu = tk.Menu(
            self.root, tearoff=False, bg=self.COLORS["surface"], fg=self.COLORS["text"],
            activebackground=self.COLORS["gold"], activeforeground="#FFFFFF", relief="solid", borderwidth=1,
        )
        self.history_menu.add_command(label="Sao chép dòng đã chọn sang Excel", command=self.copy_selected_history)

    def _build_settings(self) -> None:
        canvas = tk.Canvas(self.settings_tab, bg=self.COLORS["surface"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.settings_tab, command=canvas.yview)
        content = ttk.Frame(canvas, style="Card.TFrame", padding=18)
        self.settings_canvas = canvas
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window, width=e.width))
        self.root.bind_all("<MouseWheel>", self._scroll_settings, add="+")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.provider_var = tk.StringVar()
        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar()
        self.email_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.poll_var = tk.StringVar()
        self.scan_days_var = tk.StringVar()
        self.sound_var = tk.StringVar()
        self.source_sound_vars = {source: tk.StringVar() for source in BOOKING_SOURCES}
        self.quiet_var = tk.BooleanVar()
        self.start_windows_var = tk.BooleanVar()
        self.start_minimized_var = tk.BooleanVar()
        self.f92_enabled_var = tk.BooleanVar()
        self.f92_port_var = tk.StringVar()
        self.f92_sound_var = tk.StringVar()
        self.f92_builtin_sound_var = tk.BooleanVar()
        self.update_source_var = tk.StringVar()
        self.booking_com_enabled_var = tk.BooleanVar()
        self.booking_com_browser_var = tk.StringVar()
        self.booking_com_status_var = tk.StringVar(value="Email báo ngay. Đăng nhập Extranet riêng trên máy này để tự lấy đầy đủ chi tiết.")

        ttk.Label(content, text="Cấu hình vận hành", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(
            content, text="Các thay đổi chỉ có hiệu lực sau khi bấm “Lưu & khởi động”.",
            style="CardMuted.TLabel",
        ).pack(anchor="w", pady=(4, 14))

        connection = ttk.LabelFrame(
            content, text="  Kết nối email  ", style="Section.TLabelframe", padding=(18, 14),
        )
        connection.pack(fill="x", pady=(0, 12))
        connection.columnconfigure(0, weight=1)
        connection.columnconfigure(1, weight=1)
        self._add_setting_field(connection, "Nhà cung cấp", self.provider_var, "combo", 0, 0)
        self._add_setting_field(connection, "Địa chỉ email", self.email_var, "entry", 0, 1)
        self._add_setting_field(connection, "Máy chủ IMAP", self.host_var, "entry", 1, 0)
        self._add_setting_field(connection, "Cổng IMAP", self.port_var, "entry", 1, 1)
        self._add_setting_field(connection, "Mật khẩu ứng dụng", self.password_var, "password", 2, 0, 2)
        self._add_setting_field(connection, "Quét mỗi (giây)", self.poll_var, "entry", 3, 0)
        self._add_setting_field(connection, "Tìm email trong (ngày)", self.scan_days_var, "entry", 3, 1)

        preferences = ttk.LabelFrame(
            content, text="  Cảnh báo & khởi động  ", style="Section.TLabelframe", padding=(18, 14),
        )
        preferences.pack(fill="x", pady=(0, 12))
        preferences.columnconfigure(0, weight=1)
        preferences.columnconfigure(1, weight=1)
        checks = (
            ("Giờ yên lặng 00:00–08:00", self.quiet_var),
            ("Khởi động cùng Windows", self.start_windows_var),
            ("Ẩn xuống khay khi khởi động", self.start_minimized_var),
        )
        for index, (text, variable) in enumerate(checks):
            ttk.Checkbutton(
                preferences, text=text, variable=variable, style="Card.TCheckbutton",
            ).grid(row=index // 2, column=index % 2, sticky="w", padx=6, pady=5)

        booking = ttk.LabelFrame(content, text="  Booking.com • Chi tiết đầy đủ  ", style="Section.TLabelframe", padding=(18, 14))
        booking.pack(fill="x", pady=(0, 12))
        ttk.Checkbutton(booking, text="Tự bổ sung họ tên, phòng, ngày trả và tổng tiền từ Extranet",
                        variable=self.booking_com_enabled_var, style="Card.TCheckbutton").pack(anchor="w")
        ttk.Label(booking, text="Đăng nhập/2FA một lần trên mỗi máy. App dùng hồ sơ riêng, không lấy mật khẩu từ Chrome thường.\nNếu phiên hết hạn, email vẫn báo booking hôm nay; chi tiết được bổ sung khi đăng nhập lại.",
                  style="CardMuted.TLabel", wraplength=680).pack(anchor="w", pady=(8, 10))
        row = ttk.Frame(booking, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Combobox(row, textvariable=self.booking_com_browser_var, values=("auto", "msedge", "chrome"), state="readonly", width=10).pack(side="left")
        ttk.Button(row, name="booking_com_login", text="Đăng nhập Booking.com", command=self.login_booking_com,
                   style="Secondary.TButton").pack(side="left", padx=8)
        ttk.Button(row, text="Lấy lại chi tiết", command=self.refresh_booking_com, style="Secondary.TButton").pack(side="left")
        ttk.Label(booking, textvariable=self.booking_com_status_var, style="CardMuted.TLabel", wraplength=680).pack(anchor="w", pady=(10, 0))

        sounds = ttk.LabelFrame(content, text="  Âm thanh theo nguồn booking  ", style="Section.TLabelframe", padding=(18, 14))
        sounds.pack(fill="x", pady=(0, 12))
        self.sound_settings_frame = sounds
        self.sound_preview_buttons = {}
        self.sound_choose_buttons = {}
        self.source_sound_entries = {}
        for source, variable in self.source_sound_vars.items():
            block = ttk.Frame(sounds, style="Card.TFrame")
            block.pack(fill="x", pady=(0, 10))
            ttk.Label(block, text=f"{source} • MP3 / WAV", style="Field.TLabel").pack(anchor="w", pady=(0, 5))
            row = ttk.Frame(block, style="Card.TFrame")
            row.pack(fill="x")
            entry = ttk.Entry(row, textvariable=variable)
            entry.pack(side="left", fill="x", expand=True)
            self.source_sound_entries[source] = entry
            choose = ttk.Button(row, text="Chọn tệp", command=lambda s=source: self.choose_sound(s), style="Secondary.TButton")
            choose.pack(side="left", padx=(8, 0))
            self.sound_choose_buttons[source] = choose
            preview = ttk.Button(row, text=f"Nghe thử {source}", command=lambda s=source: self.preview_source_sound(s), style="Secondary.TButton")
            preview.pack(side="left", padx=(8, 0))
            self.sound_preview_buttons[source] = preview
        ttk.Label(sounds, text="Đã tích hợp: Agoda → 1-agoda.mp3; Expedia → 2-expedia.mp3;\nTraveloka → 3-traveloka.mp3; Trip → 4-trip.mp3 (âm thanh bạn cung cấp).\nBooking.com dùng chuông riêng; bạn có thể chọn MP3/WAV. Nghe thử tự dừng sau 4 giây.", style="CardMuted.TLabel", wraplength=680).pack(anchor="w", pady=(0, 12))
        ttk.Label(sounds, text="Âm thanh chung dự phòng (giữ cấu hình cũ)", style="Field.TLabel").pack(anchor="w", pady=(0, 5))
        common = ttk.Frame(sounds, style="Card.TFrame")
        common.pack(fill="x")
        ttk.Entry(common, textvariable=self.sound_var).pack(side="left", fill="x", expand=True)
        ttk.Button(common, text="Chọn tệp", command=self.choose_sound, style="Secondary.TButton").pack(side="left", padx=(8, 0))
        ttk.Button(common, text="Dừng nghe thử", command=self.stop_sound_preview, style="Secondary.TButton").pack(side="left", padx=(8, 0))

        f92 = ttk.LabelFrame(
            content, text="  Màn hình F92  ", style="Section.TLabelframe", padding=(18, 14),
        )
        f92.pack(fill="x", pady=(0, 12))
        f92.columnconfigure(0, weight=1)
        f92.columnconfigure(1, weight=1)
        self._add_setting_field(f92, "Cổng thiết bị", self.f92_port_var, "entry", 0, 0)
        self._add_setting_field(f92, "Âm báo (1–4)", self.f92_sound_var, "entry", 0, 1)
        ttk.Checkbutton(
            f92, text="Bật màn hình F92", variable=self.f92_enabled_var, style="Card.TCheckbutton",
        ).grid(row=1, column=0, sticky="w", padx=6, pady=6)
        ttk.Checkbutton(
            f92, text="Bật loa tích hợp F92", variable=self.f92_builtin_sound_var, style="Card.TCheckbutton",
        ).grid(row=1, column=1, sticky="w", padx=6, pady=6)
        ttk.Button(f92, text="Kiểm tra thiết bị F92", command=self.test_f92, style="Secondary.TButton").grid(
            row=2, column=0, sticky="w", padx=6, pady=(8, 2),
        )

        updates = ttk.LabelFrame(
            content, text="  Cập nhật an toàn  ", style="Section.TLabelframe", padding=(18, 14),
        )
        updates.pack(fill="x", pady=(0, 8))
        updates.columnconfigure(0, weight=1)
        self._add_setting_field(updates, "Nguồn update.json đã ký", self.update_source_var, "entry", 0, 0)

    def _add_setting_field(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.Variable,
        kind: str,
        row: int,
        column: int,
        columnspan: int = 1,
    ) -> ttk.Widget:
        block = ttk.Frame(parent, style="Card.TFrame")
        block.grid(row=row, column=column, columnspan=columnspan, sticky="ew", padx=6, pady=6)
        ttk.Label(block, text=label, style="Field.TLabel").pack(anchor="w", pady=(0, 6))
        if kind == "combo":
            widget: ttk.Widget = ttk.Combobox(
                block, textvariable=variable, values=list(PROVIDERS), state="readonly",
            )
            widget.bind("<<ComboboxSelected>>", self.on_provider_changed)
        else:
            widget = ttk.Entry(block, textvariable=variable, show="•" if kind == "password" else "")
        widget.pack(fill="x")
        return widget

    def _scroll_settings(self, event: tk.Event) -> str:
        widget = event.widget
        while widget:
            if widget == self.settings_canvas:
                self.settings_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return "break"
            widget = getattr(widget, "master", None)
        return ""

    def _build_log(self) -> None:
        heading = ttk.Frame(self.log_tab, style="Card.TFrame")
        heading.pack(fill="x", pady=(0, 12))
        ttk.Label(heading, text="Nhật ký hệ thống", style="SectionTitle.TLabel").pack(anchor="w")
        ttk.Label(
            heading, text="Thông tin kết nối, quét email và trạng thái thiết bị F92.", style="CardMuted.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        self.log_text = tk.Text(
            self.log_tab, wrap="word", state="disabled", bg=self.COLORS["primary"], fg="#D9E1EC",
            insertbackground="white", selectbackground=self.COLORS["gold"], font=("Consolas", 9),
            relief="flat", padx=16, pady=14,
        )
        self.log_text.pack(fill="both", expand=True)

    def _load_config(self) -> None:
        c = self.config
        self.provider_var.set(str(c["provider"]))
        self.host_var.set(str(c["imap_host"]))
        self.port_var.set(str(c["imap_port"]))
        self.email_var.set(str(c["email_address"]))
        try:
            self.password_var.set(unprotect_secret(str(c.get("password_encrypted", ""))))
        except Exception:
            self.password_var.set("")
        self.poll_var.set(str(c["poll_seconds"]))
        self.scan_days_var.set(str(c["scan_days"]))
        self.sound_var.set(str(c["sound_file"]))
        for source, variable in self.source_sound_vars.items():
            variable.set(str(c.get(SOURCE_SOUND_KEYS[source.lower()], "")))
        self.booking_com_enabled_var.set(bool(c.get("booking_com_enrichment", True)))
        self.booking_com_browser_var.set(str(c.get("booking_com_browser", "auto")))
        self.quiet_var.set(bool(c["quiet_hours_enabled"]))
        self.start_windows_var.set(bool(c["start_with_windows"]))
        self.start_minimized_var.set(bool(c["start_minimized"]))
        self.f92_enabled_var.set(bool(c["f92_enabled"]))
        self.f92_port_var.set(str(c["f92_port"]))
        self.f92_sound_var.set(str(c["f92_sound_index"]))
        self.f92_builtin_sound_var.set(bool(c["f92_builtin_sound_enabled"]))
        self.update_source_var.set(str(c["update_manifest_source"]))

    def _collect_config(self) -> dict[str, Any]:
        password = self.password_var.get()
        return {
            "provider": self.provider_var.get(),
            "imap_host": self.host_var.get().strip(),
            "imap_port": int(self.port_var.get()),
            "email_address": self.email_var.get().strip(),
            "password_encrypted": protect_secret(password),
            "poll_seconds": min(3600, max(30, int(self.poll_var.get()))),
            "scan_days": min(365, max(1, int(self.scan_days_var.get()))),
            "sound_file": self.sound_var.get().strip(),
            "source_sound_pack": str(self.config.get("source_sound_pack", "")),
            "trip_sound_pack": str(self.config.get("trip_sound_pack", "")),
            **{SOURCE_SOUND_KEYS[source.lower()]: variable.get().strip() for source, variable in self.source_sound_vars.items()},
            "booking_com_enrichment": self.booking_com_enabled_var.get(),
            "booking_com_browser": self.booking_com_browser_var.get(),
            "quiet_hours_enabled": self.quiet_var.get(),
            "start_with_windows": self.start_windows_var.get(),
            "start_minimized": self.start_minimized_var.get(),
            "f92_enabled": self.f92_enabled_var.get(),
            "f92_port": self.f92_port_var.get().strip().upper() or "AUTO",
            "f92_sound_index": int(self.f92_sound_var.get()),
            "f92_builtin_sound_enabled": self.f92_builtin_sound_var.get(),
            "update_manifest_source": self.update_source_var.get().strip(),
        }

    def _has_complete_config(self) -> bool:
        return bool(self.host_var.get().strip() and self.email_var.get().strip() and self.password_var.get())

    def on_provider_changed(self, _event: object = None) -> None:
        host, port = PROVIDERS.get(self.provider_var.get(), ("", 993))
        if host:
            self.host_var.set(host)
        self.port_var.set(str(port))

    def choose_sound(self, source: str | None = None) -> None:
        value = filedialog.askopenfilename(parent=self.settings_window, title=f"Chọn âm thanh {source or 'chung'}", filetypes=[("Âm thanh MP3 / WAV", "*.mp3 *.wav"), ("Tất cả", "*.*")])
        if value:
            variable = self.source_sound_vars[source] if source else self.sound_var
            variable.set(value)

    def preview_source_sound(self, source: str) -> None:
        if self.active_alert is not None:
            messagebox.showinfo("Đang có booking", "Hãy đóng thông báo booking trước khi nghe thử để không ngắt âm báo đang phát.", parent=self.settings_window)
            return
        draft = {
            "sound_file": self.sound_var.get().strip(),
            **{SOURCE_SOUND_KEYS[s.lower()]: v.get().strip() for s, v in self.source_sound_vars.items()},
        }
        try:
            self._start_source_sound(source, draft)
            self.sound_preview_job = self.root.after(4000, self.stop_sound_preview)
            self.log(f"Nghe thử âm thanh {source} (4 giây); chưa lưu cấu hình.")
        except Exception as exc:
            self.stop_sound()
            messagebox.showerror("Không nghe thử được", str(exc), parent=self.settings_window)

    def stop_sound_preview(self) -> None:
        # This button must never silence an actual pending booking.
        if self.active_alert is None:
            self.stop_sound()

    def save_and_start(self) -> None:
        try:
            self.config = self._collect_config()
            if not self._has_complete_config():
                raise ValueError("Hãy nhập đầy đủ máy chủ, email và mật khẩu ứng dụng.")
            self.config_store.save(self.config)
            set_start_with_windows(bool(self.config["start_with_windows"]))
            self.f92_worker.configure(self.config)
            self.booking_com_worker.configure(self.config)
            self._restore_f92_display()
            self.start_monitoring()
            self.log("Đã lưu cấu hình an toàn và khởi động theo dõi.")
        except Exception as exc:
            messagebox.showerror("Không lưu được", str(exc), parent=self.root)

    def start_monitoring(self) -> None:
        password = self.password_var.get()
        config = dict(self.config)
        old = self.monitor
        self.monitor_generation += 1
        generation = self.monitor_generation
        if old and old.is_alive():
            old.stop()
            self.set_status("Đang dừng phiên theo dõi cũ…")

            def wait_then_start() -> None:
                old.join(35)
                if old.is_alive():
                    self.events.put(("error", "Phiên IMAP cũ chưa dừng; không tạo worker thứ hai."))
                    return
                self.events.put(("restart_ready", (generation, config, password)))

            threading.Thread(target=wait_then_start, name="MonitorRestart", daemon=True).start()
        else:
            self._start_monitor_generation(generation, config, password)

    def _start_monitor_generation(self, generation: int, config: dict[str, Any], password: str) -> None:
        if generation != self.monitor_generation:
            return
        self.monitor = ImapMonitor(config, password, self.state, self.events)
        self.monitor.start()

    def check_now(self) -> None:
        if self.monitor and self.monitor.is_alive():
            self.monitor.check_now()
            self.set_status("Đã yêu cầu quét ngay")
        else:
            self.save_and_start()

    def _save_booking_com_preferences(self) -> None:
        # Login must work even when IMAP settings are not yet filled in.
        self.config["booking_com_enrichment"] = self.booking_com_enabled_var.get()
        self.config["booking_com_browser"] = self.booking_com_browser_var.get()
        self.config_store.save(self.config)
        self.booking_com_worker.configure(self.config)

    def login_booking_com(self, alert: BookingEvent | None = None) -> None:
        self._save_booking_com_preferences()
        self.booking_com_status_var.set("Đang mở cửa sổ đăng nhập Booking.com…")
        self.booking_com_worker.login(alert)

    def refresh_booking_com(self) -> None:
        self._save_booking_com_preferences()
        self.booking_com_worker.wake(force=True)

    def test_connection(self) -> None:
        try:
            config = self._collect_config()
            password = self.password_var.get()
        except Exception as exc:
            messagebox.showerror("Cấu hình chưa đúng", str(exc), parent=self.root)
            return
        self.set_status("Đang kiểm tra IMAP…")

        def worker() -> None:
            try:
                test_imap_connection(config, password)
                self.events.put(("connection_test", (True, "Kết nối IMAP thành công; TLS đã được xác minh.")))
            except Exception as exc:
                self.events.put(("connection_test", (False, str(exc))))

        threading.Thread(target=worker, name="ImapTest", daemon=True).start()

    def test_f92(self) -> None:
        try:
            self.f92_worker.configure(self._collect_config())
            self.f92_worker.test()
            self.set_status("Đang kiểm tra F92…")
        except Exception as exc:
            messagebox.showerror("F92", str(exc), parent=self.root)

    def check_for_updates(self, silent: bool = False) -> None:
        source = self.update_source_var.get().strip()
        if not source:
            if not silent:
                messagebox.showwarning("Cập nhật", "Chưa cấu hình nguồn update.json.", parent=self.root)
            return

        def worker() -> None:
            try:
                info = check_for_update(APP_VERSION, source)
                self.events.put(("update_result", (info, silent, None)))
            except Exception as exc:
                self.events.put(("update_result", (None, silent, exc)))

        threading.Thread(target=worker, name="UpdateCheck", daemon=True).start()

    def _offer_update(self, info: Any) -> None:
        if self.update_busy:
            return
        if not messagebox.askyesno(
            "Có bản cập nhật",
            f"Phiên bản {info.version} đã sẵn sàng.\n\n{info.notes}\n\nTải và cài đặt ngay?",
            parent=self.root,
        ):
            return
        self.set_status("Đang tải bản cập nhật đã ký…")
        self.update_busy = True

        def worker() -> None:
            try:
                downloaded = download_update(info)
                self.events.put(("update_downloaded", (info, downloaded)))
            except Exception as exc:
                self.events.put(("update_failed", f"Không tải được cập nhật: {exc}"))

        threading.Thread(target=worker, name="UpdateDownload", daemon=True).start()

    def _install_update(self, info: Any, downloaded: Path) -> None:
        self.set_status("Đang chuẩn bị cài đặt; ứng dụng sẽ tự mở lại…")

        def worker() -> None:
            try:
                launch_self_update(downloaded, Path(sys.executable), info.sha256)
                self.events.put(("update_ready", None))
            except Exception as exc:
                self.events.put(("update_failed", str(exc)))

        threading.Thread(target=worker, name="UpdatePrepare", daemon=True).start()

    def _drain_events(self) -> None:
        try:
            while True:
                event_type, payload = self.events.get_nowait()
                if event_type == "status":
                    self.set_status(str(payload))
                elif event_type == "log":
                    self.log(str(payload))
                elif event_type == "error":
                    self.set_status("Có lỗi – xem Nhật ký")
                    self.log(f"LỖI: {payload}")
                elif event_type == "alert":
                    self.enqueue_alert(payload)
                elif event_type == "deferred_alert":
                    self.log(f"Đã ghi nhận booking {payload.booking_id}; sẽ báo sau 08:00.")
                elif event_type in {"booking_cancelled", "booking_modified"}:
                    self._handle_booking_lifecycle(payload)
                elif event_type == "history_changed":
                    self.refresh_history()
                    self._refresh_pending_details()
                    self.booking_com_worker.wake()
                elif event_type == "booking_com_status":
                    self.booking_com_status_var.set(str(payload))
                    self.log(str(payload))
                    if self.active_alert and self.active_alert.source == "Booking.com" and self.active_booking_details_var:
                        self.active_booking_details_var.set("Đã lấy đầy đủ chi tiết từ Extranet." if self.active_alert.details_loaded_at else str(payload))
                elif event_type in {"expedia_print_ready", "booking_print_ready"}:
                    self.print_loading = False
                    alert, image, error = payload
                    if error:
                        messagebox.showerror(f"In phiếu {alert.source}", error, parent=self.active_popup or self.root)
                    else:
                        self.show_expedia_print_preview(alert, image)
                elif event_type in {"expedia_print_done", "booking_print_done"}:
                    self.print_spooling = False
                    preview, button, feedback, error = payload
                    if preview.winfo_exists():
                        button.configure(state="normal")
                        button.master.nametowidget("close_preview").configure(state="normal")
                        feedback.set(error or "Đã gửi đúng 1 trang A4 đến máy in. Hãy kiểm tra bản in.")
                elif event_type == "connection_test":
                    ok, text = payload
                    self.set_status(text)
                    (messagebox.showinfo if ok else messagebox.showerror)("Kiểm tra IMAP", text, parent=self.root)
                elif event_type == "f92_status":
                    self.log(str(payload))
                elif event_type == "f92_error":
                    self.log(f"F92: {payload}")
                elif event_type == "f92_test_result":
                    ok, text = payload
                    if ok:
                        self._restore_f92_display()
                    (messagebox.showinfo if ok else messagebox.showerror)("Kiểm tra F92", text, parent=self.root)
                elif event_type == "update_result":
                    info, silent, error = payload
                    if error and not silent:
                        messagebox.showerror("Kiểm tra cập nhật", str(error), parent=self.root)
                    elif info:
                        self._offer_update(info)
                    elif not silent:
                        messagebox.showinfo("Cập nhật", "Bạn đang dùng phiên bản mới nhất.", parent=self.root)
                elif event_type == "update_downloaded":
                    self._install_update(*payload)
                elif event_type == "update_ready":
                    self.exit_app()
                    return
                elif event_type == "update_failed":
                    self.update_busy = False
                    self.log(f"Lỗi cập nhật: {payload}")
                    self.set_status("Cập nhật chưa thành công; ứng dụng vẫn đang chạy")
                    messagebox.showerror("Không cài được cập nhật", str(payload), parent=self.root)
                elif event_type == "restart_ready":
                    self._start_monitor_generation(*payload)
                elif event_type == "tray_ready":
                    if self.hidden_to_tray:
                        self.hide_to_tray()
                elif event_type == "tray_open":
                    self.restore_main_window()
                elif event_type == "tray_settings":
                    self.open_settings()
                elif event_type == "tray_exit":
                    self.exit_app()
                    return
                elif event_type == "tray_failed":
                    self.restore_main_window()
                    self.log(f"Không tạo được biểu tượng khay; vẫn có thể mở ứng dụng từ Taskbar: {payload}")
        except queue.Empty:
            pass
        except Exception:
            LOGGER.exception("UI event failed; keeping notification loop alive")
            self.set_status("Lỗi hiển thị – xem Nhật ký; ứng dụng sẽ tiếp tục thử")
        finally:
            if not self.closing:
                self.root.after(180, self._drain_events)

    def _minute_tick(self) -> None:
        try:
            if self.history_day != date.today():
                self.refresh_history()
            if not (self.quiet_var.get() and is_quiet_hours()):
                for alert in self.state.pending_for_date(date.today()):
                    self.enqueue_alert(alert)
        except Exception:
            LOGGER.exception("Cannot show pending alerts; will retry")
        finally:
            self.root.after(30_000, self._minute_tick)

    def _restore_f92_display(self) -> None:
        if self.active_alert:
            self.f92_worker.notify(self.active_alert, play_sound=False)
        elif not self.alert_queue:
            self.f92_worker.idle()

    def _f92_clock_tick(self) -> None:
        self.f92_clock_job = None
        if not self.active_alert and not self.alert_queue:
            self.f92_worker.idle()
        now = datetime.now()
        delay = max(1000, 60_000 - now.second * 1000 - now.microsecond // 1000)
        self.f92_clock_job = self.root.after(delay, self._f92_clock_tick)

    def _handle_booking_lifecycle(self, event: BookingEvent) -> None:
        self.alert_queue = [item for item in self.alert_queue if item.storage_id != event.storage_id]
        self.queued_ids.discard(event.storage_id)
        if self.active_alert and self.active_alert.storage_id == event.storage_id:
            if self.active_popup:
                self.active_popup.destroy()
            self.active_popup = None
            self.active_alert = None
            self.stop_sound()
            self.log(f"Đã đóng cảnh báo booking {event.booking_id} vì có email hủy/chỉnh sửa.")
            self._show_next_alert()
        if event.status != "cancelled":
            for due in self.state.pending_for_date(date.today()):
                self.enqueue_alert(due)

    def enqueue_alert(self, alert: BookingEvent) -> None:
        if alert.checkin_date != date.today() or alert.status not in {"new", "active"}:
            return
        # The minute tick/startup can show a persisted popup before the IMAP
        # worker emits its alert. Closing that popup must also block the late event.
        if self.state.is_acknowledged(alert):
            return
        if self.quiet_var.get() and is_quiet_hours():
            return  # Already persisted pending; the minute tick releases it after 08:00.
        if alert.storage_id in self.queued_ids:
            return
        self.queued_ids.add(alert.storage_id)
        self.alert_queue.append(alert)
        if not self.active_alert:
            try:
                self._show_next_alert()
            except Exception:
                # Keep the persisted pending event retryable after a Tk/audio error.
                failed_alert = self.active_alert or alert
                if self.active_popup:
                    self.active_popup.destroy()
                self.active_popup = None
                self.active_alert = None
                self.queued_ids.discard(failed_alert.storage_id)
                self.alert_queue = [item for item in self.alert_queue if item.storage_id != failed_alert.storage_id]
                raise

    def _minimize_if_no_alert(self) -> None:
        # Startup hiding is safe even when an unacknowledged booking is open.
        if self.startup_hide_job is not None:
            self.root.after_cancel(self.startup_hide_job)
            self.startup_hide_job = None
        self.hide_to_tray()

    def _present_alert_popup(self, popup: tk.Toplevel) -> None:
        """Show only the independent alert, including while the main window is hidden."""
        try:
            popup.deiconify()
            popup.update_idletasks()
            popup.attributes("-topmost", True)
            popup.lift()
            popup.focus_force()
        except tk.TclError:
            LOGGER.exception("Không thể đưa popup booking lên trước")

        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes

                hwnd = popup.winfo_id()
                user32 = ctypes.windll.user32
                user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
                user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
            except (AttributeError, OSError, ValueError, tk.TclError):
                LOGGER.exception("Windows không thể ưu tiên popup booking")

        def reinforce_focus() -> None:
            try:
                if popup.winfo_exists():
                    popup.lift()
                    popup.focus_force()
            except tk.TclError:
                pass

        popup.after(250, reinforce_focus)

    def _show_next_alert(self) -> None:
        if self.active_alert:
            return
        while self.alert_queue:
            alert = self.alert_queue.pop(0)
            # A notification may have been acknowledged after it entered this
            # queue. Recheck immediately before creating its window/sound/F92.
            if alert.checkin_date != date.today() or self.state.is_acknowledged(alert):
                self.queued_ids.discard(alert.storage_id)
                continue
            break
        else:
            self.f92_worker.idle()
            return
        self.active_alert = alert
        popup = tk.Toplevel(self.root)
        self.active_popup = popup
        popup.title(f"{alert.source} • Check-in hôm nay")
        width, height = 620, 700 if alert.source in {"Expedia", "Traveloka"} else 640
        x = max(0, (popup.winfo_screenwidth() - width) // 2)
        y = max(0, (popup.winfo_screenheight() - height) // 2 - 20)
        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.resizable(False, False)
        popup.attributes("-topmost", True)
        popup.configure(bg=self.COLORS["surface"])
        accent = self.COLORS.get(alert.source.strip().lower(), self.COLORS["agoda"])

        hero = tk.Frame(popup, bg=self.COLORS["primary"], height=118)
        hero.pack(fill="x")
        hero.pack_propagate(False)
        hero_text = tk.Frame(hero, bg=self.COLORS["primary"])
        hero_text.pack(side="left", padx=28, pady=23)
        tk.Label(
            hero_text, text="KHÁCH ĐẾN HÔM NAY", bg=self.COLORS["primary"], fg="#BFC9D8",
            font=("Segoe UI Semibold", 9),
        ).pack(anchor="w")
        self.active_guest_var = tk.StringVar(master=popup, value=alert.guest_name or ("Chờ chi tiết khách" if alert.source == "Booking.com" else "Chưa đọc được tên khách"))
        self.active_room_var = tk.StringVar(master=popup, value=alert.room_type or "—")
        self.active_revenue_var = tk.StringVar(master=popup, value=alert.total_revenue or "—")
        self.active_checkout_var = tk.StringVar(
            master=popup, value=alert.checkout_date.strftime("%d/%m/%Y") if alert.checkout_date else "—",
        )
        self.active_nights_var = tk.StringVar(master=popup, value=str(alert.nights) if alert.nights is not None else "—")
        tk.Label(
            hero_text, textvariable=self.active_guest_var,
            bg=self.COLORS["primary"], fg=self.COLORS["header_text"], font=("Segoe UI Semibold", 22),
            wraplength=430, justify="left",
        ).pack(anchor="w", pady=(6, 0))
        tk.Label(
            hero, text=alert.source.upper(), bg=accent, fg="#FFFFFF", font=("Segoe UI Semibold", 9),
            padx=14, pady=7,
        ).pack(side="right", anchor="n", padx=28, pady=24)

        # Reserve the footer before packing the details, so long room names
        # cannot push the two actions out of the window.
        footer = tk.Frame(popup, bg=self.COLORS["surface"], padx=28, pady=20)
        footer.pack(fill="x", side="bottom")
        body = tk.Frame(popup, bg=self.COLORS["surface"], padx=28, pady=20)
        body.pack(fill="both", expand=True)
        info_card = tk.Frame(
            body, bg=self.COLORS["surface_alt"], highlightbackground=self.COLORS["border"], highlightthickness=1,
            padx=18, pady=12,
        )
        info_card.pack(fill="x")
        rows = [
            ("Mã booking", alert.booking_id),
            ("Hạng phòng", alert.room_type or "—"),
            ("Check-in", alert.checkin_date.strftime("%d/%m/%Y") if alert.checkin_date else "—"),
            ("Check-out", alert.checkout_date.strftime("%d/%m/%Y") if alert.checkout_date else "—"),
            ("Số đêm", str(alert.nights) if alert.nights is not None else "—"),
            ("Tổng thu", alert.total_revenue or "—"),
        ]
        detail_vars = {
            "Hạng phòng": self.active_room_var, "Tổng thu": self.active_revenue_var,
            "Check-out": self.active_checkout_var, "Số đêm": self.active_nights_var,
        }
        for index, (label, value) in enumerate(rows):
            row = tk.Frame(info_card, bg=self.COLORS["surface_alt"])
            row.pack(fill="x", pady=5)
            tk.Label(
                row, text=label.upper(), width=16, anchor="w", bg=self.COLORS["surface_alt"],
                fg=self.COLORS["muted"], font=("Segoe UI Semibold", 8),
            ).pack(side="left")
            tk.Label(
                row, **({"textvariable": detail_vars[label]} if label in detail_vars else {"text": value}),
                anchor="w", bg=self.COLORS["surface_alt"], fg=self.COLORS["text"],
                font=("Segoe UI Semibold", 10), wraplength=380, justify="left",
            ).pack(side="left", fill="x", expand=True)
            if index < len(rows) - 1:
                tk.Frame(info_card, bg="#E8E2D8", height=1).pack(fill="x", pady=(2, 0))

        self.copy_feedback_var = tk.StringVar(value="Chép 9 cột mẫu cũ (STT trống) • Dán từ cột A trong Excel")
        tk.Label(
            footer, textvariable=self.copy_feedback_var, bg=self.COLORS["surface"], fg=self.COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(0, 12))
        if alert.source in {"Expedia", "Traveloka"}:
            ttk.Button(
                footer, name=f"print_{alert.source.lower()}", text=f"In phiếu {alert.source} - 1 trang A4",
                command=lambda selected=alert: self._request_source_print(selected),
                style="Secondary.TButton", takefocus=True,
            ).pack(fill="x", ipady=8, pady=(0, 12))
        if alert.source == "Booking.com":
            self.active_booking_details_var = tk.StringVar(master=popup, value="Đã lấy đầy đủ chi tiết từ Extranet." if alert.details_loaded_at else "Email chỉ có mã/ngày đến. Đăng nhập trong Cài đặt để tự bổ sung chi tiết.")
            tk.Label(footer, textvariable=self.active_booking_details_var, bg=self.COLORS["surface"], fg=self.COLORS["muted"],
                     wraplength=560, justify="left", font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 10))
        actions = tk.Frame(footer, name="booking_actions", bg=self.COLORS["surface"])
        actions.pack(fill="x")
        actions.columnconfigure((0, 1), weight=1, uniform="popup_actions")
        actions.rowconfigure(0, minsize=96)
        ttk.Button(
            actions, name="copy_booking", text="Sao chép", command=self.copy_active_alert,
            style="Popup.Primary.TButton", takefocus=True,
        ).grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Button(
            actions, name="close_notification", text="Đóng thông báo", command=self.acknowledge_alert,
            style="Popup.Secondary.TButton", takefocus=True,
        ).grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.active_menu = tk.Menu(
            popup, tearoff=False, bg=self.COLORS["surface"], fg=self.COLORS["text"],
            activebackground=self.COLORS["gold"], activeforeground="#FFFFFF", relief="solid", borderwidth=1,
        )
        self.active_menu.add_command(label="Sao chép booking sang Excel", command=self.copy_active_alert)
        if alert.source in {"Expedia", "Traveloka"}:
            self.active_menu.add_command(label=f"In phiếu {alert.source} - 1 trang A4",
                                         command=lambda selected=alert: self._request_source_print(selected))
        popup.bind("<Button-3>", self.show_active_context_menu)
        popup.bind("<Button-2>", self.show_active_context_menu)
        popup.bind("<Control-c>", lambda _event: self.copy_active_alert())
        popup.protocol("WM_DELETE_WINDOW", self.acknowledge_alert)
        popup.update_idletasks()
        # Accommodate Windows font/DPI settings without truncating either label.
        button_width = max(child.winfo_reqwidth() for child in actions.winfo_children())
        width = max(width, 2 * button_width + 12 + 56)
        x = max(0, (popup.winfo_screenwidth() - width) // 2)
        popup.geometry(f"{width}x{height}+{x}+{y}")
        self._present_alert_popup(popup)
        self.log(f"POPUP {alert.source} {alert.booking_id or '(không có mã)'}: đã mở thông báo check-in hôm nay.")
        # An audio driver/file error must never dismiss a valid booking notification.
        try:
            self.play_sound()
        except Exception as exc:
            LOGGER.exception("Booking sound failed; popup remains visible")
            self.log(f"Không phát được âm thanh: {exc}; popup booking vẫn mở.")
        try:
            if self.config.get("f92_enabled", True):
                self.f92_worker.notify(alert)
        except Exception as exc:
            LOGGER.exception("F92 enqueue failed; popup remains visible")
            self.log(f"F92: {exc}; popup booking vẫn mở.")

    def play_sound(self) -> None:
        source = self.active_alert.source if self.active_alert is not None else ""
        self._start_source_sound(source, self.config)

    def _start_source_sound(self, source: str, config: dict[str, Any]) -> None:
        self.stop_sound()
        self.sound_active = True
        self.sound_uses_file = False
        if os.name == "nt":
            import winsound

            def play_file(path: Path) -> bool:
                try:
                    if not path.is_file():
                        raise FileNotFoundError("Không tìm thấy tệp")
                    if path.suffix.lower() == ".mp3":
                        self.mp3_player.play_loop(path)
                    elif path.suffix.lower() == ".wav":
                        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP | winsound.SND_NODEFAULT)
                    else:
                        raise ValueError("Chỉ hỗ trợ MP3 / WAV")
                    return True
                except Exception:
                    LOGGER.exception("Cannot play %s sound file", source)
                    self.mp3_player.stop()
                    self.log(f"Không phát được tệp âm thanh {source}; đang thử âm thanh dự phòng.")
                    return False

            source_config = {**config, "sound_file": ""}
            candidates = configured_sound_paths(source_config, source)
            if source.strip().lower() in SOURCE_SOUND_FILES:
                try:
                    candidates.append(bundled_source_sound(source, APP_DIR / "sounds"))
                except Exception:
                    LOGGER.exception("Cannot prepare bundled %s MP3; continue with fallback audio", source)
                try:
                    candidates.append(bundled_source_pcm(source, APP_DIR / "sounds"))
                except Exception:
                    LOGGER.exception("Cannot prepare bundled %s PCM; continue with fallback audio", source)
            elif source.strip().lower() == "booking.com":
                try:
                    candidates.append(default_source_sound(source, APP_DIR / "sounds"))
                except Exception:
                    LOGGER.exception("Cannot prepare Booking.com chime; continue with fallback audio")
            candidates.extend(configured_sound_paths({"sound_file": config.get("sound_file", "")}, source))
            for path in dict.fromkeys(candidates):
                if play_file(path):
                    self.sound_uses_file = True
                    break
            if not self.sound_uses_file and source.strip().lower() in SOURCE_SOUND_KEYS:
                try:
                    self.sound_uses_file = play_file(default_source_sound(source, APP_DIR / "sounds"))
                except Exception:
                    LOGGER.exception("Cannot create built-in source sound")
                    self.log(f"Không tạo được chuông mặc định {source}; đang dùng chuông Windows.")
            if not self.sound_uses_file:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        if not self.sound_uses_file:
            self.sound_repeat_job = self.root.after(3000, self._repeat_beep)

    def _repeat_beep(self) -> None:
        self.sound_repeat_job = None
        if not self.sound_active:
            return
        if os.name == "nt" and not self.sound_uses_file:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        self.sound_repeat_job = self.root.after(3000, self._repeat_beep)

    def stop_sound(self) -> None:
        self.sound_active = False
        for name in ("sound_repeat_job", "sound_preview_job"):
            job = getattr(self, name, None)
            if job is not None:
                self.root.after_cancel(job)
                setattr(self, name, None)
        if os.name == "nt":
            import winsound

            try:
                winsound.PlaySound(None, 0)
            except Exception:
                LOGGER.exception("Cannot stop booking sound")
            finally:
                self.mp3_player.stop()

    def copy_active_alert(self) -> None:
        if not self.active_alert:
            return
        copied = self._copy_alerts_to_clipboard([self.active_alert], "Đã sao chép booking sang Excel")
        if copied and hasattr(self, "copy_feedback_var"):
            self.copy_feedback_var.set("Đã sao chép • Mở Excel và nhấn Ctrl+V")

    def _refresh_pending_details(self) -> None:
        """Update repaired details in-place: preserve the popup, focus and sound."""
        pending = {event.storage_id: event for event in self.state.pending_for_date(date.today())}
        alerts = list(self.alert_queue)
        if self.active_alert is not None:
            alerts.append(self.active_alert)
        for alert in alerts:
            saved = pending.get(alert.storage_id)
            if saved is None or saved.checkin_date != alert.checkin_date:
                continue
            changed = False
            for field in ("guest_name", "room_type", "total_revenue", "checkout_date", "subject", "sender", "received_at", "details_url", "details_loaded_at"):
                value = getattr(saved, field)
                if value and value != getattr(alert, field):
                    setattr(alert, field, value)
                    changed = True
            if alert is self.active_alert and changed:
                if getattr(self, "active_booking_details_var", None) is not None and alert.details_loaded_at:
                    self.active_booking_details_var.set("Đã lấy đầy đủ chi tiết từ Extranet.")
                if self.active_guest_var is not None:
                    self.active_guest_var.set(alert.guest_name or "Chưa đọc được tên khách")
                if self.active_room_var is not None:
                    self.active_room_var.set(alert.room_type or "—")
                for variable_name, value in (
                    ("active_revenue_var", alert.total_revenue or "—"),
                    ("active_checkout_var", alert.checkout_date.strftime("%d/%m/%Y") if alert.checkout_date else "—"),
                    ("active_nights_var", str(alert.nights) if alert.nights is not None else "—"),
                ):
                    variable = getattr(self, variable_name, None)
                    if variable is not None:
                        variable.set(value)
                try:
                    self.f92_worker.notify(alert, play_sound=False)
                except Exception:
                    LOGGER.exception("Cannot refresh repaired booking details on F92")

    def show_active_context_menu(self, event: tk.Event) -> str:
        if not self.active_alert:
            return "break"
        try:
            self.active_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.active_menu.grab_release()
        return "break"

    def acknowledge_alert(self) -> None:
        if not self.active_alert:
            return
        alert = self.active_alert
        self.state.acknowledge(alert)
        self.queued_ids.discard(alert.storage_id)
        self.active_alert = None
        self.stop_sound()
        if self.active_popup:
            self.active_popup.destroy()
            self.active_popup = None
        self.active_guest_var = None
        self.active_room_var = None
        self.active_revenue_var = None
        self.active_checkout_var = None
        self.active_nights_var = None
        self.active_booking_details_var = None
        self.refresh_history()
        self._show_next_alert()

    def refresh_history(self) -> None:
        self.history_day = date.today()
        self.history_date_var.set(self.history_day.strftime("Hôm nay • %d/%m/%Y"))
        self.history_rows: dict[str, dict[str, Any]] = {}
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        for record in self.state.history():
            if record.get("checkin_date") != self.history_day.isoformat():
                continue
            index = len(self.history_rows)
            self.history_rows[str(index)] = dict(record)
            checkin = str(record.get("checkin_date", ""))
            try:
                checkin = date.fromisoformat(checkin).strftime("%d/%m/%Y") if checkin else ""
            except ValueError:
                pass
            self.history_tree.insert("", "end", iid=str(index), values=(
                record.get("source", "Agoda"), record.get("booking_id", ""),
                record.get("guest_name", ""), record.get("room_type", ""),
                checkin, record.get("total_revenue", ""),
            ), tags=("even" if index % 2 == 0 else "odd",))

    def show_history_context_menu(self, event: tk.Event) -> str:
        row_id = self.history_tree.identify_row(event.y)
        if not row_id:
            return "break"
        if row_id not in self.history_tree.selection():
            self.history_tree.selection_set(row_id)
        self.history_tree.focus(row_id)
        self.history_menu.delete(0, "end")
        self.history_menu.add_command(label="Sao chép dòng đã chọn sang Excel", command=self.copy_selected_history)
        record = self.history_rows.get(row_id)
        if record and record.get("source") in {"Expedia", "Traveloka"}:
            selected = BookingEvent.from_dict(record)
            self.history_menu.add_command(label=f"In phiếu {selected.source} - 1 trang A4",
                                         command=lambda alert=selected: self._request_source_print(alert))
        try:
            self.history_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.history_menu.grab_release()
        return "break"

    def request_expedia_print(self, alert: BookingEvent) -> None:
        """Keep the original Expedia action entry point for saved UI integrations."""
        if alert.source == "Expedia":
            self.request_booking_print(alert)

    def _request_source_print(self, alert: BookingEvent) -> None:
        if alert.source == "Expedia":
            self.request_expedia_print(alert)
        else:
            self.request_booking_print(alert)

    def request_booking_print(self, alert: BookingEvent) -> None:
        if alert.source not in {"Expedia", "Traveloka"} or self.closing or self.print_loading or self.print_spooling:
            return
        try:
            password = unprotect_secret(str(self.config.get("password_encrypted", "")))
        except Exception:
            password = ""
        if not password or not self.config.get("email_address"):
            messagebox.showerror(f"In phiếu {alert.source}", "Hãy lưu email và mật khẩu ứng dụng trong Cài đặt trước khi in.",
                                 parent=self.active_popup or self.root)
            return
        self.print_loading = True
        config = dict(self.config)
        # Freeze the clicked booking, even if the next notification opens while IMAP is loading.
        selected = BookingEvent.from_dict(alert.to_dict())
        self.set_status(f"Đang đọc email gốc để tạo phiếu {selected.source} A4…")

        def worker() -> None:
            image, error = None, ""
            try:
                if selected.source == "Expedia":
                    image = render_expedia_a4(fetch_expedia_print(config, password, selected))
                else:
                    image = render_booking_a4(fetch_traveloka_print(config, password, selected))
            except ExpediaPrintError as exc:
                error = str(exc)
            except Exception:
                # No exception traceback, raw email or card data in logs/UI queues.
                error = "Không tạo được phiếu A4. Hãy kiểm tra email gốc và thử lại."
            if not self.closing:
                self.events.put(("expedia_print_ready", (selected, image, error)))
            elif image is not None:
                image.close()

        threading.Thread(target=worker, name=f"{selected.source}PrintLoad", daemon=True).start()

    def close_expedia_print_preview(self) -> None:
        self.close_booking_print_preview()

    def close_booking_print_preview(self) -> None:
        if self.print_spooling and not self.closing:
            return  # Keep the native print dialog's owner valid until cancel/completion.
        if self.print_preview is not None:
            self.print_preview.destroy()
            self.print_preview = None
        self.print_preview_photo = None
        if self.print_preview_image is not None:
            self.print_preview_image.close()
            self.print_preview_image = None
        if not self.closing and self.active_popup is not None:
            self._present_alert_popup(self.active_popup)

    def show_expedia_print_preview(self, alert: BookingEvent, image: Any) -> None:
        self.show_booking_print_preview(alert, image)

    def show_booking_print_preview(self, alert: BookingEvent, image: Any) -> None:
        from PIL import Image, ImageTk

        if self.print_spooling:
            image.close()
            self.set_status("Đang in phiếu trước đó; hãy thử lại sau khi in/hủy in xong")
            return
        self.close_booking_print_preview()
        preview = tk.Toplevel(self.root)
        self.print_preview, self.print_preview_image = preview, image
        preview.title(f"{alert.source} - Phiếu A4 - {alert.booking_id}")
        preview.configure(bg=self.COLORS["bg"])
        # Independent of the hidden main window, just like booking popups.
        preview.attributes("-topmost", True)
        preview.protocol("WM_DELETE_WINDOW", self.close_booking_print_preview)
        preview.bind("<Escape>", lambda _event: self.close_booking_print_preview())
        tk.Label(preview, text=f"PHIẾU {alert.source.upper()} / 1 TRANG A4", font=("Segoe UI Semibold", 14),
                 bg=self.COLORS["bg"], fg=self.COLORS["primary"]).pack(pady=(14, 4))
        tk.Label(preview, text="Nội bộ: có thông tin thẻ/CVV. Không giao khách. Bảo quản và hủy giấy an toàn.\n"
                 "Ứng dụng không lưu phiếu vào đĩa; máy in hoặc máy in PDF có thể lưu bản in.",
                 font=("Segoe UI", 9), bg=self.COLORS["bg"], fg=self.COLORS["muted"], justify="center").pack(padx=16, pady=(0, 10))
        thumbnail = image.copy()
        thumbnail.thumbnail((700, max(360, min(760, preview.winfo_screenheight() - 250))), Image.Resampling.LANCZOS)
        self.print_preview_photo = ImageTk.PhotoImage(thumbnail, master=preview)
        thumbnail.close()
        tk.Label(preview, image=self.print_preview_photo, borderwidth=1, relief="solid").pack(padx=20)
        feedback = tk.StringVar(master=preview, value="Kiểm tra tên khách, tất cả phòng, khoản thu và thẻ trước khi in.")
        tk.Label(preview, textvariable=feedback, font=("Segoe UI", 9), wraplength=640, justify="center",
                 bg=self.COLORS["bg"], fg=self.COLORS["muted"]).pack(padx=14, pady=8)
        actions = tk.Frame(preview, name="print_actions", bg=self.COLORS["bg"])
        actions.pack(fill="x", padx=20, pady=(0, 16))
        actions.columnconfigure((0, 1), weight=1, uniform="print_actions")

        def send_to_printer() -> None:
            if self.print_spooling:
                return
            try:
                owner = preview.winfo_id()
                if os.name == "nt":
                    import ctypes

                    ancestor = ctypes.windll.user32.GetAncestor
                    ancestor.argtypes, ancestor.restype = [ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p
                    owner = ancestor(owner, 2) or owner
            except ExpediaPrintError as exc:
                feedback.set(str(exc))
                return
            except Exception:
                feedback.set("Không kết nối được máy in. Hãy kiểm tra máy in Windows.")
                return
            print_button.configure(state="disabled")
            actions.nametowidget("close_preview").configure(state="disabled")
            self.print_spooling = True
            feedback.set("Chọn máy in Windows để in đúng 1 trang A4…")
            page = image.copy()

            def worker() -> None:
                error = ""
                job = None
                try:
                    # Native printer dialog/spooling must not block Tk's booking queue.
                    job = choose_printer(owner)
                    if job is not None:
                        job.print_page(page)
                    else:
                        error = "Đã hủy in; popup booking vẫn giữ nguyên."
                except ExpediaPrintError as exc:
                    error = str(exc)
                except Exception:
                    error = "Không gửi được phiếu đến máy in. Hãy kiểm tra máy in và thử lại."
                finally:
                    page.close()
                    if job is not None:
                        job.close()
                if not self.closing:
                    self.events.put(("expedia_print_done", (preview, print_button, feedback, error)))

            threading.Thread(target=worker, name=f"{alert.source}PrintSpool", daemon=True).start()

        print_button = ttk.Button(actions, text="In 1 trang A4", command=send_to_printer, style="Primary.TButton")
        print_button.grid(row=0, column=0, sticky="nsew", ipady=10, padx=(0, 5))
        ttk.Button(actions, name="close_preview", text="Đóng bản xem trước", command=self.close_booking_print_preview,
                   style="Secondary.TButton").grid(row=0, column=1, sticky="nsew", ipady=10, padx=(5, 0))
        preview.update_idletasks()
        preview.lift()
        preview.focus_force()
        self.set_status(f"Đã mở phiếu {alert.source} A4; ứng dụng vẫn theo dõi booking")

    def copy_selected_history(self, _event: object = None) -> str:
        selection = self.history_tree.selection()
        if not selection:
            return "break"
        alerts: list[BookingEvent] = []
        for item in selection:
            if item in self.history_rows:
                alerts.append(BookingEvent.from_dict(self.history_rows[item]))
        if alerts:
            label = "Đã sao chép booking sang Excel" if len(alerts) == 1 else f"Đã sao chép {len(alerts)} booking sang Excel"
            self._copy_alerts_to_clipboard(alerts, label)
        return "break"

    def _copy_alerts_to_clipboard(self, alerts: list[BookingEvent], status: str) -> bool:
        if any(alert.source == "Booking.com" and not all((alert.guest_name, alert.room_type, alert.checkout_date, alert.total_revenue))
               for alert in alerts):
            messagebox.showinfo("Chưa đủ chi tiết Booking.com", "Email Booking.com này chỉ có mã/ngày đến. Hãy đăng nhập Booking.com trong Cài đặt và đợi lấy đủ chi tiết trước khi chép Excel.",
                                parent=getattr(self, "active_popup", None) or self.root)
            return False
        self.root.clipboard_clear()
        self.root.clipboard_append(excel_tsv_rows(alerts))
        self.root.update_idletasks()
        self.set_status(status)
        return True

    def set_status(self, value: str) -> None:
        self.status_var.set(value)
        lowered = value.lower()
        if "lỗi" in lowered or "thất bại" in lowered or "không" in lowered:
            color = self.COLORS["danger"]
        elif "đang" in lowered or "yêu cầu" in lowered:
            color = self.COLORS["warning"]
        else:
            color = self.COLORS["success"]
        if hasattr(self, "status_label"):
            self.status_label.configure(fg=color)

    def log(self, value: str) -> None:
        LOGGER.info(value)
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{datetime.now():%H:%M:%S}  {value}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def on_close(self) -> None:
        self.hide_to_tray()
        self.log("Ứng dụng vẫn theo dõi email và hiện popup. Bấm biểu tượng Booking Desk dưới khay để mở lại.")

    def _on_main_unmap(self, event: tk.Event) -> None:
        if event.widget is self.root and not self.closing and self.root.state() == "iconic":
            # Run after Windows finishes minimizing; ignore child/settings events.
            self.root.after_idle(self._hide_if_still_minimized)

    def _hide_if_still_minimized(self) -> None:
        if not self.closing and self.root.state() == "iconic":
            self.hide_to_tray()

    def hide_to_tray(self) -> None:
        if self.closing:
            return
        self.hidden_to_tray = True
        self.settings_window.withdraw()
        # Never withdraw without a usable icon: Taskbar remains the safe fallback.
        if self.tray.available:
            self.root.withdraw()
        elif self.root.state() != "iconic":
            self.root.iconify()
        if self.active_popup is not None:
            self._present_alert_popup(self.active_popup)

    def restore_main_window(self) -> None:
        if self.closing:
            return
        if self.startup_hide_job is not None:
            self.root.after_cancel(self.startup_hide_job)
            self.startup_hide_job = None
        self.hidden_to_tray = False
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        if self.active_popup is not None:
            self._present_alert_popup(self.active_popup)

    def exit_app(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.close_expedia_print_preview()
        self.tray.stop()
        if self.f92_clock_job is not None:
            self.root.after_cancel(self.f92_clock_job)
        self.monitor_generation += 1
        if self.monitor:
            self.monitor.stop()
            self.monitor.join(3)
        self.f92_worker.close(3)
        # Let the owning worker close its profile/driver before OTA replaces us.
        # No Tk or browser API is called from a different owning thread.
        self.booking_com_worker.close(35)
        self.stop_sound()
        self.root.destroy()

def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--booking-browser-smoke":
        from booking_notifier.booking_com_browser import packaged_browser_smoke

        return packaged_browser_smoke(Path(sys.argv[2]))
    helper_result = run_update_helper_from_argv(sys.argv)
    if helper_result is not None:
        return helper_result
    setup_logging()
    instance = WindowsSingleInstance()
    if not instance.acquire():
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, "Ứng dụng đã chạy.", APP_NAME, 0x40)
        return 0
    try:
        root = tk.Tk()
        app = BookingNotifierApp(root)
        root.update_idletasks()
        report_update_startup(sys.argv)
        # A real Exit command remains accessible even when the close button minimizes.
        root.bind("<Control-Shift-Q>", lambda _event: app.exit_app())
        root.mainloop()
        return 0
    finally:
        instance.close()


if __name__ == "__main__":
    raise SystemExit(main())
